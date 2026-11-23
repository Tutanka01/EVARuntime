"""
ClusterManager — orchestrateur multi-nœuds.

Responsabilités :
  - Maintenir un état par nœud (online/offline, modèles chargés, VRAM)
  - Lancer et maintenir le heartbeat de chaque nœud
  - Choisir le nœud cible via scheduler.pick_node (best-fit + éviction LRU)
  - Charger / décharger des modèles en délégant aux agents via NodeClient
  - Retourner un ClusterModelHandle compatible avec l'interface ServerManager
    attendue par proxy.py (pin/unpin/llama_url/.model)

Interface publique (même forme que LocalModelManager) :
  ensure_model_loaded(model_id)    → ClusterModelHandle
  unload_model(model_id)
  shutdown()
  status()
  registry                         → ModelRegistry

Concurrence :
  - asyncio.Lock global uniquement pour les snapshots/mutations en mémoire ;
  - un lock par nœud sérialise load/unload ;
  - un lock par modèle déduplique les chargements concurrents ;
  - aucun I/O réseau long n'est effectué sous le lock global.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from typing import NoReturn
from urllib.parse import urlparse
from uuid import uuid4

from model_registry import ModelDefinition, ModelRegistry
from telemetry import MODEL_LOAD_SECONDS

from .node_client import NodeClient, NodeUnreachableError, NodeProtocolError
from .node_protocol import (
    LoadResponse,
    ModelStateOnNode,
    NodeHealth,
    deployment_digest,
)
from .scheduler import (
    EvictionPlan,
    LoadedModelSnapshot,
    ModelToPlace,
    NodeSnapshot,
    NoPlacementError,
    pick_node,
)

log = logging.getLogger(__name__)


class ClusterUnloadUncertainError(RuntimeError):
    """Le nœud n'a pas confirmé la disparition d'un modèle déchargé."""

    def __init__(self, model_id: str, node_id: str) -> None:
        self.model_id = model_id
        self.node_id = node_id
        super().__init__(
            f"Le déchargement du modèle '{model_id}' sur le nœud '{node_id}' "
            "n'a pas été confirmé ; placement et VRAM conservés."
        )


class ClusterLoadUncertainError(RuntimeError):
    """Le résultat d'un chargement distant ne peut pas encore être établi."""

    def __init__(self, model_id: str, node_id: str, operation_id: str) -> None:
        self.model_id = model_id
        self.node_id = node_id
        self.operation_id = operation_id
        super().__init__(
            f"Le chargement du modèle '{model_id}' sur le nœud '{node_id}' "
            f"reste incertain (opération {operation_id}) ; aucun failover "
            "ne sera tenté avant réconciliation."
        )


class ClusterLoadConflictError(RuntimeError):
    """Le nœud possède une autre génération de la définition demandée."""


class ClusterLoadAbandonedError(RuntimeError):
    """
    Un chargement distant est établi comme perdu : opération inconnue du nœud,
    nœud offline, ou échéance négociée + grâce dépassée. Contrairement à
    `ClusterLoadUncertainError`, la réservation peut être purgée et un
    failover tenté — l'agent ne peut plus produire ce résultat.
    """

    def __init__(
        self, model_id: str, node_id: str, operation_id: str, reason: str
    ) -> None:
        self.model_id = model_id
        self.node_id = node_id
        self.operation_id = operation_id
        self.reason = reason
        super().__init__(
            f"Le chargement du modèle '{model_id}' sur le nœud '{node_id}' "
            f"est abandonné (opération {operation_id}) : {reason}."
        )


# Statuts d'un `failed` agent qui décrivent la capacité du nœud ou une course
# transitoire, pas le modèle : jamais mémorisés comme échec du modèle.
_TRANSIENT_FAILURE_STATUSES = frozenset({409, 429, 503})


class ClusterModelLoadFailedError(RuntimeError):
    """
    Un agent joignable rapporte l'échec terminal d'un chargement (`failed`,
    `deadline_exceeded`). Le nœud a répondu : ce n'est pas une panne du plan de
    contrôle et cela ne compte jamais vers le seuil offline.
    """

    def __init__(
        self,
        model_id: str,
        node_id: str,
        response: LoadResponse,
    ) -> None:
        self.model_id = model_id
        self.node_id = node_id
        self.state = response.state
        self.error_status = response.error_status
        self.error_code = response.error_code
        super().__init__(
            response.message
            or f"Échec de chargement de '{model_id}' sur '{node_id}' ({response.state})"
        )

    @property
    def is_model_failure(self) -> bool:
        """True si l'échec tient au modèle (artefact, llama-server, échéance)."""
        if self.error_code == "unloaded" or self.error_status is None:
            # None : agent antérieur sans `error_status` — un 503 de capacité
            # y serait indiscernable d'un échec du modèle. Inconnu = pas de
            # cache négatif (le nœud ne compte pas non plus comme en panne).
            return False
        return self.error_status not in _TRANSIENT_FAILURE_STATUSES


class _StaleGenerationBlocked(NoPlacementError):
    """Aucun placement : le seul nœud possible sert encore l'ancienne génération."""


# ── État interne par modèle chargé sur un nœud ───────────────────────────────

@dataclass
class _LoadedInfo:
    node_id: str
    llama_url: str
    internal_api_key: str
    vram_gb: float
    # Durée du chargement qui a mis ce handle en place, mesurée côté
    # orchestrateur (RPC réseau inclus — même sémantique que MODEL_LOAD_SECONDS).
    # None si inconnue : modèle déjà chargé sur le nœud au moment du placement.
    last_load_seconds: float | None = None
    _last_request: float = field(default_factory=time.monotonic)
    _active_requests: int = 0
    # Empêche le fast-path de distribuer un nouveau handle pendant une
    # éviction déjà décidée.
    evicting: bool = False
    # Le nœud n'a pas confirmé un unload précédent. L'inventaire et la VRAM
    # restent conservés jusqu'à une confirmation ultérieure.
    unload_uncertain: bool = False
    # Identité de la définition réellement servie par ce handle. Les entrées
    # reconstruites depuis un ancien status peuvent rester vides jusqu'au
    # refresh idempotent sur le nœud.
    deployment_digest: str = ""
    generation_id: str = ""
    operation_id: str | None = None

    def touch(self) -> None:
        self._last_request = time.monotonic()

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_request

    @property
    def active_requests(self) -> int:
        return self._active_requests


@dataclass
class _PendingLoad:
    model_id: str
    deployment_digest: str
    generation_id: str
    operation_id: str
    vram_gb: float
    progress: float = 0.0
    deadline_seconds: float | None = None
    # Origine de la borne absolue d'incertitude : l'agent applique sa propre
    # échéance depuis la réception, une réservation ne peut donc pas lui
    # survivre indéfiniment.
    created_at: float = field(default_factory=time.monotonic)
    # L'agent a prouvé connaître l'opération (réponse au POST ou au GET). Un
    # 404 ultérieur signifie alors qu'il l'a perdue (redémarrage, purge), et
    # non qu'un POST serait encore en vol.
    acknowledged: bool = False


# ── État interne par nœud ─────────────────────────────────────────────────────

@dataclass
class _NodeState:
    node_id: str
    client: NodeClient
    online: bool = True
    consecutive_failures: int = 0
    last_health: NodeHealth | None = None
    # model_id → info sur ce modèle sur CE nœud
    loaded: dict[str, _LoadedInfo] = field(default_factory=dict)
    # Chargements acceptés mais pas encore READY. Ces réservations restent
    # visibles au scheduler même si le RPC initial a expiré.
    pending_loads: dict[str, _PendingLoad] = field(default_factory=dict)
    # Les mutations distantes (load/unload) sont sérialisées par nœud sans
    # immobiliser le lock global du cluster pendant les I/O.
    operation_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Un échec du data-plane exclut temporairement ce couple modèle/nœud.
    suspect_models: set[str] = field(default_factory=set)
    # Empêche une réponse status() lente d'annuler une invalidation plus
    # récente du data-plane ou du heartbeat.
    inventory_revision: int = 0

    def snapshot(self) -> NodeSnapshot:
        """Construit un NodeSnapshot à partir de l'état courant."""
        if self.last_health is None:
            return NodeSnapshot(
                node_id=self.node_id,
                online=False,
                total_vram_gb=0.0,
                used_vram_gb=0.0,
                reported_available_vram_gb=0.0,
                free_ports=0,
            )
        h = self.last_health
        loaded_snapshots = tuple(
            LoadedModelSnapshot(
                id=mid,
                vram_gb=info.vram_gb,
                idle_seconds=info.idle_seconds,
                active_requests=info.active_requests,
            )
            for mid, info in self.loaded.items()
        )
        pending_snapshots = tuple(
            LoadedModelSnapshot(
                id=mid,
                vram_gb=pending.vram_gb,
                # Une opération en cours n'est jamais évictable par une autre
                # demande : son résultat peut encore apparaître côté agent.
                active_requests=1,
            )
            for mid, pending in self.pending_loads.items()
            if mid not in self.loaded
        )
        all_loaded = loaded_snapshots + pending_snapshots
        # L'agent compte déjà un modèle LOADING dans sa VRAM utilisée, ses
        # ports et `loaded_model_ids` : seule une réservation qu'il ne publie
        # pas encore (POST en vol, health antérieur) est retranchée ici.
        unreported = [
            pending
            for mid, pending in self.pending_loads.items()
            if mid not in h.loaded_model_ids
        ]
        pending_vram = sum(p.vram_gb for p in unreported)
        pending_ports = len(unreported)
        return NodeSnapshot(
            node_id=self.node_id,
            online=self.online,
            draining=False,
            total_vram_gb=h.total_vram_gb,
            used_vram_gb=h.used_vram_gb,
            reported_available_vram_gb=max(
                0.0, h.available_vram_gb - pending_vram
            ),
            free_ports=max(0, h.free_ports - pending_ports),
            loaded_models=all_loaded,
        )


# ── Handle retourné à proxy.py ────────────────────────────────────────────────

