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
    await model_manager.unload_model(model_id)   # refuse si requêtes actives
    await model_manager.shutdown()
    await model_manager.start_health_monitor()   # no-op en mode local
          model_manager.status()
          model_manager.registry                 → ModelRegistry
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from collections import deque

from config import settings
from gpu_inventory import GpuVramMeasurement, probe_gpu_memory
from model_registry import ModelDefinition, ModelRegistry
from server_manager import LOAD_CAPACITY_MARKERS, ModelState, ServerManager
from telemetry import CAPACITY_QUEUE_SECONDS

log = logging.getLogger(__name__)


# ── Sondes best-effort (non-fatales, sans GPU requis) ─────────────────────────

async def probe_gpu_used_mb() -> float | None:
    """
    Compatibilité : renvoie la VRAM utilisée des devices visibles (Mo).

    Le chemin de mesure réel conserve les UUID dans ``probe_gpu_memory`` et ne
    somme que les devices gouvernés par ``CUDA_VISIBLE_DEVICES``. Ce wrapper
    garde l'ancienne API scalaire pour les intégrations/tests internes ; aucune
    nouvelle décision de capacité ne doit l'utiliser pour perdre l'identité des
    devices.
    """
    timeout = settings.vram_reconcile_probe_timeout_seconds
    try:
        measurement = await probe_gpu_memory(
            timeout,
            cuda_visible_devices=settings.cuda_visible_devices,
        )
    except Exception:
        # L'API historique promettait un best-effort silencieux. Le nouveau
        # chemin expose l'état détaillé au réconciliateur, mais ce wrapper doit
        # continuer à renvoyer ``None`` si un intégrateur remplace la sonde.
        return None
    if not isinstance(measurement, GpuVramMeasurement):
        return None
    return measurement.visible_used_mb


# Les tests et quelques intégrations historiques remplacent le wrapper scalaire
# ci-dessus. Le réconciliateur détecte ce cas pour ne pas casser leur contrat,
# tout en empruntant systématiquement la sonde UUID dans le chemin par défaut.
_DEFAULT_PROBE_GPU_USED_MB = probe_gpu_used_mb


async def _probe_gpu_measurement() -> GpuVramMeasurement:
    """Obtient un résultat UUID, avec un repli de compatibilité injectable."""
    if probe_gpu_used_mb is not _DEFAULT_PROBE_GPU_USED_MB:
        try:
            used_mb = await probe_gpu_used_mb()
        except Exception as exc:
            return GpuVramMeasurement.unavailable(
                "legacy_probe_error", settings.cuda_visible_devices,
                detail=type(exc).__name__,
            )
        if used_mb is None:
            return GpuVramMeasurement.unavailable(
                "legacy_probe_unavailable", settings.cuda_visible_devices
            )
        return GpuVramMeasurement.measured_aggregate(
            used_mb, settings.cuda_visible_devices
        )
    try:
        measurement = await probe_gpu_memory(
            settings.vram_reconcile_probe_timeout_seconds,
            cuda_visible_devices=settings.cuda_visible_devices,
        )
    except Exception as exc:
        return GpuVramMeasurement.unavailable(
            "gpu_probe_error", settings.cuda_visible_devices,
            detail=type(exc).__name__,
        )
    if not isinstance(measurement, GpuVramMeasurement):
        return GpuVramMeasurement.unavailable(
            "gpu_probe_invalid", settings.cuda_visible_devices
        )
    return measurement


def _port_is_occupied(port: int, host: str = "127.0.0.1", connect_timeout: float = 0.2) -> bool:
    """
    Détection best-effort d'un port TCP déjà occupé (stdlib, pas de psutil).
    True si une connexion s'établit (quelqu'un écoute). Attrape TOUT → False
    en cas d'erreur (jamais fatal, jamais bloquant longtemps).
    """
    try:
        with socket.create_connection((host, port), timeout=connect_timeout):
            return True
    except Exception:
        return False


# ── Erreurs d'admission VRAM ──────────────────────────────────────────────────

class CapacityQueueError(RuntimeError):
    """Erreur temporaire liée à la queue d'admission VRAM."""


class CapacityQueueFull(CapacityQueueError):
    """La queue d'admission est pleine."""


