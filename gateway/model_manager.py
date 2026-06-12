"""
Gestionnaire du pool de modèles — façade locale/cluster.

Ce module expose UN SEUL singleton `model_manager` qui est soit :
  - Un `LocalModelManager`  (CLUSTER_MODE=local, défaut) : comportement historique
    exact — la gateway lance des sous-processus llama-server en local.
  - Un `ClusterManager`     (CLUSTER_MODE=cluster) : délègue aux agents distants.

Tous les imports existants (`from model_manager import model_manager`) fonctionnent
sans modification — la sélection est transparente.

Interface commune exposée par les deux implémentations :
    await model_manager.ensure_model_loaded(model_id) → ServerManager | ClusterModelHandle
    await model_manager.unload_model(model_id)
    await model_manager.shutdown()
    await model_manager.start_health_monitor()   # no-op en mode local
          model_manager.status()
          model_manager.registry                 → ModelRegistry
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque

from config import settings
from model_registry import ModelDefinition, ModelRegistry
from server_manager import ModelState, ServerManager

log = logging.getLogger(__name__)


# ── Erreurs d'admission VRAM ──────────────────────────────────────────────────

class CapacityQueueError(RuntimeError):
    """Erreur temporaire liée à la queue d'admission VRAM."""


class CapacityQueueFull(CapacityQueueError):
    """La queue d'admission est pleine."""


class CapacityQueueTimeout(CapacityQueueError):
    """La requête a trop attendu une libération de capacité."""


# ── LocalModelManager (mode local — comportement historique) ──────────────────