class ClusterModelHandle:
    """
    Objet compatible ServerManager, retourné par ClusterManager.ensure_model_loaded().

    proxy.py utilise exclusivement :
      handle.pin()
      handle.unpin()
      handle.llama_url(path)
      handle.auth_headers()
      handle.model.id          (→ ModelDefinition)
    """

    def __init__(
        self,
        info: _LoadedInfo,
        model_def: ModelDefinition,
        manager: "ClusterManager",
    ) -> None:
        self._info = info
        self.model = model_def
        self._manager = manager

    def pin(self) -> None:
        self._info._active_requests += 1
        self._info.touch()

    def unpin(self) -> None:
        self._info._active_requests = max(0, self._info._active_requests - 1)
        # Fenêtre idle fraîche après la fin d'une requête (cohérent avec ServerManager).
        self._info.touch()

    def llama_url(self, path: str) -> str:
        return self._info.llama_url.rstrip("/") + path

    def auth_headers(self) -> dict[str, str]:
        """
        Clé interne du llama-server DISTANT, reçue dans LoadResponse.
        Chaque nœud a sa propre INTERNAL_API_KEY — ne pas utiliser celle de
        l'orchestrateur (settings.internal_api_key) pour le canal de données.
        """
        return {"Authorization": f"Bearer {self._info.internal_api_key}"}

    @property
    def active_requests(self) -> int:
        return self._info.active_requests

    @property
    def telemetry_node(self) -> str:
        return self._info.node_id

    async def report_backend_failure(self) -> None:
        """
        Signale un échec de connexion au llama-server.

        L'invalidation est idempotente et compare l'identité de `_LoadedInfo` :
        un vieux handle ne peut donc pas supprimer un placement plus récent.
        Le prochain ensure_model_loaded() basculera sur un autre nœud sans
        attendre les trois heartbeats du plan de contrôle.
        """
        await self._manager._report_backend_failure(self._info)


# ── ClusterManager ─────────────────────────────────────────────────────────────