class CapacityQueueTimeout(CapacityQueueError):
    """La requête a trop attendu une libération de capacité."""


# ── Erreurs de cycle de vie (opérations admin) ────────────────────────────────

class ModelBusyError(RuntimeError):
    """
    Une opération admin de déchargement a été refusée : le modèle traite encore
    des requêtes actives après le drain. Mappée en HTTP 409 par admin.py.

    Équivalent local du refus explicite de `ClusterManager.unload_model()`.
    """


class ModelDrainingError(RuntimeError):
    """
    Admission refusée : le modèle est en cours de déchargement administratif
    (quarantaine). Temporaire — la requête peut être réessayée. Mappée en 503
    comme les autres RuntimeError d'admission.
    """


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

    # Le forçage d'un déchargement malgré des requêtes actives n'existe qu'en
    # mode local (la gateway possède ses propres sous-processus). admin.py teste
    # cet attribut plutôt que le mode de déploiement.
    supports_unload_force = True

    def __init__(self, registry: ModelRegistry) -> None:
        self._registry = registry

        self._managers: dict[str, ServerManager] = {}
        self._allocated_ports: dict[str, int] = {}
        self._port_pool: list[int] = list(range(
            settings.base_llama_port,
            settings.base_llama_port + settings.max_loaded_models,
        ))

        # Modèles always-on : chargés au démarrage, exempt d'idle timeout,
        # rechargés automatiquement quand un autre modèle se décharge pour inactivité.
        self._always_on: set[str] = set(settings.always_on_models) if settings.always_on_models else set()

        self._capacity_cond = asyncio.Condition()
        self._capacity_waiters: deque[object] = deque()
        self._model_locks: dict[str, asyncio.Lock] = {}

        # Modèles en quarantaine : un déchargement admin est en cours de drain.
        # Aucune NOUVELLE requête n'est admise sur ces modèles, sinon le drain
        # pourrait ne jamais converger. Toujours vidé dans un bloc finally.
        self._draining: set[str] = set()
        # Gate fail-closed du bootstrap. Il survit à un unload en échec ; seul
        # le protocole bootstrap explicite peut rouvrir l'admission.
        self._bootstrap_blocked: set[str] = set()

        # Réconciliation VRAM (nvidia-smi) — additif dans status(). Le résultat
        # conserve l'identité UUID de chaque device et l'état explicite d'une
        # mesure indisponible ; aucun agrégat stale ne doit survivre à un échec
        # de sonde ultérieur.
        self._vram_reconcile_task: asyncio.Task | None = None
        self._last_gpu_used_mb: float | None = None
        self._last_vram_drift_mb: float | None = None
        self._last_gpu_measurement = GpuVramMeasurement.not_measured(
            settings.cuda_visible_devices
        )

    # ── Point d'entrée principal ──────────────────────────────────────────────

    async def ensure_model_loaded(self, model_id: str) -> ServerManager:
        """Sérialise admission/chargement avec le rollback du même modèle."""
        # Rejet immédiat pendant un drain, sans attendre le lock détenu par
        # l'unload. Le même contrôle est refait sous lock ci-dessous.
        self._admitted_model(model_id)
        model_lock = self._model_locks.setdefault(model_id, asyncio.Lock())
        async with model_lock:
            return await self._ensure_model_loaded_locked(model_id)

    def block_bootstrap_admission(self, model_id: str) -> None:
        """Ferme l'admission synchroniquement, avant tout await de rollback."""
        self._bootstrap_blocked.add(model_id)
        self._notify_capacity_changed()

    def unblock_bootstrap_admission(self, model_id: str) -> None:
        self._bootstrap_blocked.discard(model_id)
        self._notify_capacity_changed()

    def is_model_loaded(self, model_id: str) -> bool:
        manager = self._managers.get(model_id)
        return manager is not None and manager.state in {
            ModelState.LOADING, ModelState.READY, ModelState.UNLOADING,
        }

    def _admitted_model(self, model_id: str) -> ModelDefinition:
        if model_id in self._bootstrap_blocked or model_id in self._draining:
            raise ModelDrainingError(
                f"Le modèle '{model_id}' est fermé par une transition administrative."
            )
        model = self._registry.get(model_id)
        if model is None:
            raise LookupError(f"Modèle inconnu : '{model_id}'. Consultez GET /admin/models.")
        if not model.enabled:
            raise PermissionError(
                f"Le modèle '{model_id}' est désactivé dans le registre. "
                f"Activez-le via PATCH /admin/models/{model_id}."
            )
        return model

    async def _ensure_model_loaded_locked(self, model_id: str) -> ServerManager:
        """
        Garantit qu'un modèle est chargé et retourne son ServerManager.

        1. Valide que le modèle est dans le registre et activé.
        2. Fast path si déjà READY.
        3. Sous lock : vérifie le budget VRAM, évinçe LRU si nécessaire,
           alloue un port et crée le ServerManager.
        4. Attend le chargement hors du lock.
        """
        model = self._admitted_model(model_id)

        force_extra_eviction = False
        last_error: Exception | None = None
        for attempt in range(2):
            model = self._admitted_model(model_id)

            manager = self._managers.get(model_id)
            if manager and manager.state == ModelState.READY:
                await manager.ensure_loaded()
                self._admitted_model(model_id)
                return manager

            async with self._capacity_cond:
                manager = self._managers.get(model_id)
                if manager and manager.state in (ModelState.READY, ModelState.LOADING, ModelState.UNLOADED):
                    pass
                else:
                    await self._ensure_capacity(model, force_extra_eviction=force_extra_eviction)
                    model = self._admitted_model(model_id)

                    port = self._port_pool.pop(0)
                    self._allocated_ports[model_id] = port

                    manager = ServerManager(
                        model=model,
                        port=port,
                        on_unload=self._on_model_unloaded,
                        on_capacity_change=self._notify_capacity_changed,
                        idle_unload_enabled=model.id not in self._always_on,
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
                self._admitted_model(model_id)
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
        configured = settings.effective_vram_budget_gb() - self._used_vram_gb()
        measurement = self._last_gpu_measurement
        measured_used_mb = measurement.visible_used_mb
        measured_total_mb = measurement.visible_total_mb
        if measured_used_mb is None or measured_total_mb is None:
            return configured
        measured_free_gb = max(0.0, measured_total_mb - measured_used_mb) / 1024.0
        return min(configured, measured_free_gb)

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
        queue_started_at: float | None = None
        queue_outcome = "success"
        extra_eviction_done = not force_extra_eviction
        deadline = time.monotonic() + settings.capacity_queue_timeout_seconds
        try:
            while True:
                self._admitted_model(model.id)
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
                        CAPACITY_QUEUE_SECONDS.observe(
                            0.0,
                            model=model.id,
                            node="local",
                            outcome="full",
                        )
                        raise CapacityQueueFull(
                            f"Impossible de charger '{model.id}' : queue VRAM pleine "
                            f"({settings.capacity_queue_max_waiters} requêtes en attente). "
                            f"Réessayez dans quelques secondes."
                        )
                    ticket = object()
                    queue_started_at = time.monotonic()
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
                    queue_outcome = "timeout"
                    raise CapacityQueueTimeout(
                        f"Impossible de charger '{model.id}' : capacité VRAM indisponible "
                        f"après {settings.capacity_queue_timeout_seconds}s d'attente. "
                        f"Réessayez dans quelques secondes."
                    )

                try:
                    await asyncio.wait_for(self._capacity_cond.wait(), timeout=remaining)
                except asyncio.TimeoutError as exc:
                    queue_outcome = "timeout"
                    raise CapacityQueueTimeout(
                        f"Impossible de charger '{model.id}' : capacité VRAM indisponible "
                        f"après {settings.capacity_queue_timeout_seconds}s d'attente. "
                        f"Réessayez dans quelques secondes."
                    ) from exc
        except asyncio.CancelledError:
            queue_outcome = "cancelled"
            raise
        except Exception:
            if queue_started_at is not None and queue_outcome == "success":
                queue_outcome = "error"
            raise
        finally:
            if queue_started_at is not None:
                CAPACITY_QUEUE_SECONDS.observe(
                    max(0.0, time.monotonic() - queue_started_at),
                    model=model.id,
                    node="local",
                    outcome=queue_outcome,
                )
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
        return any(marker in text for marker in LOAD_CAPACITY_MARKERS)

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

    def _on_model_unloaded(self, model_id: str, reason: str = "manual") -> None:
        if model_id in self._allocated_ports:
            port = self._allocated_ports.pop(model_id)
            self._port_pool.append(port)
            log.debug("Port %d libéré et retourné au pool (modèle '%s')", port, model_id)
        self._managers.pop(model_id, None)

        # Si un modèle se décharge pour inactivité, recharger les always-on qui ne le sont pas.
        if reason == "idle" and self._always_on:
            asyncio.create_task(self._reload_always_on_models())

        self._notify_capacity_changed()

    async def _reload_always_on_models(self) -> None:
        """Recharge les modèles always-on qui ne sont plus chargés."""
        for model_id in list(self._always_on):
            if not self.is_model_loaded(model_id):
                log.info(
                    "Modèle '%s' dans ALWAYS_ON_MODELS — rechargement automatique",
                    model_id,
                )
                try:
                    await self.ensure_model_loaded(model_id)
                except Exception as exc:
                    log.error(
                        "Échec du rechargement automatique de '%s' : %s",
                        model_id, exc,
                    )

    async def load_always_on_models(self) -> None:
        """Charge les modèles always-on au démarrage."""
        if not self._always_on:
            return

        for model_id in list(self._always_on):
            log.info(
                "Chargement automatique du modèle '%s' (ALWAYS_ON_MODELS)",
                model_id,
            )
            try:
                await self.ensure_model_loaded(model_id)
            except Exception as exc:
                log.error(
                    "Échec du chargement initial de '%s' dans ALWAYS_ON_MODELS : %s",
                    model_id, exc,
                )

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

    async def unload_model(
        self,
        model_id: str,
        *,
        force: bool = False,
        drain_timeout: float | None = None,
    ) -> None:
        model_lock = self._model_locks.setdefault(model_id, asyncio.Lock())
        async with model_lock:
            await self._unload_model_locked(
                model_id,
                force=force,
                drain_timeout=drain_timeout,
            )

    async def _unload_model_locked(
        self,
        model_id: str,
        *,
        force: bool = False,
        drain_timeout: float | None = None,
    ) -> None:
        """
        Décharge un modèle à la demande d'un opérateur (routes /admin/*).

        INVARIANT : un modèle qui traite une requête active n'est jamais tué
        silencieusement (mêmes garanties que pour l'éviction LRU et le shutdown).

        Déroulé : quarantaine du modèle (plus aucune nouvelle requête admise),
        drain borné par `drain_timeout` (défaut :
        `settings.admin_unload_drain_timeout_seconds`), puis :
          - drain réussi → déchargement normal ;
          - requêtes encore actives et force=False (défaut) → `ModelBusyError`,
            le modèle reste chargé et immédiatement réutilisable (409 côté HTTP) ;
          - requêtes encore actives et force=True → déchargement quand même, avec
            un log critique (l'opérateur assume l'interruption des générations).

        Sans effet si le modèle n'est pas chargé.
        """
        async with self._capacity_cond:
            manager = self._managers.get(model_id)
        if manager is None:
            return  # rien de chargé : ni quarantaine ni drain nécessaires

        timeout = (
            settings.admin_unload_drain_timeout_seconds
            if drain_timeout is None
            else drain_timeout
        )

        self._draining.add(model_id)
        try:
            drained = await self._drain_pinned(
                timeout, [model_id], context=f"Déchargement admin de '{model_id}'",
            )

            # Le modèle a pu disparaître pendant le drain (moniteur d'inactivité,
            # crash de llama-server, éviction LRU) — rien à décharger alors.
            manager = self._managers.get(model_id)
            if manager is None:
                return

            reason = "admin request"
            if not drained and manager.is_pinned:
                active = getattr(manager, "active_requests", 1)
                if not force:
                    raise ModelBusyError(
                        f"Le modèle '{model_id}' traite encore {active} requête(s) "
                        f"active(s) après {timeout:.0f}s de drain — déchargement "
                        f"refusé pour ne pas interrompre les générations en cours. "
                        f"Réessayez plus tard, ou passez force=true pour "
                        f"interrompre explicitement les requêtes actives."
                    )
                log.critical(
                    "Déchargement FORCÉ de '%s' : %s requête(s) active(s) "
                    "interrompue(s) sur demande explicite de l'opérateur "
                    "(force=true) après %.0fs de drain.",
                    model_id, active, timeout,
                )
                reason = "admin request (forcé, requêtes actives interrompues)"

            await manager.unload(reason=reason)
        finally:
            # La quarantaine ne doit jamais fuir : un modèle laissé « draining »
            # après une erreur serait définitivement inutilisable.
            self._draining.discard(model_id)

    async def unload_all_models(self, *, force: bool = False) -> None:
        """
        Décharge tous les modèles sans arrêter les tâches de la gateway
        (action admin `POST /admin/unload`).

        Comme `unload_model`, refuse par défaut d'interrompre des requêtes
        actives : `ModelBusyError` si le drain n'a pas convergé (409 côté HTTP),
        aucun modèle déchargé dans ce cas. Le chemin de shutdown appelle cette
        méthode avec force=True : au SIGTERM, il faut finir par libérer la VRAM
        et les ports, sous peine de laisser des llama-server orphelins.
        """
        drain_timeout = (
            settings.shutdown_drain_timeout_seconds
            if force
            else settings.admin_unload_drain_timeout_seconds
        )
        context = "Shutdown" if force else "Déchargement admin global"

        quarantined = list(self._managers.keys())
        self._draining.update(quarantined)
        try:
            # Drain borné : attendre que les requêtes actives (modèles pinnés) se
            # terminent avant de tuer leurs llama-server. RETOUR IMMÉDIAT si aucun
            # modèle n'est pinné.
            drained = await self._drain_pinned(drain_timeout, quarantined, context=context)

            if not drained:
                busy = [
                    mid for mid in quarantined
                    if (mgr := self._managers.get(mid)) is not None and mgr.is_pinned
                ]
                if busy and not force:
                    raise ModelBusyError(
                        "Impossible de tout décharger : requêtes actives sur "
                        + ", ".join(f"'{mid}'" for mid in busy)
                        + f" après {drain_timeout:.0f}s de drain. Aucun modèle n'a "
                        "été déchargé. Réessayez plus tard, ou déchargez modèle par "
                        "modèle avec POST /admin/models/{id}/unload?force=true."
                    )
                if busy:
                    log.critical(
                        "%s : déchargement forcé de %d modèle(s) avec requêtes "
                        "encore actives : %s",
                        context, len(busy), ", ".join(busy),
                    )

            model_ids = list(self._managers.keys())
            log.info("Déchargement global : %d modèle(s)…", len(model_ids))
            for model_id in model_ids:
                manager = self._managers.get(model_id)
                if manager:
                    await manager.unload(reason="admin unload all")
        finally:
            self._draining.difference_update(quarantined)

    async def shutdown(self) -> None:
        # Arrêter la tâche de réconciliation VRAM en premier (best-effort).
        await self._stop_vram_reconcile()
        # Le shutdown force : après le drain borné, la VRAM et les ports DOIVENT
        # être libérés avant que systemd ne tue le processus.
        await self.unload_all_models(force=True)

    async def _drain_pinned(
        self,
        timeout: float,
        model_ids: list[str] | None = None,
        *,
        context: str = "Shutdown",
    ) -> bool:
        """
        Attend (borné par `timeout`) que les modèles visés libèrent leurs requêtes
        actives. Poll court. Retourne immédiatement si rien n'est pinné.

        `model_ids=None` → tous les modèles chargés. Retourne True si plus aucune
        requête active n'est en cours au retour, False si le timeout a expiré
        alors que des modèles restaient pinnés.

        Aucun effet de bord : ne décharge rien, ne modifie aucun état. C'est
        l'appelant qui décide quoi faire du résultat (forcer au shutdown,
        refuser en 409 sur une route admin).
        """
        def pinned_ids() -> list[str]:
            targets = list(self._managers.keys()) if model_ids is None else model_ids
            return [
                mid for mid in targets
                if (mgr := self._managers.get(mid)) is not None and mgr.is_pinned
            ]

        pinned = pinned_ids()
        if not pinned:
            return True  # aucune requête active — pas d'attente (cas des tests)

        log.info(
            "%s : %d modèle(s) avec requêtes actives — drain (max %.0fs) : %s",
            context, len(pinned), timeout, ", ".join(pinned),
        )

        poll = max(0.01, settings.shutdown_drain_poll_seconds)
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            pinned = pinned_ids()
            if not pinned:
                log.info("%s : toutes les requêtes actives drainées.", context)
                return True
            await asyncio.sleep(poll)

        still_pinned = pinned_ids()
        if still_pinned:
            log.warning(
                "%s : timeout de drain (%.0fs) — %d modèle(s) avec requêtes "
                "encore actives : %s",
                context, timeout, len(still_pinned), ", ".join(still_pinned),
            )
            return False
        return True

    # ── Réconciliation VRAM (nvidia-smi) — best-effort, non fatal ─────────────

    async def start_vram_reconcile(self) -> None:
        """
        Démarre la tâche périodique de réconciliation VRAM. No-op si désactivée
        (interval=0) ou si déjà démarrée. Appelée au lifespan.
        """
        if settings.vram_reconcile_interval_seconds <= 0:
            return
        if self._vram_reconcile_task and not self._vram_reconcile_task.done():
            return
        # La première admission doit profiter de la capacité physique si elle
        # est disponible. Une sonde absente/échouée reste non fatale et le
        # calcul retombe alors sur le budget configuré historique.
        await self._reconcile_vram_once()
        self._vram_reconcile_task = asyncio.create_task(self._vram_reconcile_loop())

    async def _stop_vram_reconcile(self) -> None:
        task = self._vram_reconcile_task
        self._vram_reconcile_task = None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    async def _vram_reconcile_loop(self) -> None:
        interval = settings.vram_reconcile_interval_seconds
        try:
            while True:
                await asyncio.sleep(interval)
                try:
                    await self._reconcile_vram_once()
                except Exception as exc:  # non fatal — la boucle continue
                    log.debug("Réconciliation VRAM ignorée (erreur non fatale) : %s", exc)
        except asyncio.CancelledError:
            raise

    async def _reconcile_vram_once(self) -> None:
        """
        Compare la VRAM réelle visible (nvidia-smi, par UUID) à la somme des
        vram_gb déclarés des modèles READY. Log un warning si dérive
        significative. Une sonde indisponible reste visible dans le statut et
        n'est jamais transformée en zéro ou en ancienne mesure.
        """
        measurement = await _probe_gpu_measurement()
        self._last_gpu_measurement = measurement
        used_mb = measurement.visible_used_mb
        if measurement.status != "measured" or used_mb is None:
            self._last_gpu_used_mb = None
            self._last_vram_drift_mb = None
            return

        declared_gb = self._used_vram_gb()
        declared_mb = declared_gb * 1024.0
        drift_mb = used_mb - declared_mb

        self._last_gpu_used_mb = round(used_mb, 1)
        self._last_vram_drift_mb = round(drift_mb, 1)

        threshold = settings.vram_reconcile_drift_threshold
        # Dérive significative : VRAM réelle dépasse le déclaré de plus de seuil %.
        # On ignore le bruit sous 512 Mo (contexte CUDA résiduel, mesures).
        if declared_mb > 0 and used_mb > declared_mb * (1.0 + threshold) and drift_mb > 512:
            log.warning(
                "Dérive VRAM détectée sur les devices CUDA visibles (%s) : "
                "nvidia-smi rapporte %.0f Mo utilisés, "
                "mais le budget déclaré des modèles READY est %.0f Mo (+%.0f Mo, seuil +%.0f%%). "
                "Vérifiez d'éventuels processus GPU orphelins : pgrep -af llama-server",
                ",".join(measurement.visible_uuids) or "aucun",
                used_mb, declared_mb, drift_mb, threshold * 100,
            )

    # ── Détection des llama-server orphelins au démarrage ─────────────────────

    async def detect_orphan_ports(self) -> list[int]:
        """
        Détection best-effort des ports du pool déjà occupés au démarrage
        (llama-server survivants d'un crash gateway). LOG seulement par défaut.
        Ne tue RIEN sauf si kill_orphan_llama_on_startup=True ET psutil dispo.
        En test, les ports sont libres → retourne [] et n'agit pas.
        """
        occupied = [p for p in self._port_pool if _port_is_occupied(p)]
        if not occupied:
            return []

        port_list = ", ".join(str(p) for p in occupied)
        log.warning(
            "Port(s) du pool déjà occupé(s) au démarrage : %s — llama-server "
            "orphelin(s) probable(s) (crash gateway précédent tenant VRAM/ports). "
            "Diagnostic : pgrep -af llama-server | Nettoyage manuel : "
            "kill <pid> (ou pkill -f llama-server).",
            port_list,
        )

        if settings.kill_orphan_llama_on_startup:
            for port in occupied:
                await self._try_kill_orphan_on_port(port)

        return occupied

    async def _try_kill_orphan_on_port(self, port: int) -> None:
        """
        Kill best-effort du process écoutant `port` SI c'est bien un llama-server.
        Strictement borné : port du pool + ligne de commande contenant
        'llama-server'. Nécessite psutil (import protégé) ; sinon skip + log.
        Attrape TOUT — jamais fatal.
        """
        try:
            import psutil  # optionnel — pas une dépendance dure
        except Exception:
            log.warning(
                "kill_orphan_llama_on_startup=True mais psutil indisponible — "
                "port %d NON nettoyé automatiquement. Installez psutil ou "
                "nettoyez manuellement (pgrep -af llama-server).",
                port,
            )
            return

        try:
            for conn in psutil.net_connections(kind="inet"):
                if conn.laddr and conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                    pid = conn.pid
                    if pid is None:
                        continue
                    try:
                        proc = psutil.Process(pid)
                        cmdline = " ".join(proc.cmdline())
                    except Exception:
                        continue
                    if "llama-server" not in cmdline:
                        log.warning(
                            "Port %d occupé par un process NON-llama-server (PID %d) — "
                            "kill refusé par sécurité.", port, pid,
                        )
                        continue
                    log.critical(
                        "Kill de l'orphelin llama-server PID %d sur port %d (opt-in activé).",
                        pid, port,
                    )
                    try:
                        proc.terminate()
                    except Exception as exc:
                        log.warning("Échec du kill de l'orphelin PID %d : %s", pid, exc)
                    return
        except Exception as exc:
            log.warning("Détection/kill orphelin sur port %d échouée (non fatal) : %s", port, exc)

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
                    "last_load_seconds": None,
                    "llama_params": None,
                }
            models_status.append(entry)

        vram_budget = {
            "total_gb": settings.total_vram_gb,
            "overhead_gb": settings.vram_overhead_gb,
            "safety_margin": settings.vram_safety_margin,
            "used_gb": round(self._used_vram_gb(), 2),
            "available_gb": round(self._available_vram_gb(), 2),
            "budget_net_gb": round(settings.effective_vram_budget_gb(), 2),
        }
        # Le détail par UUID et le statut sont toujours présents : une mesure
        # absente doit être explicite. Les anciens champs scalaires restent
        # additifs et ne sont présents que pour une mesure réussie.
        vram_budget["gpu_measurement"] = self._last_gpu_measurement.to_dict()
        if self._last_gpu_used_mb is not None:
            vram_budget["gpu_used_mb_measured"] = self._last_gpu_used_mb
            vram_budget["vram_drift_mb"] = self._last_vram_drift_mb

        return {
            "vram_budget": vram_budget,
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

    if settings.agent_secret_is_placeholder() or len(settings.agent_secret) < 32:
        raise RuntimeError(
            "CLUSTER_MODE=cluster mais AGENT_SECRET est vide, trop court ou "
            "laissé à sa valeur d'exemple. Définissez un secret d'au moins "
            "32 caractères partagé avec tous les "
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
            load_timeout_seconds=settings.cluster_load_timeout,
            verify=cluster_cfg.tls_verify,
        )
        for node in cluster_cfg.nodes
    ]

    log.info(
        "Nœuds cluster configurés : %s",
        ", ".join(f"{n.id}({n.base_url})" for n in cluster_cfg.nodes),
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