class LocalModelManager:
    """
    Gestionnaire du pool de ServerManager actifs.

    Responsabilités :
      - Maintenir un pool de ServerManager (un par modèle chargé)
      - Enforcer le budget VRAM avant chaque chargement
      - Évincer le modèle le moins récemment utilisé (LRU) si le budget est dépassé
      - Gérer un pool de ports pour les sous-processus llama-server

    Concurrence :
        asyncio.Condition sur les transitions de pool et la queue d'admission VRAM.
        L'attente de capacité relâche le verrou ; le chargement llama-server se
        fait hors condition après réservation du port.
    """

    def __init__(self, registry: ModelRegistry) -> None:
        self._registry = registry

        self._managers: dict[str, ServerManager] = {}
        self._allocated_ports: dict[str, int] = {}
        self._port_pool: list[int] = list(range(
            settings.base_llama_port,
            settings.base_llama_port + settings.max_loaded_models,
        ))

        self._capacity_cond = asyncio.Condition()
        self._capacity_waiters: deque[object] = deque()

    # ── Point d'entrée principal ──────────────────────────────────────────────

    async def ensure_model_loaded(self, model_id: str) -> ServerManager:
        """
        Garantit qu'un modèle est chargé et retourne son ServerManager.

        1. Valide que le modèle est dans le registre et activé.
        2. Fast path si déjà READY.
        3. Sous lock : vérifie le budget VRAM, évinçe LRU si nécessaire,
           alloue un port et crée le ServerManager.
        4. Attend le chargement hors du lock.
        """
        model = self._registry.get(model_id)
        if model is None:
            raise LookupError(f"Modèle inconnu : '{model_id}'. Consultez GET /admin/models.")
        if not model.enabled:
            raise PermissionError(
                f"Le modèle '{model_id}' est désactivé dans le registre. "
                f"Activez-le via PATCH /admin/models/{model_id}."
            )

        force_extra_eviction = False
        last_error: Exception | None = None
        for attempt in range(2):
            manager = self._managers.get(model_id)
            if manager and manager.state == ModelState.READY:
                await manager.ensure_loaded()
                return manager

            async with self._capacity_cond:
                manager = self._managers.get(model_id)
                if manager and manager.state in (ModelState.READY, ModelState.LOADING, ModelState.UNLOADED):
                    pass
                else:
                    await self._ensure_capacity(model, force_extra_eviction=force_extra_eviction)

                    port = self._port_pool.pop(0)
                    self._allocated_ports[model_id] = port

                    manager = ServerManager(
                        model=model,
                        port=port,
                        on_unload=self._on_model_unloaded,
                        on_capacity_change=self._notify_capacity_changed,
                    )
                    self._managers[model_id] = manager
                    log.info(
                        "Nouveau ServerManager créé pour '%s' sur port %d "
                        "(%.1f GB VRAM estimée, budget restant après : %.1f GB)",
                        model_id, port, model.vram_gb,
                        self._available_vram_gb() - model.vram_gb,
                    )

            try:
                await manager.ensure_loaded()
                return manager
            except Exception as exc:
                last_error = exc
                await self._forget_failed_manager(model_id, manager)
                if attempt == 0 and self._is_load_capacity_error(exc):
                    force_extra_eviction = True
                    log.warning(
                        "Chargement de '%s' refusé par CUDA malgré le budget estimé. "
                        "Nettoyage puis nouvelle tentative après libération VRAM réelle.",
                        model_id,
                    )
                    continue
                raise

        if last_error:
            raise last_error
        raise RuntimeError(f"Impossible de charger '{model_id}'.")

    # ── Budget VRAM ───────────────────────────────────────────────────────────

    def _used_vram_gb(self) -> float:
        total = 0.0
        for model_id, manager in self._managers.items():
            if manager.state in (ModelState.READY, ModelState.LOADING):
                model = self._registry.get(model_id)
                if model:
                    total += model.vram_gb
        return total

    def _available_vram_gb(self) -> float:
        return settings.effective_vram_budget_gb() - self._used_vram_gb()

    async def _ensure_capacity(
        self,
        model: ModelDefinition,
        *,
        force_extra_eviction: bool = False,
    ) -> None:
        """
        Vérifie que VRAM et port sont disponibles. Évinçe LRU si nécessaire.
        Doit être appelé sous _capacity_cond.
        """
        budget = settings.effective_vram_budget_gb()
        if model.vram_gb > budget:
            raise RuntimeError(
                f"Impossible de charger '{model.id}' : besoin {model.vram_gb:.1f} GB, "
                f"budget VRAM net {budget:.1f} GB. Ce modèle ne peut pas tenir seul."
            )

        ticket: object | None = None
        extra_eviction_done = not force_extra_eviction
        deadline = time.monotonic() + settings.capacity_queue_timeout_seconds
        try:
            while True:
                is_turn = ticket is None and not self._capacity_waiters
                is_turn = is_turn or (
                    ticket is not None
                    and self._capacity_waiters
                    and self._capacity_waiters[0] is ticket
                )

                if is_turn:
                    if not extra_eviction_done:
                        evicted = await self._evict_lru_idle(exclude=model.id)
                        if evicted:
                            extra_eviction_done = True
                        elif not self._has_temporary_capacity_blocker(model):
                            extra_eviction_done = True

                    while not self._has_capacity_for(model):
                        reasons = self._capacity_reasons(model)
                        log.warning(
                            "Capacité insuffisante pour '%s' — %s — tentative d'éviction LRU…",
                            model.id, " | ".join(reasons),
                        )
                        evicted = await self._evict_lru_idle(exclude=model.id)
                        if not evicted:
                            break

                    if extra_eviction_done and self._has_capacity_for(model):
                        return

                if not settings.capacity_queue_enabled:
                    raise self._capacity_unavailable_error(model)

                if ticket is None:
                    if len(self._capacity_waiters) >= settings.capacity_queue_max_waiters:
                        raise CapacityQueueFull(
                            f"Impossible de charger '{model.id}' : queue VRAM pleine "
                            f"({settings.capacity_queue_max_waiters} requêtes en attente). "
                            f"Réessayez dans quelques secondes."
                        )
                    ticket = object()
                    self._capacity_waiters.append(ticket)
                    log.info(
                        "Requête pour '%s' mise en attente de capacité VRAM "
                        "(position=%d/%d, timeout=%ds)",
                        model.id,
                        len(self._capacity_waiters),
                        settings.capacity_queue_max_waiters,
                        settings.capacity_queue_timeout_seconds,
                    )

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CapacityQueueTimeout(
                        f"Impossible de charger '{model.id}' : capacité VRAM indisponible "
                        f"après {settings.capacity_queue_timeout_seconds}s d'attente. "
                        f"Réessayez dans quelques secondes."
                    )

                try:
                    await asyncio.wait_for(self._capacity_cond.wait(), timeout=remaining)
                except asyncio.TimeoutError as exc:
                    raise CapacityQueueTimeout(
                        f"Impossible de charger '{model.id}' : capacité VRAM indisponible "
                        f"après {settings.capacity_queue_timeout_seconds}s d'attente. "
                        f"Réessayez dans quelques secondes."
                    ) from exc
        finally:
            if ticket is not None:
                try:
                    self._capacity_waiters.remove(ticket)
                except ValueError:
                    pass
                self._capacity_cond.notify_all()

    def _has_capacity_for(self, model: ModelDefinition) -> bool:
        return self._available_vram_gb() >= model.vram_gb and bool(self._port_pool)

    def _capacity_reasons(self, model: ModelDefinition) -> list[str]:
        reasons: list[str] = []
        available = self._available_vram_gb()
        if available < model.vram_gb:
            reasons.append(
                f"VRAM insuffisante (besoin {model.vram_gb:.1f} GB, "
                f"disponible {available:.1f} GB)"
            )
        if not self._port_pool:
            reasons.append(
                f"pool de ports épuisé ({settings.max_loaded_models} modèles simultanés max)"
            )
        return reasons or ["capacité temporairement indisponible"]

    def _capacity_unavailable_error(self, model: ModelDefinition) -> RuntimeError:
        reasons = self._capacity_reasons(model)
        busy_models = [
            mid for mid, mgr in self._managers.items()
            if mid != model.id
            and mgr.state == ModelState.READY
            and mgr.is_pinned
        ]
        if busy_models:
            busy_list = ", ".join(f"'{m}'" for m in busy_models)
            return RuntimeError(
                f"Impossible de charger '{model.id}' : {' | '.join(reasons)}. "
                f"Les modèles {busy_list} ont des requêtes en cours et ne peuvent pas être évincés. "
                f"Réessayez dans quelques secondes."
            )
        loading_models = [
            mid for mid, mgr in self._managers.items()
            if mid != model.id and mgr.state == ModelState.LOADING
        ]
        if loading_models:
            loading_list = ", ".join(f"'{m}'" for m in loading_models)
            return RuntimeError(
                f"Impossible de charger '{model.id}' : {' | '.join(reasons)}. "
                f"Les modèles {loading_list} sont en cours de chargement et ne peuvent pas être évincés. "
                f"Réessayez dans quelques secondes une fois leur chargement terminé."
            )
        return RuntimeError(
            f"Impossible de charger '{model.id}' : {' | '.join(reasons)}. "
            f"Aucun modèle idle à évincer. Attendez la fin des requêtes en cours "
            f"ou déchargez un modèle manuellement via POST /admin/models/{model.id}/unload."
        )

    def _has_temporary_capacity_blocker(self, model: ModelDefinition) -> bool:
        for mid, mgr in self._managers.items():
            if mid == model.id:
                continue
            if mgr.state == ModelState.LOADING:
                return True
            if mgr.state == ModelState.READY and mgr.is_pinned:
                return True
        return False

    @staticmethod
    def _is_load_capacity_error(exc: Exception) -> bool:
        text = str(exc).lower()
        return any(
            marker in text
            for marker in (
                "cuda",
                "out of memory",
                "unable to allocate",
                "failed to allocate",
                "failed to fit params to free device memory",
                "cudamalloc failed",
            )
        )

    async def _forget_failed_manager(self, model_id: str, manager: ServerManager) -> None:
        async with self._capacity_cond:
            if self._managers.get(model_id) is manager:
                self._managers.pop(model_id, None)
            port = self._allocated_ports.pop(model_id, None)
            if port is not None and port not in self._port_pool:
                self._port_pool.append(port)
                log.debug("Port %d libéré après échec de chargement (modèle '%s')", port, model_id)
            self._capacity_cond.notify_all()

    async def _evict_lru_idle(self, exclude: str) -> bool:
        candidates = [
            (mid, mgr) for mid, mgr in self._managers.items()
            if mid != exclude
            and mgr.state == ModelState.READY
            and not mgr.is_pinned
        ]
        if not candidates:
            return False

        lru_id, lru_mgr = min(candidates, key=lambda x: x[1]._last_request_time)

        log.info(
            "Éviction LRU : '%s' (idle depuis %.0fs) pour libérer %.1f GB VRAM",
            lru_id, lru_mgr.idle_seconds,
            self._registry.get(lru_id).vram_gb if self._registry.get(lru_id) else 0.0,
        )

        await lru_mgr.unload(reason=f"LRU eviction pour '{exclude}'")
        return True

    # ── Callback de déchargement ──────────────────────────────────────────────

    def _on_model_unloaded(self, model_id: str) -> None:
        if model_id in self._allocated_ports:
            port = self._allocated_ports.pop(model_id)
            self._port_pool.append(port)
            log.debug("Port %d libéré et retourné au pool (modèle '%s')", port, model_id)
        self._managers.pop(model_id, None)
        self._notify_capacity_changed()

    def _notify_capacity_changed(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._notify_capacity_waiters())

    async def _notify_capacity_waiters(self) -> None:
        async with self._capacity_cond:
            self._capacity_cond.notify_all()

    # ── Actions admin ─────────────────────────────────────────────────────────

    async def unload_model(self, model_id: str) -> None:
        async with self._capacity_cond:
            manager = self._managers.get(model_id)
        if manager is None:
            return
        await manager.unload(reason="admin request")

    async def shutdown(self) -> None:
        model_ids = list(self._managers.keys())
        log.info("Shutdown : déchargement de %d modèle(s)…", len(model_ids))
        for model_id in model_ids:
            manager = self._managers.get(model_id)
            if manager:
                await manager.unload(reason="shutdown")

    async def start_health_monitor(self) -> None:
        """No-op en mode local — pas de heartbeat réseau nécessaire."""
        pass

    # ── Statut ────────────────────────────────────────────────────────────────

    def status(self) -> dict:
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
            "capacity_queue": {
                "enabled": settings.capacity_queue_enabled,
                "waiters": len(self._capacity_waiters),
                "max_waiters": settings.capacity_queue_max_waiters,
                "timeout_seconds": settings.capacity_queue_timeout_seconds,
            },
        }

    @property
    def registry(self) -> ModelRegistry:
        return self._registry