class ClusterManager:
    """
    Singleton multi-nœuds — remplace LocalModelManager quand CLUSTER_MODE=cluster.
    """

    # Grâce accordée à l'agent (a) pour enregistrer une opération dont le POST
    # est incertain avant qu'un 404 soit tenu pour définitif, et (b) au-delà de
    # l'échéance négociée pour publier un état terminal. Couvre l'attente du
    # verrou de modèle côté agent pendant un unload (SIGTERM 10 s + SIGKILL).
    _operation_grace_seconds: float = 30.0
    # Durée pendant laquelle l'échec d'un chargement (rapporté `failed` par
    # tous les nœuds candidats) est renvoyé sans nouvelle tentative pour la
    # même définition. Court : un opérateur qui corrige l'artefact sur le nœud
    # n'attend jamais plus que cette fenêtre.
    _load_failure_ttl_seconds: float = 10.0
    # Borne défensive des itérations de placement (replans, failovers).
    _max_placement_attempts: int = 64

    def __init__(
        self,
        registry: ModelRegistry,
        nodes: list[NodeClient],
        *,
        health_interval: int = 10,
        health_failures_to_offline: int = 3,
    ) -> None:
        self._registry = registry
        self._health_interval = health_interval
        self._failures_threshold = health_failures_to_offline

        # node_id → _NodeState
        self._nodes: dict[str, _NodeState] = {
            n.node_id: _NodeState(node_id=n.node_id, client=n)
            for n in nodes
        }

        # model_id → node_id (vue globale du placement)
        self._placement: dict[str, str] = {}

        # Lock sur toutes les mutations de _nodes / _placement
        self._lock = asyncio.Lock()

        # Un seul chargement/replacement concurrent par modèle. Les locks par
        # nœud protègent, eux, la capacité et l'ordre unload -> load.
        self._model_locks: dict[str, asyncio.Lock] = {}
        # Identité stable d'une définition tant que le digest du registre ne
        # change pas. Les retries réutilisent la même génération.
        self._desired_identities: dict[str, tuple[str, str]] = {}
        self._bootstrap_blocked: set[str] = set()
        self._unloading_all = False
        # Cache négatif court PAR NŒUD : (model_id, node_id) → (digest,
        # échéance monotone). Un nœud qui a rapporté un échec propre au modèle
        # (GGUF absent, llama-server qui crashe…) est écarté du placement de
        # cette définition ; la requête n'échoue vite que si TOUS les nœuds
        # online sont écartés. Les requêtes en file partagent ainsi l'échec au
        # lieu de rejouer le load sur tout le cluster, sans qu'un seul nœud
        # défaillant bloque un nœud sain momentanément plein.
        self._load_failures: dict[tuple[str, str], tuple[str, float]] = {}

        self._monitor_task: asyncio.Task | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start_health_monitor(self) -> None:
        """Lance la tâche heartbeat. Appelé depuis le lifespan FastAPI."""
        # Premier health-check immédiat pour détecter les nœuds offline au boot
        await self._check_all_nodes()
        # Réconciliation d'état : après un redémarrage de la gateway, des
        # llama-server peuvent encore tourner sur les nœuds. On reconstruit
        # _placement / loaded à partir de status() pour éviter des placements
        # fantômes et des rechargements redondants.
        await self._reconcile_state()
        self._monitor_task = asyncio.create_task(self._health_loop())
        log.info(
            "ClusterManager démarré — %d nœud(s) : %s",
            len(self._nodes),
            ", ".join(
                f"{nid}({'online' if s.online else 'OFFLINE'})"
                for nid, s in self._nodes.items()
            ),
        )

    async def _reconcile_state(self) -> None:
        """
        Reconstruit _placement / _NodeState.loaded à partir de l'état réel des
        nœuds ONLINE (status()). Appelé une fois au démarrage, APRÈS le premier
        _check_all_nodes, pour ne pas repartir d'un état vide alors que des
        llama-server tournent encore côté nœuds (placements fantômes /
        rechargements redondants après un redémarrage de la gateway).

        Note : status() ne renvoie PAS l'internal_api_key du llama-server. Une
        entrée réconciliée porte donc un internal_api_key vide, ce qui la marque
        comme "à rafraîchir" : le fast-path de ensure_model_loaded déclenchera
        un start_load_model() idempotent (already_loaded côté agent) pour récupérer la
        vraie clé + l'URL de confiance lors de la première requête sur ce modèle.
        La réconciliation elle-même ne déclenche AUCUN rechargement.
        """
        await asyncio.gather(
            *(
                self._reconcile_node_status(state)
                for state in self._nodes.values()
                if state.online
            )
        )

    async def _reconcile_node_status(self, state: _NodeState) -> None:
        """Lit status() hors lock global puis applique un inventaire atomique."""
        try:
            async with state.operation_lock:
                async with self._lock:
                    revision = state.inventory_revision
                node_status = await state.client.status()
                async with self._lock:
                    if (
                        not state.online
                        or revision != state.inventory_revision
                    ):
                        return
                    state.last_health = node_status.health
                    self._apply_node_inventory(state, node_status.models)
                    state.inventory_revision += 1
        except (NodeUnreachableError, NodeProtocolError) as exc:
            log.warning(
                "Réconciliation : status() injoignable sur '%s' : %s — nœud sauté.",
                state.node_id, exc,
            )
            return
        except Exception as exc:  # défensif : ne jamais bloquer le boot/monitor
            log.warning(
                "Réconciliation : erreur inattendue sur '%s' : %s — nœud sauté.",
                state.node_id, exc,
            )
            return

    def _apply_node_inventory(
        self, state: _NodeState, models: list[ModelStateOnNode]
    ) -> None:
        """
        Réconcilie l'inventaire détaillé d'un nœud sous `_lock`.

        Les doublons sont conservés dans `state.loaded` : leur VRAM reste donc
        visible et ils sont évictables. Un doublon ne remplace toutefois pas un
        placement primaire sain sur un autre nœud.
        """
        ready: dict[str, tuple[object, str]] = {}
        loading = {
            model.id: model
            for model in models
            if model.state in {"loading", "accepted"}
        }
        for model in models:
            if model.state != "ready":
                continue
            llama_url = self._reconciled_llama_url(state, model.port)
            if llama_url is not None:
                ready[model.id] = (model, llama_url)

        retained: set[str] = set()
        for model_id in set(state.loaded) - set(ready) - set(loading):
            if not self._forget_vanished_locked(state, model_id, "Réconciliation"):
                retained.add(model_id)

        for model_id, (model, llama_url) in ready.items():
            info = state.loaded.get(model_id)
            if (
                info is not None
                and info.unload_uncertain
                and info.generation_id == model.generation_id
            ):
                # Le nœud sert toujours la même génération : l'unload non
                # confirmé n'a pas eu lieu, l'entrée redevient routable.
                info.unload_uncertain = False
                log.info(
                    "Réconciliation : '%s' toujours READY sur '%s' — "
                    "déchargement non confirmé levé.",
                    model_id,
                    state.node_id,
                )
            if info is None:
                info = _LoadedInfo(
                    node_id=state.node_id,
                    llama_url=llama_url,
                    internal_api_key="",
                    vram_gb=model.vram_gb,
                    deployment_digest=model.deployment_digest,
                    generation_id=model.generation_id,
                    operation_id=model.operation_id,
                )
                state.loaded[model_id] = info
            else:
                # Préserver clé, compteurs actifs et LRU d'une entrée connue.
                info.llama_url = llama_url
                info.vram_gb = model.vram_gb
                if model.deployment_digest:
                    info.deployment_digest = model.deployment_digest
                if model.generation_id:
                    info.generation_id = model.generation_id
                if model.operation_id:
                    info.operation_id = model.operation_id

            current_node_id = self._placement.get(model_id)
            current_state = self._nodes.get(current_node_id) if current_node_id else None
            current_is_healthy = bool(
                current_state
                and current_state.online
                and model_id in current_state.loaded
                and model_id not in current_state.suspect_models
            )
            desired = self._registry.get(model_id)
            desired_matches = bool(
                desired is not None
                and (
                    not model.deployment_digest
                    or model.deployment_digest
                    == deployment_digest(desired.to_dict())
                )
            )
            if not desired_matches:
                state.suspect_models.add(model_id)
                if self._placement.get(model_id) == state.node_id:
                    self._placement.pop(model_id, None)
            elif (
                model.deployment_digest
                and model.generation_id
                and desired is not None
                and model.deployment_digest == deployment_digest(desired.to_dict())
            ):
                # Après redémarrage de l'orchestrateur, la génération live du
                # nœud fait autorité pour éviter un rechargement redondant.
                self._desired_identities[model_id] = (
                    model.deployment_digest,
                    model.generation_id,
                )
                # Un suspect (échec data-plane) n'est jamais republié comme
                # route primaire par un simple READY du plan de contrôle.
                if (
                    self._placement.get(model_id) is None
                    and model_id not in state.suspect_models
                ):
                    self._placement[model_id] = state.node_id
            elif (
                self._registry.get(model_id) is not None
                and model_id not in state.suspect_models
                and not current_is_healthy
            ):
                self._placement[model_id] = state.node_id

        for model_id, model in loading.items():
            if not (
                model.deployment_digest
                and model.generation_id
                and model.operation_id
            ):
                continue
            registry_model = self._registry.get(model_id)
            if registry_model is None:
                continue
            if model.deployment_digest != deployment_digest(registry_model.to_dict()):
                state.suspect_models.add(model_id)
                continue
            existing = state.pending_loads.get(model_id)
            if (
                existing is not None
                and existing.operation_id == model.operation_id
            ):
                # Conserver l'identité de l'objet : les comparaisons `is`
                # d'un appelant qui finalise cette même opération restent vraies.
                existing.progress = model.progress
                existing.acknowledged = True
                continue
            state.pending_loads[model_id] = _PendingLoad(
                model_id=model_id,
                deployment_digest=model.deployment_digest,
                generation_id=model.generation_id,
                operation_id=model.operation_id,
                vram_gb=registry_model.vram_gb,
                progress=model.progress,
                deadline_seconds=model.deadline_seconds,
                # Rapportée par l'agent lui-même : elle existe de son côté.
                acknowledged=True,
            )

        # Le caller tient `operation_lock` : aucune réservation encore suivie
        # ici n'a de chargement en cours côté orchestrateur (annulé ou
        # incertain). READY → l'entrée `loaded` fait foi ; absent → l'agent ne
        # la produira plus. Seul un POST jamais acquitté reste couvert par la
        # grâce, il peut encore être en vol vers l'agent.
        now = time.monotonic()
        for model_id, pending in list(state.pending_loads.items()):
            if model_id in loading:
                continue
            if (
                model_id in ready
                or pending.acknowledged
                or now >= pending.created_at + self._operation_grace_seconds
            ):
                state.pending_loads.pop(model_id, None)

        # Un status READY du control-plane ne prouve pas que le port data-plane
        # est routable (pare-feu, route, bind). Conserver ces suspects ; un
        # modèle absent peut en revanche être rechargé sans ambiguïté. Une
        # entrée disparue mais encore pinnée reste suspecte jusqu'à son unpin.
        state.suspect_models.intersection_update(set(ready) | retained)

    def _forget_vanished_locked(
        self, state: _NodeState, model_id: str, source: str
    ) -> bool:
        """
        Retire un modèle que le nœud ne rapporte plus. Retourne False si
        l'entrée est conservée.

        Une entrée encore pinnée n'est jamais jetée : les handles en vol
        pointent vers CET objet. La jeter puis réadopter le modèle créerait un
        `_LoadedInfo` neuf à zéro requête active, évictable sous un stream en
        cours. Elle perd sa route primaire, devient suspecte, et sera soit
        réadoptée en place (même entrée), soit retirée au prochain inventaire
        une fois les requêtes terminées.
        """
        info = state.loaded.get(model_id)
        if info is None:
            return True
        if self._placement.get(model_id) == state.node_id:
            self._placement.pop(model_id, None)
        if info.active_requests > 0:
            state.suspect_models.add(model_id)
            log.warning(
                "%s : '%s' a disparu de '%s' pendant %d requête(s) active(s) — "
                "entrée conservée (suspecte) jusqu'à la fin des requêtes.",
                source,
                model_id,
                state.node_id,
                info.active_requests,
            )
            return False
        state.loaded.pop(model_id, None)
        log.warning("%s : '%s' a disparu de '%s'.", source, model_id, state.node_id)
        return True

    @staticmethod
    def _reconciled_llama_url(state: _NodeState, port: int | None) -> str | None:
        """
        Reconstruit une URL llama-server de confiance (http://<hôte réel>:<port>)
        pour une entrée réconciliée. L'hôte provient TOUJOURS du base_url du
        nœud (source de confiance, nodes.yaml) — jamais d'une valeur renvoyée
        par l'agent — cohérent avec RemoteNodeClient._trusted_llama_url.
        """
        if not isinstance(port, int) or not (1 <= port <= 65535):
            log.warning(
                "Réconciliation : port invalide (%r) sur '%s' — modèle ignoré.",
                port, state.node_id,
            )
            return None
        base = state.client.base_url
        hostname = urlparse(base).hostname if base else None
        # LocalNodeAdapter expose base_url="in-process" (pas d'hôte) : dans ce
        # cas on retombe sur le format host:port utilisé par le backend local.
        if not hostname:
            hostname = state.node_id
        url_host = f"[{hostname}]" if ":" in hostname else hostname
        return f"http://{url_host}:{port}"

    async def shutdown(self, *, unload_nodes: bool = False) -> None:
        """
        Arrête le heartbeat et ferme les clients.

        Par défaut les llama-server distants restent chauds : un redémarrage
        gracieux de l'orchestrateur ne doit pas vider tout le cluster. Le mode
        destructif reste disponible explicitement pour les tests/opérations.
        """
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        if unload_nodes:
            try:
                await self.unload_all_models()
            except RuntimeError as exc:
                log.warning("Shutdown : unload destructif incomplet : %s", exc)

        for node in self._nodes.values():
            try:
                await node.client.close()
            except Exception as exc:
                log.warning("Shutdown : échec close sur '%s' : %s", node.node_id, exc)

    async def unload_all_models(self) -> None:
        """
        Décharge explicitement le cluster sans fermer les clients ni le monitor.

        Cette API est destinée à l'action admin destructive. Elle refuse une
        purge partielle si une requête est encore active.
        """
        async with self._lock:
            if self._unloading_all:
                raise RuntimeError("Un déchargement global est déjà en cours.")
            active = [
                f"{model_id}@{state.node_id}"
                for state in self._nodes.values()
                for model_id, info in state.loaded.items()
                if info.active_requests > 0
            ]
            if active:
                raise RuntimeError(
                    "Impossible de tout décharger : requêtes actives sur "
                    + ", ".join(active)
                )
            self._unloading_all = True

        try:
            results = await asyncio.gather(
                *(self._unload_node_all(state) for state in self._nodes.values())
            )
        finally:
            async with self._lock:
                self._unloading_all = False
        failures = [result for result in results if result is not None]
        if failures:
            raise RuntimeError(
                "Unload-all incomplet — " + "; ".join(failures)
            )

    async def _unload_node_all(self, state: _NodeState) -> str | None:
        async with state.operation_lock:
            try:
                await state.client.unload_all()
            except Exception as exc:
                log.warning(
                    "Unload-all : échec sur '%s' : %s — inventaire conservé.",
                    state.node_id,
                    exc,
                )
                async with self._lock:
                    self._record_failure_locked(state, exc)
                return f"{state.node_id}: {exc}"

            try:
                health = await state.client.health()
            except Exception:
                health = None

            async with self._lock:
                if health is None and state.last_health is not None:
                    old = state.last_health
                    # Ne retrancher que ce que le dernier health comptait
                    # (même règle que _account_load_locked / _do_unload).
                    counted = {
                        model_id: info.vram_gb
                        for model_id, info in state.loaded.items()
                        if model_id in old.loaded_model_ids
                    }
                    tracked_vram = sum(counted.values())
                    state.last_health = old.model_copy(
                        update={
                            "used_vram_gb": max(0.0, old.used_vram_gb - tracked_vram),
                            "available_vram_gb": old.available_vram_gb + tracked_vram,
                            "loaded_model_ids": [
                                model_id
                                for model_id in old.loaded_model_ids
                                if model_id not in counted
                            ],
                            "free_ports": old.free_ports + len(counted),
                        }
                    )
                elif health is not None:
                    state.last_health = health
                for model_id in list(state.loaded):
                    if self._placement.get(model_id) == state.node_id:
                        self._placement.pop(model_id, None)
                state.loaded.clear()
                state.suspect_models.clear()
                # Les réservations (orphelines : operation_lock est tenu) sont
                # CONSERVÉES. L'unload-all de l'agent ne décharge que les
                # managers présents à son appel, après avoir attendu le verrou
                # de modèle d'un load en cours ; une opération encore en
                # validation n'a pas de manager et lui survit, puis chargera.
                # Les vider sous-compterait VRAM et port jusqu'à ce que l'agent
                # publie ce modèle. Le heartbeat suivant déclenche status() pour
                # toute réservation non publiée et purge celles qui ont disparu.
                state.inventory_revision += 1
            return None

    # ── Point d'entrée principal (proxy.py) ───────────────────────────────────

    def block_bootstrap_admission(self, model_id: str) -> None:
        """Ferme l'admission synchroniquement, avant tout await de rollback."""
        self._bootstrap_blocked.add(model_id)

    def unblock_bootstrap_admission(self, model_id: str) -> None:
        self._bootstrap_blocked.discard(model_id)

    def is_model_loaded(self, model_id: str) -> bool:
        return model_id in self._placement

    def _admitted_model(self, model_id: str) -> ModelDefinition:
        if model_id in self._bootstrap_blocked:
            raise RuntimeError(
                f"Le modèle '{model_id}' est fermé par une transition administrative."
            )
        model = self._registry.get(model_id)
        if model is None:
            raise LookupError(
                f"Modèle inconnu : '{model_id}'. Consultez GET /admin/models."
            )
        if not model.enabled:
            raise PermissionError(
                f"Le modèle '{model_id}' est désactivé dans le registre. "
                f"Activez-le via PATCH /admin/models/{model_id}."
            )
        return model

    def _identity_for_model_locked(
        self, model_id: str, digest: str
    ) -> tuple[str, str]:
        """Retourne (digest, génération) stable pour une définition donnée."""
        current = self._desired_identities.get(model_id)
        if current is None or current[0] != digest:
            current = (digest, uuid4().hex)
            self._desired_identities[model_id] = current
        return current

    async def ensure_model_loaded(self, model_id: str) -> ClusterModelHandle:
        """
        Garantit qu'un modèle est chargé quelque part dans le cluster.

        1. Valide que le modèle est dans le registre et activé.
        2. Fast path si déjà chargé.
        3. Sous locks courts : placement via scheduler et mutations d'état.
           Les load/unload réseau restent hors du lock global.
        4. Retourne un ClusterModelHandle compatible proxy.py.
        """
        self._admitted_model(model_id)

        model_lock = self._model_locks.setdefault(model_id, asyncio.Lock())
        async with model_lock:
            # Le registre ou le gate bootstrap peuvent changer pendant l'attente
            # du lock. Ce recheck ferme la course rollback/admission.
            model = self._admitted_model(model_id)
            model_digest = deployment_digest(model.to_dict())
            refresh_state: _NodeState | None = None
            pending_to_resume: tuple[_NodeState, _PendingLoad] | None = None
            async with self._lock:
                self._identity_for_model_locked(model.id, model_digest)
                if self._unloading_all:
                    raise RuntimeError("Déchargement global en cours, réessayer.")
                pending_node = next(
                    (
                        state
                        for state in self._nodes.values()
                        if model_id in state.pending_loads
                    ),
                    None,
                )
                pending = (
                    pending_node.pending_loads.get(model_id)
                    if pending_node is not None
                    else None
                )
                if pending is not None:
                    if pending.deployment_digest != model_digest:
                        raise ClusterLoadConflictError(
                            f"Le modèle '{model_id}' possède déjà une opération "
                            "de chargement pour une autre définition."
                        )
                    if pending_node is not None:
                        pending_to_resume = (pending_node, pending)
                node_id = self._placement.get(model_id)
                node_state = self._nodes.get(node_id) if node_id else None
                info = node_state.loaded.get(model_id) if node_state else None
                if pending_to_resume is None and info and node_state:
                    if info.unload_uncertain:
                        # L'unload précédent n'est toujours pas résolu. Ne pas
                        # router vers une URL potentiellement disparue et ne
                        # pas créer une copie sur un autre nœud avant que le
                        # prochain heartbeat/retry confirme la disparition.
                        raise ClusterUnloadUncertainError(
                            model_id, node_state.node_id
                        )
                    if (
                        info.deployment_digest
                        and info.deployment_digest != model_digest
                    ):
                        # L'ancienne génération reste comptée/évictable sur le
                        # nœud, mais elle ne peut jamais être servie pour la
                        # nouvelle définition.
                        if self._placement.get(model_id) == node_state.node_id:
                            self._placement.pop(model_id, None)
                        node_state.suspect_models.add(model_id)
                        node_state.inventory_revision += 1
                        node_state = None
                        info = None
                    if info is None:
                        pass
                    elif not node_state.online or model_id in node_state.suspect_models:
                        # On retire uniquement la route primaire : l'inventaire
                        # reste suivi pour compter/faire évincer la VRAM si le
                        # nœud revient avec une copie ancienne ou dupliquée.
                        if self._placement.get(model_id) == node_state.node_id:
                            self._placement.pop(model_id, None)
                        log.warning(
                            "Placement de '%s' invalidé sur '%s' — replacement.",
                            model_id,
                            node_state.node_id,
                        )
                    elif info.evicting:
                        # L'éviction de ce modèle est décidée (unload en cours
                        # pour placer un autre modèle sur ce nœud) : elle ne
                        # prend pas le lock de CE modèle, ce chemin est donc
                        # atteignable. Refuser ce handle instable et replacer
                        # reste plus sûr qu'un 502 en plein stream.
                        self._placement.pop(model_id, None)
                    elif not info.internal_api_key:
                        refresh_state = node_state
                    else:
                        info.touch()
                        self._admitted_model(model_id)
                        return ClusterModelHandle(info, model, self)

            if pending_to_resume is not None:
                pending_node, pending = pending_to_resume
                try:
                    async with pending_node.operation_lock:
                        response = await self._poll_load_operation(
                            pending_node, pending, None
                        )
                except ClusterLoadAbandonedError as exc:
                    # Le nœud ne peut plus produire ce résultat : sans purge, le
                    # modèle resterait inutilisable jusqu'au redémarrage de la
                    # gateway, même avec un autre nœud sain (P0-2).
                    log.warning(
                        "Reprise du chargement de '%s' : %s Réservation purgée, "
                        "nouveau placement.",
                        model_id,
                        exc,
                    )
                    async with self._lock:
                        # Préférer un autre nœud sain au replacement ; ce nœud
                        # reste un repli si aucune alternative n'a la capacité.
                        pending_node.suspect_models.add(model_id)
                    response = None
                if response is not None and response.state == "ready":
                    try:
                        return await self._finish_pending_load(
                            pending_node, model, pending, response
                        )
                    except BaseException:
                        await self._clear_pending_load(pending_node, pending)
                        raise
                await self._clear_pending_load(pending_node, pending)

            refreshed_nodes: set[str] = set()
            if refresh_state is not None:
                refreshed_nodes.add(refresh_state.node_id)
                refreshed = await self._refresh_reconciled(refresh_state, model)
                if refreshed is not None:
                    self._admitted_model(model_id)
                    return refreshed

            self._raise_if_recent_load_failure(model.id, model_digest)

            reused = await self._reuse_existing_copy(
                model, model_digest, exclude=refreshed_nodes
            )
            if reused is not None:
                self._admitted_model(model_id)
                return reused

            handle = await self._place_and_load(model)
            self._admitted_model(model_id)
            return handle

    def _recent_failure_nodes(self, model_id: str, digest: str) -> set[str]:
        """Nœuds écartés pour cette définition (échec récent propre au modèle)."""
        now = time.monotonic()
        excluded: set[str] = set()
        for key, (cached_digest, expires_at) in list(self._load_failures.items()):
            if key[0] != model_id:
                continue
            if cached_digest != digest or now >= expires_at:
                self._load_failures.pop(key, None)
                continue
            excluded.add(key[1])
        return excluded

    def _remember_load_failure(
        self, model_id: str, node_id: str, digest: str
    ) -> None:
        self._load_failures[(model_id, node_id)] = (
            digest,
            time.monotonic() + self._load_failure_ttl_seconds,
        )

    def forget_load_failures(self, model_id: str) -> None:
        """
        Oublie les échecs récents d'un modèle (chargement admin explicite).

        Un opérateur qui vient de corriger l'artefact sur le nœud et demande
        `POST /admin/models/{id}/load` ne doit pas recevoir « échec récent ».
        """
        for key in [key for key in self._load_failures if key[0] == model_id]:
            self._load_failures.pop(key, None)

    def _raise_if_recent_load_failure(self, model_id: str, digest: str) -> None:
        """Échoue vite si TOUS les nœuds online ont récemment échoué ce modèle."""
        excluded = self._recent_failure_nodes(model_id, digest)
        if not excluded:
            return
        online = {state.node_id for state in self._nodes.values() if state.online}
        if not online or not online <= excluded:
            return
        raise RuntimeError(
            f"Le modèle '{model_id}' n'a pas pu être chargé sur les nœuds "
            "disponibles (échec récent, détail dans les logs). Réessayez dans "
            "quelques secondes."
        )

    async def _reuse_existing_copy(
        self,
        model: ModelDefinition,
        model_digest: str,
        *,
        exclude: set[str],
    ) -> ClusterModelHandle | None:
        """
        Réutilise une copie déjà chargée de la même définition avant de passer
        par le scheduler.

        Après un échec data-plane, le placement est retiré mais la copie reste
        sur le nœud. Le scheduler exigerait `vram_gb` libre EN PLUS de cette
        copie : 503 si elle est pinnée, sinon évictions et rechargement à froid
        pour un modèle qui tourne déjà. Le load idempotent (`already_loaded`
        côté agent) ne demande aucune VRAM supplémentaire. Une alternative saine
        capable d'accueillir le modèle reste prioritaire sur un nœud suspect.
        """
        async with self._lock:
            holders: list[_NodeState] = []
            for state in self._nodes.values():
                info = state.loaded.get(model.id)
                if (
                    info is None
                    or state.node_id in exclude
                    or not state.online
                    or state.last_health is None
                    or model.id not in state.last_health.loaded_model_ids
                    or model.id in state.pending_loads
                    or info.evicting
                    or info.unload_uncertain
                    or (
                        info.deployment_digest
                        and info.deployment_digest != model_digest
                    )
                ):
                    continue
                holders.append(state)
            healthy = [s for s in holders if model.id not in s.suspect_models]
            suspect = [s for s in holders if model.id in s.suspect_models]
            if suspect and not healthy:
                suspects = {
                    state.node_id
                    for state in self._nodes.values()
                    if model.id in state.suspect_models
                }
                try:
                    self._pick_locked(model, suspects)
                except NoPlacementError:
                    pass
                else:
                    # Une alternative saine a la capacité : le replacement
                    # normal la préfère au nœud qui vient d'échouer.
                    suspect = []
            candidates = healthy + suspect

        for state in candidates:
            handle = await self._refresh_reconciled(state, model)
            if handle is not None:
                return handle
        return None

    @staticmethod
    def _load_deadline_seconds(model: ModelDefinition, client: NodeClient) -> float:
        """Calcule le budget négocié à partir de la définition du modèle."""
        model_timeout = getattr(model, "load_timeout_seconds", None)
        if model_timeout is None:
            model_timeout = getattr(client, "_load_timeout", 300.0)
        # Marge pour la création du process et le dernier health probe, bornée
        # par le maximum accepté par LoadRequest.
        return min(86_400.0, max(1.0, float(model_timeout) + 10.0))

    @staticmethod
    def _validate_load_identity(
        response: LoadResponse, pending: _PendingLoad
    ) -> LoadResponse:
        values = (
            response.model_id,
            response.deployment_digest,
            response.generation_id,
            response.operation_id,
        )
        expected = (
            pending.model_id,
            pending.deployment_digest,
            pending.generation_id,
            pending.operation_id,
        )
        # Les réponses legacy ne passent jamais par le polling moderne. Une
        # réponse moderne partielle est donc une erreur de protocole, jamais un
        # succès à rattacher à la génération courante.
        if values != expected:
            raise NodeProtocolError(
                "Réponse load ne correspondant pas à l'opération demandée"
            )
        return response

    async def _poll_load_operation(
        self,
        node_state: _NodeState,
        pending: _PendingLoad,
        initial: LoadResponse | None,
    ) -> LoadResponse:
        """
        Résout le résultat d'un load accepté, sans changer d'opération.

        Trois issues : un état terminal de l'agent ; `ClusterLoadUncertainError`
        (réservation conservée, aucun failover — l'agent peut encore aboutir) ;
        `ClusterLoadAbandonedError` (réservation purgeable — il ne le peut
        plus). L'incertitude est bornée : au-delà de
        `created_at + deadline_seconds + _operation_grace_seconds`, l'échéance
        appliquée par l'agent est forcément échue. Sans cette borne, une
        réservation orpheline survivait jusqu'au redémarrage de la gateway.
        """
        response = initial
        if response is not None:
            self._validate_load_identity(response, pending)
            pending.acknowledged = True
            if response.state in {
                "ready", "failed", "conflict", "deadline_exceeded"
            }:
                return response

        budget = pending.deadline_seconds or 300.0
        expires_at = pending.created_at + budget + self._operation_grace_seconds
        # Un POST incertain peut encore être en cours de traitement côté agent
        # (attente de son verrou de modèle) : un 404 n'est définitif qu'après
        # la grâce, ou dès que l'agent a prouvé connaître l'opération.
        unknown_grace_until = min(
            pending.created_at + self._operation_grace_seconds, expires_at
        )

        def abandon(reason: str) -> ClusterLoadAbandonedError:
            return ClusterLoadAbandonedError(
                pending.model_id, node_state.node_id, pending.operation_id, reason
            )

        if time.monotonic() >= expires_at:
            raise abandon("échéance négociée et délai de grâce dépassés")

        getter = getattr(node_state.client, "get_operation_status", None)
        if getter is None:
            raise ClusterLoadUncertainError(
                pending.model_id, node_state.node_id, pending.operation_id
            )

        deadline = min(time.monotonic() + budget, expires_at)
        while True:
            now = time.monotonic()
            if now >= expires_at:
                raise abandon("échéance négociée et délai de grâce dépassés")
            if not node_state.online:
                # Même politique que pour un modèle READY sur un nœud offline :
                # replacement. Une copie revenue plus tard est réconciliée comme
                # doublon comptabilisé et évictable.
                raise abandon("nœud offline")
            remaining = deadline - now
            if remaining <= 0:
                raise ClusterLoadUncertainError(
                    pending.model_id, node_state.node_id, pending.operation_id
                )
            try:
                response = await asyncio.wait_for(
                    getter(pending.operation_id), timeout=min(remaining, 1.0)
                )
            except asyncio.CancelledError:
                # L'opération distante continue ; pending_loads conserve la
                # réservation et interdit tout failover concurrent.
                raise
            except (NodeUnreachableError, asyncio.TimeoutError):
                # Une réponse tardive/une coupure ne prouve pas l'échec. On
                # retente le même operation_id jusqu'à son échéancier.
                await asyncio.sleep(min(0.2, max(0.0, remaining)))
                continue
            except NodeProtocolError as exc:
                if exc.status_code == 404:
                    if (
                        pending.acknowledged
                        or time.monotonic() >= unknown_grace_until
                    ):
                        raise abandon("opération inconnue du nœud") from exc
                    await asyncio.sleep(min(0.2, max(0.0, remaining)))
                    continue
                raise ClusterLoadUncertainError(
                    pending.model_id, node_state.node_id, pending.operation_id
                ) from exc

            self._validate_load_identity(response, pending)
            pending.acknowledged = True
            async with self._lock:
                current = node_state.pending_loads.get(pending.model_id)
                if current is pending:
                    current.progress = response.progress
            if response.state in {
                "ready", "failed", "conflict", "deadline_exceeded"
            }:
                return response
            await asyncio.sleep(min(0.2, max(0.0, remaining)))

    async def _start_load_operation(
        self,
        node_state: _NodeState,
        model: ModelDefinition,
        pending: _PendingLoad,
    ) -> LoadResponse:
        """Démarre l'opération `pending` sur le nœud et en résout l'issue."""
        try:
            initial = await node_state.client.start_load_model(
                model.to_dict(),
                deployment_digest=pending.deployment_digest,
                generation_id=pending.generation_id,
                operation_id=pending.operation_id,
                deadline_seconds=pending.deadline_seconds or 300.0,
            )
        except asyncio.CancelledError:
            raise
        except NodeProtocolError as exc:
            if exc.status_code == 409:
                # Conflit d'identité (autre génération active) ou unload en
                # cours côté agent. Le corps de l'agent reste au journal :
                # ce message remonte jusqu'au client.
                log.warning(
                    "Chargement de '%s' refusé par '%s' (409) : %s",
                    model.id, node_state.node_id, exc,
                )
                raise ClusterLoadConflictError(
                    f"Le chargement du modèle '{model.id}' est refusé par le "
                    "nœud : conflit de génération ou déchargement en cours. "
                    "Réessayez dans quelques secondes."
                ) from exc
            if exc.is_rejection:
                # Refus explicite (404, 422, 401…) : l'agent n'a rien
                # enregistré, un polling ne pourrait que répondre 404 et
                # bloquer le modèle (P0-2). Échec franc → failover.
                raise
            # 5xx, 408/429, JSON invalide : le POST a peut-être été traité.
            return await self._poll_load_operation(node_state, pending, None)
        except NodeUnreachableError:
            # Le POST a peut-être été reçu. Résoudre avec le même identifiant
            # avant de considérer un échec ou un failover.
            return await self._poll_load_operation(node_state, pending, None)
        return await self._poll_load_operation(node_state, pending, initial)

    async def _clear_pending_load(
        self, node_state: _NodeState, pending: _PendingLoad
    ) -> None:
        async with self._lock:
            if node_state.pending_loads.get(pending.model_id) is pending:
                node_state.pending_loads.pop(pending.model_id, None)
                node_state.inventory_revision += 1

    async def _finish_pending_load(
        self,
        node_state: _NodeState,
        model: ModelDefinition,
        pending: _PendingLoad,
        response: LoadResponse,
        *,
        load_seconds: float | None = None,
    ) -> ClusterModelHandle:
        self._validate_load_identity(response, pending)
        if response.state != "ready" or not response.llama_url:
            raise NodeProtocolError(
                f"Réponse READY incomplète pour '{model.id}'"
            )
        async with self._lock:
            if self._unloading_all:
                raise RuntimeError(
                    "Déchargement global en cours, chargement annulé."
                )
            if node_state.pending_loads.get(model.id) is pending:
                node_state.pending_loads.pop(model.id, None)
            info = node_state.loaded.get(model.id)
            if info is None:
                info = _LoadedInfo(
                    node_id=node_state.node_id,
                    llama_url=response.llama_url,
                    internal_api_key=response.internal_api_key or "",
                    vram_gb=model.vram_gb,
                )
                node_state.loaded[model.id] = info
            else:
                # Replacement sur le nœud qui détient déjà l'entrée (suspect,
                # réconcilié, réponse `already_loaded`) : mise à jour EN PLACE.
                # Les handles distribués avant pinent cet objet ; un objet neuf
                # repartirait à zéro requête active et deviendrait évictable
                # sous un stream en cours.
                info.llama_url = response.llama_url
                info.internal_api_key = (
                    response.internal_api_key or info.internal_api_key
                )
                info.vram_gb = model.vram_gb
                info.unload_uncertain = False
            if not response.already_loaded:
                info.last_load_seconds = load_seconds
            info.deployment_digest = pending.deployment_digest
            info.generation_id = pending.generation_id
            info.operation_id = pending.operation_id
            info.touch()
            node_state.suspect_models.discard(model.id)
            self._placement[model.id] = node_state.node_id
            self._load_failures.pop((model.id, node_state.node_id), None)
            self._account_load_locked(
                node_state, model, already_loaded=response.already_loaded
            )
            node_state.inventory_revision += 1
        log.info(
            "Modèle '%s' chargé sur nœud '%s' (%.1f GB VRAM, url=%s)",
            model.id,
            node_state.node_id,
            model.vram_gb,
            response.llama_url,
        )
        return ClusterModelHandle(info, model, self)

    async def _place_and_load(self, model: ModelDefinition) -> ClusterModelHandle:
        """Placement + chargement avec failover, sans I/O sous le lock global."""
        failed_nodes: set[str] = set()
        failures: list[str] = []
        suspect_exclusions: set[str] = set()
        model_digest = deployment_digest(model.to_dict())

        async with self._lock:
            _, generation_id = self._identity_for_model_locked(model.id, model_digest)
            # Nœuds ayant récemment échoué CETTE définition : écartés pour
            # toute la durée du placement (jamais réintégrés comme suspects).
            recently_failed = self._recent_failure_nodes(model.id, model_digest)
            failed_nodes.update(recently_failed)
            suspects = {
                state.node_id
                for state in self._nodes.values()
                if model.id in state.suspect_models
            } - recently_failed
            healthy_alternative = any(
                state.online and state.node_id not in suspects
                for state in self._nodes.values()
            )
            if healthy_alternative:
                suspect_exclusions.update(suspects)
                failed_nodes.update(suspect_exclusions)

        attempts = 0
        while True:
            attempts += 1
            if attempts > self._max_placement_attempts:
                log.error(
                    "Placement de '%s' abandonné après %d tentatives (plans "
                    "obsolètes répétés) : %s",
                    model.id,
                    attempts - 1,
                    "; ".join(failures) or "aucun échec de chargement",
                )
                raise RuntimeError(
                    f"Le placement du modèle '{model.id}' n'a pas convergé ; "
                    "réessayez dans quelques secondes."
                )
            if attempts > 1:
                # Un plan obsolète (`continue`) n'attend rien de suspendant :
                # sans ce point de suspension, la boucle monopoliserait
                # l'event loop tant que l'état qui l'invalide ne change pas.
                await asyncio.sleep(0)
            async with self._lock:
                try:
                    chosen_snapshot, _ = self._pick_locked(model, failed_nodes)
                except NoPlacementError as exc:
                    if suspect_exclusions:
                        # Les nœuds sains ont la priorité, mais un suspect ne
                        # doit pas être banni si les alternatives n'ont pas la
                        # capacité. Le load idempotent sert alors de tentative
                        # explicite de récupération.
                        failed_nodes.difference_update(suspect_exclusions)
                        suspect_exclusions.clear()
                        continue
                    self._raise_placement_exhausted(
                        model, exc, failures, recently_failed
                    )
                chosen_state = self._nodes[chosen_snapshot.node_id]

            # Ne jamais attendre le lock d'un nœud en conservant le lock global.
            async with chosen_state.operation_lock:
                evictions: list[tuple[str, _LoadedInfo]] = []
                async with self._lock:
                    # La capacité peut avoir changé pendant l'attente : recalcul
                    # atomique. Si le best-fit a changé, on libère ce nœud et
                    # recommence plutôt que d'appliquer un plan obsolète.
                    try:
                        current_choice, eviction_plan = self._pick_locked(
                            model, failed_nodes
                        )
                    except NoPlacementError as exc:
                        if suspect_exclusions:
                            failed_nodes.difference_update(suspect_exclusions)
                            suspect_exclusions.clear()
                            continue
                        self._raise_placement_exhausted(
                            model, exc, failures, recently_failed
                        )
                    if current_choice.node_id != chosen_state.node_id:
                        continue

                    victims = list(eviction_plan.models_to_evict)
                    if (
                        self._stale_generation_locked(
                            chosen_state, model.id, model_digest
                        )
                        is not None
                        and model.id not in victims
                    ):
                        # L'agent refuse (409) une nouvelle génération tant que
                        # l'ancienne occupe l'identifiant : elle part d'abord.
                        # `_pick_locked` a déjà compté sa VRAM comme libérable.
                        victims.insert(0, model.id)
                    stale_plan = False
                    for victim_id in victims:
                        info = chosen_state.loaded.get(victim_id)
                        if info is None or info.active_requests > 0 or info.evicting:
                            stale_plan = True
                            break
                        info.evicting = True
                        evictions.append((victim_id, info))
                    if stale_plan:
                        for _, info in evictions:
                            info.evicting = False
                        continue

                eviction_failed = False
                try:
                    for victim_id, info in evictions:
                        if not await self._do_unload(chosen_state, victim_id, info):
                            eviction_failed = True
                            break
                finally:
                    # Échec, annulation ou succès : aucune entrée de la série ne
                    # garde `evicting`. Une victime non déchargée resterait sinon
                    # ni servie ni évictable (fast-path et plans la refusent).
                    # Affectations synchrones : sûres même sous annulation.
                    for _, info in evictions:
                        info.evicting = False
                if eviction_failed:
                    failed_nodes.add(chosen_state.node_id)
                    failures.append(
                        f"{chosen_state.node_id}: éviction non confirmée"
                    )
                    continue

                pending = _PendingLoad(
                    model_id=model.id,
                    deployment_digest=model_digest,
                    generation_id=generation_id,
                    operation_id=uuid4().hex,
                    vram_gb=model.vram_gb,
                    deadline_seconds=self._load_deadline_seconds(
                        model, chosen_state.client
                    ),
                )
                async with self._lock:
                    chosen_state.pending_loads[model.id] = pending
                    chosen_state.inventory_revision += 1

                load_started_at = time.monotonic()
                load_outcome = "success"
                try:
                    resp = await self._start_load_operation(
                        chosen_state, model, pending
                    )
                    self._validate_load_identity(resp, pending)
                    if resp.state == "conflict":
                        raise ClusterLoadConflictError(
                            resp.message
                            or f"Génération incompatible pour '{model.id}'"
                        )
                    if resp.state in {"failed", "deadline_exceeded"}:
                        raise ClusterModelLoadFailedError(
                            model.id, chosen_state.node_id, resp
                        )
                except asyncio.CancelledError:
                    load_outcome = "cancelled"
                    # Le pending reste volontairement en place : le nœud peut
                    # encore terminer le chargement après la déconnexion du
                    # demandeur. L'inventaire le purgera s'il devient READY ou
                    # disparaît.
                    raise
                except ClusterLoadUncertainError:
                    load_outcome = "uncertain"
                    raise
                except ClusterLoadConflictError:
                    load_outcome = "conflict"
                    await self._clear_pending_load(chosen_state, pending)
                    raise
                except Exception as exc:
                    # Inclut ClusterLoadAbandonedError, les refus 4xx et les
                    # échecs rapportés : l'agent ne produira pas ce résultat,
                    # la réservation est purgée.
                    load_outcome = "error"
                    await self._clear_pending_load(chosen_state, pending)
                    if self._counts_as_node_failure(exc):
                        async with self._lock:
                            self._record_failure_locked(chosen_state, exc)
                    if (
                        isinstance(exc, ClusterModelLoadFailedError)
                        and exc.is_model_failure
                    ):
                        async with self._lock:
                            self._remember_load_failure(
                                model.id, chosen_state.node_id, model_digest
                            )
                    failed_nodes.add(chosen_state.node_id)
                    failures.append(f"{chosen_state.node_id}: {exc}")
                    log.warning(
                        "Chargement de '%s' échoué sur '%s' (%s) — failover.",
                        model.id,
                        chosen_state.node_id,
                        exc,
                    )
                    continue
                finally:
                    load_seconds = max(0.0, time.monotonic() - load_started_at)
                    MODEL_LOAD_SECONDS.observe(
                        load_seconds,
                        model=model.id,
                        node=chosen_state.node_id,
                        outcome=load_outcome,
                    )

                try:
                    return await self._finish_pending_load(
                        chosen_state,
                        model,
                        pending,
                        resp,
                        load_seconds=load_seconds,
                    )
                except BaseException:
                    # Sans cette purge, une réservation READY refusée (unload
                    # global concurrent, réponse incomplète) survivait jusqu'au
                    # prochain inventaire et faussait la capacité.
                    await self._clear_pending_load(chosen_state, pending)
                    raise

    def _raise_placement_exhausted(
        self,
        model: ModelDefinition,
        exc: NoPlacementError,
        failures: list[str],
        recently_failed: set[str],
    ) -> NoReturn:
        """Lève l'erreur client finale d'un placement sans candidat restant."""
        if recently_failed:
            # Les nœuds écartés auraient été tentés sans le cache : leur échec
            # fait partie de la réponse, même si les autres sont seulement pleins.
            failures = [
                *failures,
                *(f"{node_id}: échec récent (cache)" for node_id in sorted(recently_failed)),
            ]
        if failures:
            # Le détail des échecs embarque les corps HTTP renvoyés par les
            # node-agents (stderr llama-server, chemins de modèles) : journal
            # serveur uniquement, jamais dans le message client (SEC — fuite
            # d'infra, audit 2026-08-28).
            log.error(
                "Chargement de '%s' impossible sur tous les nœuds candidats : %s",
                model.id,
                "; ".join(failures),
            )
            raise RuntimeError(
                f"Le modèle '{model.id}' n'a pas pu être chargé sur les nœuds "
                "disponibles (détail dans les logs)."
            ) from exc
        if isinstance(exc, _StaleGenerationBlocked):
            raise ClusterLoadConflictError(
                f"Une génération précédente du modèle '{model.id}' traite encore "
                "des requêtes ; la nouvelle définition sera chargée après leur "
                "fin. Réessayez dans quelques secondes."
            ) from exc
        raise RuntimeError(str(exc)) from exc

    @staticmethod
    def _stale_generation_locked(
        state: _NodeState, model_id: str, digest: str
    ) -> _LoadedInfo | None:
        """Entrée de ce modèle servant une AUTRE définition sur ce nœud."""
        info = state.loaded.get(model_id)
        if info is None or not info.deployment_digest:
            return None
        return info if info.deployment_digest != digest else None

    def _pick_locked(
        self, model: ModelDefinition, excluded_nodes: set[str]
    ) -> tuple[NodeSnapshot, EvictionPlan]:
        if self._unloading_all:
            raise RuntimeError("Déchargement global en cours, réessayer.")
        digest = deployment_digest(model.to_dict())
        snapshots: list[NodeSnapshot] = []
        blocked: list[str] = []
        for state in self._nodes.values():
            if state.node_id in excluded_nodes:
                continue
            snapshot = state.snapshot()
            stale = self._stale_generation_locked(state, model.id, digest)
            if stale is not None:
                if stale.active_requests > 0 or stale.evicting:
                    # L'agent refuserait la nouvelle génération (409).
                    blocked.append(state.node_id)
                    continue
                # Son éviction est obligatoire avant le load : sa VRAM et son
                # port sont donc disponibles pour ce placement-ci.
                health = state.last_health
                counted = health is not None and model.id in health.loaded_model_ids
                snapshot = replace(
                    snapshot,
                    reported_available_vram_gb=(
                        snapshot.available_vram_gb
                        + (stale.vram_gb if counted else 0.0)
                    ),
                    free_ports=snapshot.free_ports + (1 if counted else 0),
                    loaded_models=tuple(
                        loaded
                        for loaded in snapshot.loaded_models
                        if loaded.id != model.id
                    ),
                )
            snapshots.append(snapshot)
        try:
            return pick_node(
                ModelToPlace(id=model.id, vram_gb=model.vram_gb), snapshots
            )
        except NoPlacementError as exc:
            if blocked:
                raise _StaleGenerationBlocked(str(exc)) from exc
            raise

    def _account_load_locked(
        self,
        node_state: _NodeState,
        model: ModelDefinition,
        *,
        already_loaded: bool,
    ) -> None:
        """
        Applique localement un load confirmé en attendant le prochain health.

        L'agent compte un modèle dès LOADING (VRAM, port, `loaded_model_ids`) :
        s'il figure déjà dans le dernier health, celui-ci l'inclut et toute
        déduction supplémentaire le compterait deux fois jusqu'au heartbeat.
        """
        if node_state.last_health is None:
            return
        health = node_state.last_health
        if model.id in health.loaded_model_ids:
            return
        loaded_ids = [*health.loaded_model_ids, model.id]
        used_delta = 0.0 if already_loaded else model.vram_gb
        port_delta = 0 if already_loaded else 1
        node_state.last_health = health.model_copy(
            update={
                "used_vram_gb": health.used_vram_gb + used_delta,
                "available_vram_gb": max(
                    0.0, health.available_vram_gb - used_delta
                ),
                "loaded_model_ids": loaded_ids,
                "free_ports": max(0, health.free_ports - port_delta),
            }
        )

    async def _refresh_reconciled(
        self, node_state: _NodeState, model: ModelDefinition
    ) -> ClusterModelHandle | None:
        """
        Rafraîchit une entrée réconciliée (internal_api_key vide) sur SON nœud.
        load_model est idempotent côté agent (already_loaded)
        donc le modèle n'est PAS réellement rechargé — on récupère juste la vraie
        clé interne + l'URL de confiance. En cas d'échec, on invalide l'entrée et
        on retombe sur un placement frais sur un autre nœud.
        """
        model_digest = deployment_digest(model.to_dict())
        async with self._lock:
            _, generation_id = self._identity_for_model_locked(model.id, model_digest)
        pending = _PendingLoad(
            model_id=model.id,
            deployment_digest=model_digest,
            generation_id=generation_id,
            operation_id=uuid4().hex,
            vram_gb=model.vram_gb,
            deadline_seconds=self._load_deadline_seconds(model, node_state.client),
        )
        async with node_state.operation_lock:
            try:
                async with self._lock:
                    node_state.pending_loads[model.id] = pending
                    node_state.inventory_revision += 1
                resp = await self._start_load_operation(node_state, model, pending)
                self._validate_load_identity(resp, pending)
                if resp.state in {"failed", "deadline_exceeded"}:
                    raise ClusterModelLoadFailedError(
                        model.id, node_state.node_id, resp
                    )
                if resp.state != "ready" or not resp.llama_url:
                    raise NodeProtocolError(
                        resp.message or f"Rafraîchissement de '{model.id}' incomplet"
                    )
            except asyncio.CancelledError:
                raise
            except ClusterLoadUncertainError:
                raise
            except Exception as exc:
                await self._clear_pending_load(node_state, pending)
                async with self._lock:
                    if self._counts_as_node_failure(exc):
                        self._record_failure_locked(node_state, exc)
                    if self._placement.get(model.id) == node_state.node_id:
                        self._placement.pop(model.id, None)
                log.warning(
                    "Rafraîchissement de '%s' sur '%s' échoué (%s) — replacement.",
                    model.id, node_state.node_id, exc,
                )
                return None

            await self._clear_pending_load(node_state, pending)
            async with self._lock:
                if self._unloading_all:
                    raise RuntimeError(
                        "Déchargement global en cours, rafraîchissement annulé."
                    )
                info = node_state.loaded.get(model.id)
                if info is None:
                    info = _LoadedInfo(
                        node_id=node_state.node_id,
                        llama_url=resp.llama_url,
                        internal_api_key=resp.internal_api_key or "",
                        vram_gb=model.vram_gb,
                    )
                    node_state.loaded[model.id] = info
                else:
                    # En place : les pins des handles déjà distribués survivent.
                    info.llama_url = resp.llama_url
                    info.internal_api_key = (
                        resp.internal_api_key or info.internal_api_key
                    )
                    # READY confirmé par l'agent pour cette entrée : même
                    # règle que _finish_pending_load.
                    info.unload_uncertain = False
                info.deployment_digest = model_digest
                info.generation_id = generation_id
                info.operation_id = pending.operation_id
                node_state.suspect_models.discard(model.id)
                self._placement[model.id] = node_state.node_id
                self._load_failures.pop((model.id, node_state.node_id), None)
                # No-op si le health courant compte déjà ce modèle ; couvre un
                # agent qui a dû le recharger réellement.
                self._account_load_locked(
                    node_state, model, already_loaded=resp.already_loaded
                )
                node_state.inventory_revision += 1
                info.touch()
                return ClusterModelHandle(info, model, self)

    # ── Déchargement ──────────────────────────────────────────────────────────

    async def unload_model(self, model_id: str) -> None:
        """
        Force le déchargement d'un modèle (action admin) sur TOUS les nœuds.

        Toute copie suivie compte : la route primaire, mais aussi un doublon
        revenu d'un nœud offline ou une ancienne génération retirée du
        placement après un changement de définition. Ne regarder que
        `_placement` laissait ces copies en VRAM pendant que la route admin
        annonçait « VRAM libérée ». Refus global (aucun unload partiel) si une
        copie traite encore des requêtes. Les nœuds offline sont sautés : leur
        copie éventuelle est réconciliée comme doublon évictable à leur retour.
        """
        model_lock = self._model_locks.setdefault(model_id, asyncio.Lock())
        async with model_lock:
            async with self._lock:
                targets: list[_NodeState] = []
                for state in self._nodes.values():
                    if model_id not in state.loaded:
                        continue
                    if not state.online:
                        log.warning(
                            "Unload admin de '%s' : nœud '%s' offline, copie "
                            "non déchargée (réconciliée à son retour).",
                            model_id,
                            state.node_id,
                        )
                        continue
                    targets.append(state)
                placement_node = self._placement.get(model_id)
                if placement_node is not None and not any(
                    state.node_id == placement_node for state in targets
                ):
                    placement_state = self._nodes.get(placement_node)
                    if placement_state is None or model_id not in placement_state.loaded:
                        self._placement.pop(model_id, None)
                active = sum(
                    state.loaded[model_id].active_requests for state in targets
                )
                if active > 0:
                    raise RuntimeError(
                        f"Le modèle '{model_id}' traite encore {active} "
                        "requête(s) et ne peut pas être déchargé."
                    )

            uncertain: ClusterUnloadUncertainError | None = None
            for node_state in targets:
                async with node_state.operation_lock:
                    async with self._lock:
                        info = node_state.loaded.get(model_id)
                        if info is None:
                            continue
                        if info.active_requests > 0:
                            raise RuntimeError(
                                f"Le modèle '{model_id}' traite encore "
                                f"{info.active_requests} requête(s) et ne peut "
                                "pas être déchargé."
                            )
                        info.evicting = True
                    # `_do_unload` remet `evicting` à False sur toute issue,
                    # annulation comprise (finally).
                    confirmed = await self._do_unload(node_state, model_id, info)
                if not confirmed and uncertain is None:
                    uncertain = ClusterUnloadUncertainError(
                        model_id, node_state.node_id
                    )
            if uncertain is not None:
                raise uncertain

    async def _do_unload(
        self,
        node_state: _NodeState,
        model_id: str,
        expected_info: _LoadedInfo,
    ) -> bool:
        """
        Décharge un modèle, sans lock global pendant l'I/O.

        Le caller sérialise avec `node_state.operation_lock`. Retourne True
        uniquement si la disparition est confirmée par unload ou health.

        Propriétaire du drapeau `evicting` posé par le caller : il est remis à
        False sur TOUTE issue, y compris une annulation pendant l'unload ou la
        re-synchronisation `health()`. Sans cela, l'entrée restait ni servie
        (fast-path) ni évictable (plans obsolètes), indéfiniment.
        """
        try:
            return await self._do_unload_io(node_state, model_id, expected_info)
        finally:
            # Affectation synchrone : sûre même sous annulation.
            expected_info.evicting = False

    async def _do_unload_io(
        self,
        node_state: _NodeState,
        model_id: str,
        expected_info: _LoadedInfo,
    ) -> bool:
        try:
            resp = await node_state.client.unload_model(model_id)
        except (NodeUnreachableError, NodeProtocolError) as exc:
            # Nœud flaky : l'unload a peut-être échoué → le llama-server tourne
            # peut-être encore et occupe toujours sa VRAM. On NE purge PAS l'état
            # de façon optimiste (cela sur-réserverait au prochain placement).
            # Choix le plus sûr : re-synchroniser immédiatement via health() et
            # ne libérer localement QUE si le nœud confirme que le modèle est bien
            # parti. Sinon on laisse l'entrée en place (VRAM considérée occupée).
            log.warning(
                "Impossible de décharger '%s' de '%s' : %s — re-synchronisation health().",
                model_id, node_state.node_id, exc,
            )
            return await self._resync_after_failed_unload(
                node_state, model_id, expected_info
            )
        except asyncio.CancelledError:
            async with self._lock:
                expected_info.evicting = False
            raise
        except Exception as exc:
            log.warning(
                "Impossible de décharger '%s' de '%s' : erreur inattendue %s.",
                model_id,
                node_state.node_id,
                exc,
            )
            async with self._lock:
                expected_info.evicting = False
                if node_state.loaded.get(model_id) is expected_info:
                    expected_info.unload_uncertain = True
                self._record_failure_locked(node_state, exc)
            return False

        if resp.model_id != model_id:
            log.warning(
                "Déchargement incohérent sur '%s' : demandé '%s', reçu '%s' — "
                "placement conservé.",
                node_state.node_id,
                model_id,
                resp.model_id,
            )
            async with self._lock:
                expected_info.evicting = False
                if node_state.loaded.get(model_id) is expected_info:
                    expected_info.unload_uncertain = True
            return False

        if not resp.unloaded:
            # `unloaded=False` signifie que l'agent n'a pas confirmé la
            # disparition. Une re-synchronisation peut encore établir que le
            # modèle est parti entre-temps ; sinon l'état reste conservé.
            log.warning(
                "Déchargement de '%s' sur '%s' non confirmé par l'agent "
                "(re-synchronisation health()).",
                model_id,
                node_state.node_id,
            )
            return await self._resync_after_failed_unload(
                node_state, model_id, expected_info
            )

        log.info(
            "Modèle '%s' déchargé de '%s' (libéré %.1f GB VRAM)",
            model_id, node_state.node_id, resp.freed_vram_gb,
        )

        async with self._lock:
            # Ne jamais purger une entrée plus récente créée par une autre
            # réconciliation (comparaison d'identité).
            if node_state.loaded.get(model_id) is expected_info:
                node_state.loaded.pop(model_id, None)
            expected_info.evicting = False
            if self._placement.get(model_id) == node_state.node_id:
                self._placement.pop(model_id, None)
            # Le caller tient operation_lock : une réservation restante est
            # orpheline (load annulé) et l'agent vient de décharger le modèle.
            node_state.pending_loads.pop(model_id, None)

            # Symétrique de _account_load_locked : ne retrancher que ce que le
            # dernier health comptait encore.
            if (
                node_state.last_health is not None
                and model_id in node_state.last_health.loaded_model_ids
            ):
                health = node_state.last_health
                node_state.last_health = health.model_copy(
                    update={
                        "used_vram_gb": max(
                            0.0, health.used_vram_gb - expected_info.vram_gb
                        ),
                        "available_vram_gb": (
                            health.available_vram_gb + expected_info.vram_gb
                        ),
                        "loaded_model_ids": [
                            item
                            for item in health.loaded_model_ids
                            if item != model_id
                        ],
                        "free_ports": health.free_ports + 1,
                    }
                )
            node_state.inventory_revision += 1
        return True

    async def _resync_after_failed_unload(
        self,
        node_state: _NodeState,
        model_id: str,
        expected_info: _LoadedInfo,
    ) -> bool:
        """
        Après un unload_model en échec, re-synchronise l'état local via health().
        On ne purge l'entrée locale QUE si le nœud confirme que le modèle n'est
        plus chargé (absent de loaded_model_ids). Sinon on considère la VRAM
        toujours occupée : le placement suivant ne sur-réservera pas. Le caller
        tient `operation_lock` ; `_lock` n'est pris que pour les mutations.
        """
        try:
            health = await node_state.client.health()
        except Exception as exc:
            # health() KO aussi : nœud vraisemblablement injoignable. On laisse
            # l'entrée en place (VRAM occupée) ; le heartbeat le passera offline
            # et le failover (fix 2) invalidera le placement à la prochaine requête.
            log.warning(
                "Re-sync '%s' sur '%s' : health() KO (%s) — entrée conservée (VRAM occupée).",
                model_id, node_state.node_id, exc,
            )
            async with self._lock:
                expected_info.evicting = False
                if node_state.loaded.get(model_id) is expected_info:
                    expected_info.unload_uncertain = True
                self._record_failure_locked(node_state, exc)
            return False

        async with self._lock:
            node_state.last_health = health
            expected_info.evicting = False
            if model_id not in health.loaded_model_ids:
                # Le nœud confirme que le modèle est parti : purge sûre.
                if node_state.loaded.get(model_id) is expected_info:
                    node_state.loaded.pop(model_id, None)
                if self._placement.get(model_id) == node_state.node_id:
                    self._placement.pop(model_id, None)
                node_state.pending_loads.pop(model_id, None)
                node_state.inventory_revision += 1
                log.info(
                    "Re-sync '%s' sur '%s' : modèle absent côté nœud — état local purgé.",
                    model_id, node_state.node_id,
                )
                return True
            if node_state.loaded.get(model_id) is expected_info:
                expected_info.unload_uncertain = True

        log.warning(
            "Re-sync '%s' sur '%s' : modèle TOUJOURS chargé côté nœud — "
            "entrée conservée (VRAM occupée).",
            model_id, node_state.node_id,
        )
        return False

    # ── Heartbeat ─────────────────────────────────────────────────────────────

    async def _health_loop(self) -> None:
        while True:
            await asyncio.sleep(self._health_interval)
            await self._check_all_nodes()

    async def _check_all_nodes(self) -> None:
        results = await asyncio.gather(
            *[self._check_node(state) for state in self._nodes.values()],
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                log.debug("Exception non gérée dans _check_node : %s", r)

    async def _check_node(self, state: _NodeState) -> None:
        # Révision capturée AVANT le round-trip : une mutation locale pendant
        # l'attente (load terminé, unload, invalidation) rend cette réponse
        # antérieure à l'état courant.
        async with self._lock:
            revision = state.inventory_revision
        try:
            health = await state.client.health()
        except NodeUnreachableError as exc:
            async with self._lock:
                self._record_failure_locked(state, exc)
            return
        except NodeProtocolError as exc:
            log.warning("Nœud '%s' heartbeat KO (protocol) : %s", state.node_id, exc)
            async with self._lock:
                # Une réponse malformée/5xx persistante est aussi une panne :
                # elle doit appliquer exactement le même seuil qu'un timeout.
                self._record_failure_locked(state, exc)
            return
        except Exception as exc:
            # Toute autre exception (bug client, erreur inattendue) doit COMPTER
            # comme un échec de heartbeat — sinon un nœud réellement KO resterait
            # online=True indéfiniment (l'exception était auparavant absorbée en
            # debug par _check_all_nodes).
            log.warning(
                "Nœud '%s' heartbeat : exception inattendue (%s) : %s",
                state.node_id, exc.__class__.__name__, exc,
            )
            async with self._lock:
                self._record_failure_locked(state, exc)
            return

        # Succès
        needs_status = False
        async with self._lock:
            was_offline = not state.online
            previous_failures = state.consecutive_failures
            state.online = True
            state.consecutive_failures = 0
            stale = revision != state.inventory_revision
            if not stale or state.last_health is None:
                state.last_health = health

            # Une réponse périmée n'est jamais appliquée à l'inventaire : elle
            # retirerait un modèle chargé pendant le round-trip, puis le
            # heartbeat suivant le réadopterait sous un `_LoadedInfo` neuf à zéro
            # requête active. Idem pendant un load/unload sérialisé. Le
            # prochain heartbeat fera la convergence.
            if was_offline:
                needs_status = True
            elif not stale and not state.operation_lock.locked():
                reported = set(health.loaded_model_ids)
                for model_id in set(state.loaded) - reported:
                    self._forget_vanished_locked(state, model_id, "Heartbeat")
                    state.inventory_revision += 1
                needs_status = bool(
                    (reported - set(state.loaded))
                    or (state.suspect_models & reported)
                    # Seul status() porte la génération qui lève un
                    # `unload_uncertain` resté READY côté nœud.
                    or any(
                        info.unload_uncertain
                        for model_id, info in state.loaded.items()
                        if model_id in reported
                    )
                    # operation_lock libre : toute réservation restante est
                    # orpheline (load annulé). Si l'agent ne la publie pas, ou
                    # si sa grâce est échue, seul status() peut établir son
                    # issue (READY, encore en validation, ou terminée en
                    # `failed` — jamais visible dans loaded_model_ids) et la
                    # purger ; sinon elle réservait VRAM et port à vie.
                    or any(
                        model_id not in reported
                        or time.monotonic()
                        >= pending.created_at + self._operation_grace_seconds
                        for model_id, pending in state.pending_loads.items()
                    )
                )

        if was_offline:
            log.info(
                "Nœud '%s' revient ONLINE (était offline depuis %d échecs).",
                state.node_id,
                previous_failures,
            )
        if needs_status:
            await self._reconcile_node_status(state)

    @staticmethod
    def _counts_as_node_failure(exc: Exception) -> bool:
        """
        Un refus explicite (4xx définitif, conflit) prouve que l'agent répond :
        le compter vers le seuil offline sortirait du placement un nœud sain
        pour une définition qu'il refuse, et ses autres modèles avec lui.
        """
        if isinstance(exc, (ClusterLoadConflictError, ClusterModelLoadFailedError)):
            # `failed`/`deadline_exceeded` rapporté par un agent joignable :
            # un GGUF absent sur un nœud ne doit pas sortir tout le cluster du
            # placement en trois requêtes.
            return False
        return not (isinstance(exc, NodeProtocolError) and exc.is_rejection)

    def _record_failure_locked(self, state: _NodeState, exc: Exception) -> None:
        """Comptabilise uniformément toute panne de plan de contrôle."""
        state.consecutive_failures += 1
        if state.online and state.consecutive_failures >= self._failures_threshold:
            state.online = False
            log.warning(
                "Nœud '%s' marqué OFFLINE après %d échecs consécutifs (%s). "
                "Les modèles chargés sur ce nœud sont indisponibles.",
                state.node_id,
                state.consecutive_failures,
                exc,
            )

    async def _report_backend_failure(self, info: _LoadedInfo) -> None:
        """Invalide prudemment un placement data-plane encore courant."""
        async with self._lock:
            state = self._nodes.get(info.node_id)
            if state is None:
                return
            model_id = self._model_id_for_info(state, info)
            if model_id is None or state.loaded.get(model_id) is not info:
                return
            if self._placement.get(model_id) != state.node_id:
                return
            self._placement.pop(model_id, None)
            state.suspect_models.add(model_id)
            state.inventory_revision += 1
            log.warning(
                "Data-plane : placement de '%s' sur '%s' invalidé immédiatement.",
                model_id,
                state.node_id,
            )

    @staticmethod
    def _model_id_for_info(
        state: _NodeState, expected_info: _LoadedInfo
    ) -> str | None:
        return next(
            (
                model_id
                for model_id, info in state.loaded.items()
                if info is expected_info
            ),
            None,
        )

    # ── Statut (admin) ────────────────────────────────────────────────────────

    def cluster_status(self) -> list[dict]:
        """Retourne l'état de chaque nœud pour GET /admin/cluster."""
        result = []
        for node_id, state in self._nodes.items():
            h = state.last_health
            result.append({
                "node_id": node_id,
                "base_url": state.client.base_url,
                "online": state.online,
                "consecutive_failures": state.consecutive_failures,
                "total_vram_gb": h.total_vram_gb if h else None,
                "used_vram_gb": h.used_vram_gb if h else None,
                "available_vram_gb": h.available_vram_gb if h else None,
                "free_ports": h.free_ports if h else None,
                "gpu_measurement": (
                    h.gpu_measurement.model_dump(mode="json")
                    if h and h.gpu_measurement is not None
                    else None
                ),
                "loaded_models": [
                    {
                        "model_id": mid,
                        "llama_url": info.llama_url,
                        "vram_gb": info.vram_gb,
                        "state": (
                            "unload_uncertain"
                            if info.unload_uncertain
                            else "ready"
                        ),
                        "deployment_digest": info.deployment_digest,
                        "generation_id": info.generation_id,
                        "operation_id": info.operation_id,
                        "progress": 1.0,
                        "idle_seconds": round(info.idle_seconds, 1),
                        "active_requests": info.active_requests,
                    }
                    for mid, info in state.loaded.items()
                ],
            })
        return result

    async def collect_llama_metrics(self) -> dict:
        """
        Agrège les métriques llama-server de tous les nœuds ONLINE (observabilité).

        Additif et hors chemin d'inférence : peuple /admin/metrics/llama et
        l'exposition Prometheus en mode cluster. Retourne un dict compact
        {model_id: {clés métriques…, "node": node_id}} ; le node_id est propagé
        pour tagger la provenance et éviter les collisions de model_id entre
        nœuds. Best-effort : un nœud injoignable est simplement ignoré, jamais
        d'exception propagée (le heartbeat reste seul responsable de l'état).
        """
        # Snapshot des nœuds online sous lock, puis appels réseau HORS lock pour
        # ne jamais bloquer le placement/heartbeat sur des I/O métriques.
        async with self._lock:
            online = [
                (state.node_id, state.client)
                for state in self._nodes.values()
                if state.online
            ]

        result: dict = {}
        # Nœuds interrogés en parallèle : un nœud lent ne retarde plus les
        # autres de son timeout de health.
        responses = await asyncio.gather(
            *(client.metrics() for _, client in online),
            return_exceptions=True,
        )
        for (node_id, _client), node_metrics in zip(online, responses):
            if isinstance(node_metrics, (NodeUnreachableError, NodeProtocolError)):
                log.debug(
                    "Métriques nœud '%s' indisponibles : %s", node_id, node_metrics
                )
                continue
            if isinstance(node_metrics, asyncio.CancelledError):
                raise node_metrics
            if isinstance(node_metrics, BaseException):
                # Défensif : jamais fatal pour l'observabilité.
                log.debug(
                    "Métriques nœud '%s' : erreur inattendue (%s)",
                    node_id,
                    node_metrics,
                )
                continue
            if not isinstance(node_metrics, dict):
                continue
            for model_id, m in node_metrics.items():
                entry = dict(m) if isinstance(m, dict) else {}
                entry["node"] = node_id
                result[model_id] = entry
        return result

    def status(self) -> dict:
        """
        Retourne l'état agrégé pour /admin/status — même format que LocalModelManager.
        """
        # Seuls les nœuds ONLINE contribuent à la readiness/capacité.
        #
        # Le budget NET (allouable aux modèles) est used + available annoncé par
        # les agents, donc déjà après leur overhead et leur marge — jamais la
        # VRAM physique brute. On expose en plus le total physique et la réserve
        # agrégée pour tenir la même arithmétique qu'en local :
        #   total_gb - overhead_gb == budget_net_gb
        online = [
            s.last_health for s in self._nodes.values()
            if s.online and s.last_health
        ]
        used_gb = sum(h.used_vram_gb for h in online)
        available_gb = sum(h.available_vram_gb for h in online)
        budget_net_gb = used_gb + available_gb
        physical_gb = sum(h.total_vram_gb for h in online)
        # Réserve agrégée des nœuds (overhead + marge de chacun), en GB. La
        # `safety_margin` locale est un ratio mono-hôte : aucun sens agrégé ici,
        # elle reste donc absente du statut cluster.
        overhead_gb = max(0.0, physical_gb - budget_net_gb)

        models_status = []
        for model in self._registry.list_all():
            node_id = self._placement.get(model.id)
            if node_id and node_id in self._nodes:
                state = self._nodes[node_id]
                info = (
                    state.loaded.get(model.id)
                    if state.online and model.id not in state.suspect_models
                    else None
                )
            else:
                info = None
                node_id = None

            if info:
                entry = {
                    "id": model.id,
                    "description": model.description,
                    "enabled": model.enabled,
                    "vram_gb": model.vram_gb,
                    "capabilities": model.capabilities,
                    "state": (
                        "unload_uncertain"
                        if info.unload_uncertain
                        else "ready"
                    ),
                    "deployment_digest": info.deployment_digest,
                    "generation_id": info.generation_id,
                    "operation_id": info.operation_id,
                    "progress": 1.0,
                    "path": str(model.path),
                    "node": node_id,
                    "llama_url": info.llama_url,
                    "idle_seconds": round(info.idle_seconds, 1),
                    "active_requests": info.active_requests,
                    "last_load_seconds": (
                        round(info.last_load_seconds, 2)
                        if info.last_load_seconds is not None
                        else None
                    ),
                    "pid": None,
                    "port": None,
                    "uptime_seconds": None,
                    "llama_params": None,
                }
            else:
                entry = {
                    "id": model.id,
                    "description": model.description,
                    "enabled": model.enabled,
                    "vram_gb": model.vram_gb,
                    "capabilities": model.capabilities,
                    "state": "unloaded",
                    "deployment_digest": "",
                    "generation_id": "",
                    "operation_id": None,
                    "progress": 0.0,
                    "path": str(model.path),
                    "node": None,
                    "llama_url": None,
                    "idle_seconds": None,
                    "active_requests": 0,
                    "last_load_seconds": None,
                    "pid": None,
                    "port": None,
                    "uptime_seconds": None,
                    "llama_params": None,
                }
            models_status.append(entry)

        return {
            "vram_budget": {
                "total_gb": round(physical_gb, 2),
                "overhead_gb": round(overhead_gb, 2),
                "used_gb": round(used_gb, 2),
                "available_gb": round(max(0.0, available_gb), 2),
                "budget_net_gb": round(budget_net_gb, 2),
                "nodes": len(self._nodes),
                "nodes_online": sum(1 for s in self._nodes.values() if s.online),
            },
            "models": models_status,
        }

    @property
    def registry(self) -> ModelRegistry:
        return self._registry
