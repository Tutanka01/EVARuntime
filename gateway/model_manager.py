"""
Gestionnaire du pool de modèles — Budget VRAM + Éviction LRU + Pool de ports.

Responsabilités :
  - Maintenir un pool de ServerManager (un par modèle chargé)
  - Enforcer le budget VRAM avant chaque chargement
  - Évincer le modèle le moins récemment utilisé (LRU) si le budget est dépassé
  - Gérer un pool de ports pour les sous-processus llama-server
  - Recevoir les callbacks de déchargement (idle + admin) pour nettoyer l'état

Logique budget VRAM :
    budget_net = total_vram_gb - vram_overhead_gb - (total_vram_gb × vram_safety_margin)
    avant chargement : si used_vram + model.vram_gb > budget_net → éviction LRU

Concurrence :
    asyncio.Lock sur toutes les transitions de pool (chargement, éviction, déchargement).
    La phase d'attente du chargement (ensure_loaded) se passe hors du lock.
"""
from __future__ import annotations

import asyncio
import logging

from config import settings
from model_registry import ModelDefinition, ModelRegistry
from server_manager import ModelState, ServerManager

log = logging.getLogger(__name__)


class ModelManager:
    """
    Singleton gérant le pool de ServerManager actifs.
    Instancié une fois dans model_manager.py et importé partout.
    """

    def __init__(self, registry: ModelRegistry) -> None:
        self._registry = registry

        # model_id → ServerManager (uniquement les modèles en cours de chargement ou chargés)
        self._managers: dict[str, ServerManager] = {}
        # model_id → port alloué
        self._allocated_ports: dict[str, int] = {}
        # Ports disponibles
        self._port_pool: list[int] = list(range(
            settings.base_llama_port,
            settings.base_llama_port + settings.max_loaded_models,
        ))

        # Lock sur les opérations de pool (chargement / éviction / libération)
        self._pool_lock = asyncio.Lock()

    # ── Point d'entrée principal ──────────────────────────────────────────────

    async def ensure_model_loaded(self, model_id: str) -> ServerManager:
        """
        Garantit qu'un modèle est chargé et retourne son ServerManager.

        1. Valide que le modèle est dans le registre et activé
        2. Fast path si déjà READY
        3. Sous lock : vérifie le budget VRAM, évinçe LRU si nécessaire,
           alloue un port et crée le ServerManager
        4. Attend le chargement hors du lock (pour ne pas bloquer les autres requêtes)
        """
        model = self._registry.get(model_id)
        if model is None:
            raise LookupError(f"Modèle inconnu : '{model_id}'. Consultez GET /admin/models.")
        if not model.enabled:
            raise PermissionError(
                f"Le modèle '{model_id}' est désactivé dans le registre. "
                f"Activez-le via PATCH /admin/models/{model_id}."
            )

        # Fast path : déjà READY (pas de lock nécessaire)
        manager = self._managers.get(model_id)
        if manager and manager.state == ModelState.READY:
            return manager

        # Slow path : sous lock
        async with self._pool_lock:
            # Re-vérifier après acquisition du lock
            manager = self._managers.get(model_id)
            if manager and manager.state in (ModelState.READY, ModelState.LOADING):
                # Déjà en cours — on attend hors du lock ci-dessous
                pass
            else:
                # Nouveau chargement : vérifier le budget VRAM
                await self._ensure_vram_budget(model)

                # Allouer un port
                if not self._port_pool:
                    raise RuntimeError(
                        f"Pool de ports épuisé ({settings.max_loaded_models} modèles max). "
                        f"Déchargez un modèle via POST /admin/models/{{id}}/unload."
                    )
                port = self._port_pool.pop(0)
                self._allocated_ports[model_id] = port

                # Créer le ServerManager avec callback de déchargement
                manager = ServerManager(
                    model=model,
                    port=port,
                    on_unload=self._on_model_unloaded,
                )
                self._managers[model_id] = manager
                log.info(
                    "Nouveau ServerManager créé pour '%s' sur port %d "
                    "(%.1f GB VRAM estimée, budget restant après : %.1f GB)",
                    model_id, port, model.vram_gb,
                    self._available_vram_gb() - model.vram_gb,
                )

        # Attendre le chargement hors du lock
        await manager.ensure_loaded()
        return manager

    # ── Budget VRAM ───────────────────────────────────────────────────────────

    def _used_vram_gb(self) -> float:
        """VRAM consommée par tous les modèles actuellement READY ou LOADING."""
        total = 0.0
        for model_id, manager in self._managers.items():
            if manager.state in (ModelState.READY, ModelState.LOADING):
                model = self._registry.get(model_id)
                if model:
                    total += model.vram_gb
        return total

    def _available_vram_gb(self) -> float:
        """Budget VRAM disponible pour un nouveau modèle."""
        return settings.effective_vram_budget_gb() - self._used_vram_gb()

    async def _ensure_vram_budget(self, model: ModelDefinition) -> None:
        """
        Vérifie que le budget VRAM permet de charger le modèle.
        Évinçe des modèles LRU idle jusqu'à avoir suffisamment d'espace.
        Lève RuntimeError si impossible (aucun modèle idle à évincer).
        Doit être appelé sous _pool_lock.
        """
        while self._available_vram_gb() < model.vram_gb:
            log.warning(
                "Budget VRAM insuffisant pour '%s' (besoin %.1f GB, disponible %.1f GB) "
                "— tentative d'éviction LRU…",
                model.id, model.vram_gb, self._available_vram_gb(),
            )
            evicted = await self._evict_lru_idle(exclude=model.id)
            if not evicted:
                raise RuntimeError(
                    f"VRAM insuffisante pour charger '{model.id}' "
                    f"({model.vram_gb:.1f} GB requis, {self._available_vram_gb():.1f} GB disponibles). "
                    f"Aucun modèle idle à évincer. Attendez la fin des requêtes en cours "
                    f"ou déchargez un modèle manuellement via POST /admin/models/{{id}}/unload."
                )

    async def _evict_lru_idle(self, exclude: str) -> bool:
        """
        Évinçe le modèle READY le moins récemment utilisé (hors `exclude`).
        Retourne True si un modèle a été évincé, False si aucun candidat.
        Doit être appelé sous _pool_lock (relâche brièvement pour await).
        """
        # Candidats : modèles READY non exclus
        candidates = [
            (mid, mgr) for mid, mgr in self._managers.items()
            if mid != exclude and mgr.state == ModelState.READY
        ]
        if not candidates:
            return False

        # LRU : celui avec le _last_request_time le plus ancien
        lru_id, lru_mgr = min(candidates, key=lambda x: x[1]._last_request_time)

        log.info(
            "Éviction LRU : '%s' (idle depuis %.0fs) pour libérer %.1f GB VRAM",
            lru_id, lru_mgr.idle_seconds,
            self._registry.get(lru_id).vram_gb if self._registry.get(lru_id) else 0.0,
        )

        # Décharger hors du lock serait idéal, mais en asyncio single-thread
        # c'est sans danger : unload() est cooperative (await interne)
        await lru_mgr.unload(reason=f"LRU eviction pour '{exclude}'")
        # Note : _on_model_unloaded est appelé par le callback, nettoyant _managers et _allocated_ports
        return True

    # ── Callback de déchargement ──────────────────────────────────────────────

    def _on_model_unloaded(self, model_id: str) -> None:
        """
        Appelé par ServerManager.unload() après déchargement complet.
        Libère le port et retire le manager du pool.
        Opérations dict pures — safe en asyncio single-thread sans lock.
        """
        if model_id in self._allocated_ports:
            port = self._allocated_ports.pop(model_id)
            self._port_pool.append(port)
            log.debug("Port %d libéré et retourné au pool (modèle '%s')", port, model_id)

        self._managers.pop(model_id, None)

    # ── Actions admin ─────────────────────────────────────────────────────────

    async def unload_model(self, model_id: str) -> None:
        """Force le déchargement d'un modèle spécifique."""
        async with self._pool_lock:
            manager = self._managers.get(model_id)
        if manager is None:
            return  # Déjà déchargé
        await manager.unload(reason="admin request")

    async def shutdown(self) -> None:
        """Décharge tous les modèles — appelé au shutdown de la gateway."""
        model_ids = list(self._managers.keys())
        log.info("Shutdown : déchargement de %d modèle(s)…", len(model_ids))
        for model_id in model_ids:
            manager = self._managers.get(model_id)
            if manager:
                await manager.unload(reason="shutdown")

    # ── Statut ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
        """
        Retourne l'état complet du pool pour /admin/status.
        Inclut tous les modèles du registre (chargés ou non) + le budget VRAM.
        """
        models_status = []
        for model in self._registry.list_all():
            manager = self._managers.get(model.id)
            if manager:
                entry = manager.status()
            else:
                entry = {
                    "id": model.id,
                    "description": model.description,
                    "enabled": model.enabled,
                    "vram_gb": model.vram_gb,
                    "capabilities": model.capabilities,
                    "state": ModelState.UNLOADED.value,
                    "path": str(model.path),
                    "pid": None,
                    "port": None,
                    "uptime_seconds": None,
                    "idle_seconds": None,
                    "llama_params": None,
                }
            models_status.append(entry)

        return {
            "vram_budget": {
                "total_gb": settings.total_vram_gb,
                "overhead_gb": settings.vram_overhead_gb,
                "safety_margin": settings.vram_safety_margin,
                "used_gb": round(self._used_vram_gb(), 2),
                "available_gb": round(self._available_vram_gb(), 2),
                "budget_net_gb": round(settings.effective_vram_budget_gb(), 2),
            },
            "models": models_status,
        }

    @property
    def registry(self) -> ModelRegistry:
        return self._registry


# ── Singleton global ──────────────────────────────────────────────────────────
# Instancié ici, importé dans proxy.py, admin.py et main.py.
# Le registre se charge depuis models_config_path défini dans settings.

registry = ModelRegistry(
    config_path=settings.models_config_path,
    allowed_model_dirs=settings.allowed_model_dirs if settings.allowed_model_dirs else None,
)

model_manager = ModelManager(registry=registry)