# Alias de rétro-compat : le code ancien importe `from model_manager import ModelManager`
ModelManager = LocalModelManager


# ── Sélection du backend selon CLUSTER_MODE ───────────────────────────────────

def _build_manager():
    """
    Construit le manager approprié au mode de déploiement.
    Appelé une seule fois au chargement du module.
    """
    registry = ModelRegistry(
        config_path=settings.models_config_path,
        allowed_model_dirs=settings.allowed_model_dirs if settings.allowed_model_dirs else None,
    )

    if settings.cluster_mode == "local":
        log.info("Mode CLUSTER_MODE=local — gateway mono-nœud (comportement historique).")
        return LocalModelManager(registry=registry)

    # ── Mode cluster ──────────────────────────────────────────────────────────
    log.info("Mode CLUSTER_MODE=cluster — gateway multi-nœuds.")

    if settings.agent_secret_is_placeholder():
        raise RuntimeError(
            "CLUSTER_MODE=cluster mais AGENT_SECRET est vide ou laissé à sa "
            "valeur d'exemple. Définissez un secret fort partagé avec tous les "
            "agents : python3 -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )

    from cluster.nodes_config import load_nodes_config
    from cluster.node_client import RemoteNodeClient
    from cluster.cluster_manager import ClusterManager

    cluster_cfg = load_nodes_config(settings.cluster_nodes_path)
    if not cluster_cfg.nodes:
        raise RuntimeError(
            "CLUSTER_MODE=cluster mais aucun nœud défini dans "
            f"{settings.cluster_nodes_path}"
        )

    clients = [
        RemoteNodeClient(
            node_id=node.id,
            base_url=node.base_url,
            agent_secret=settings.agent_secret,
            timeout_seconds=settings.cluster_request_timeout,
            verify=cluster_cfg.tls_verify,
        )
        for node in cluster_cfg.nodes
    ]

    log.info(
        "Nœuds cluster configurés : %s",
        ", ".join(f"{n.node_id}({n.base_url})" for n in cluster_cfg.nodes),
    )

    return ClusterManager(
        registry=registry,
        nodes=clients,
        health_interval=settings.cluster_health_interval,
        health_failures_to_offline=settings.cluster_health_failures_to_offline,
    )


# ── Singleton global ──────────────────────────────────────────────────────────
# Importé partout dans l'application : proxy.py, admin.py, main.py.

model_manager = _build_manager()
