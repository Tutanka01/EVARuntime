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
  asyncio.Lock global sur les mutations de l'état du cluster (chargement,
  éviction, mise-à-jour heartbeat). Le heartbeat tourne dans une tâche
  background ; les requêtes d'inférence ne prennent jamais le lock — seul
  le cluster manager le prend lors du placement.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from model_registry import ModelDefinition, ModelRegistry

from .node_client import NodeClient, NodeUnreachableError, NodeProtocolError
from .node_protocol import NodeHealth
from .scheduler import (
    LoadedModelSnapshot,
    ModelToPlace,
    NodeSnapshot,
    NoPlacementError,
    build_eviction_plan,
    pick_node,
)

log = logging.getLogger(__name__)


# ── État interne par modèle chargé sur un nœud ───────────────────────────────

@dataclass
class _LoadedInfo:
    node_id: str
    llama_url: str
    internal_api_key: str
    vram_gb: float
    _last_request: float = field(default_factory=time.monotonic)
    _active_requests: int = 0

    def touch(self) -> None:
        self._last_request = time.monotonic()

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_request

    @property
    def active_requests(self) -> int:
        return self._active_requests


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

    def snapshot(self, health_failures_threshold: int) -> NodeSnapshot:
        """Construit un NodeSnapshot à partir de l'état courant."""
        if self.last_health is None:
            return NodeSnapshot(
                node_id=self.node_id,
                online=False,
                total_vram_gb=0.0,
                used_vram_gb=0.0,
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
        return NodeSnapshot(
            node_id=self.node_id,
            online=self.online,
            draining=False,
            total_vram_gb=h.total_vram_gb,
            used_vram_gb=h.used_vram_gb,
            free_ports=h.free_ports,
            loaded_models=loaded_snapshots,
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

    def __init__(self, info: _LoadedInfo, model_def: ModelDefinition) -> None:
        self._info = info
        self.model = model_def

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


# ── ClusterManager ─────────────────────────────────────────────────────────────

class ClusterManager:
    """
    Singleton multi-nœuds — remplace LocalModelManager quand CLUSTER_MODE=cluster.
    """

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

        self._monitor_task: asyncio.Task | None = None

    # ── Lifecycle ──────────────────────────────────────────────────────────────

    async def start_health_monitor(self) -> None:
        """Lance la tâche heartbeat. Appelé depuis le lifespan FastAPI."""
        # Premier health-check immédiat pour détecter les nœuds offline au boot
        await self._check_all_nodes()
        self._monitor_task = asyncio.create_task(self._health_loop())
        log.info(
            "ClusterManager démarré — %d nœud(s) : %s",
            len(self._nodes),
            ", ".join(
                f"{nid}({'online' if s.online else 'OFFLINE'})"
                for nid, s in self._nodes.items()
            ),
        )

    async def shutdown(self) -> None:
        """Arrête le heartbeat et décharge tous les modèles sur tous les nœuds."""
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass

        for node in self._nodes.values():
            try:
                await node.client.unload_all()
            except Exception as exc:
                log.warning("Shutdown : échec unload_all sur '%s' : %s", node.node_id, exc)
            finally:
                await node.client.close()

    # ── Point d'entrée principal (proxy.py) ───────────────────────────────────

    async def ensure_model_loaded(self, model_id: str) -> ClusterModelHandle:
        """
        Garantit qu'un modèle est chargé quelque part dans le cluster.

        1. Valide que le modèle est dans le registre et activé.
        2. Fast path si déjà chargé.
        3. Sous lock : placement via scheduler, éviction si nécessaire, chargement.
        4. Retourne un ClusterModelHandle compatible proxy.py.
        """
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

        # Fast path — pas de lock si déjà chargé
        async with self._lock:
            node_id = self._placement.get(model_id)
            if node_id and node_id in self._nodes:
                node_state = self._nodes[node_id]
                info = node_state.loaded.get(model_id)
                if info:
                    info.touch()
                    return ClusterModelHandle(info, model)

            # Placement fresh
            return await self._place_and_load(model)

    async def _place_and_load(self, model: ModelDefinition) -> ClusterModelHandle:
        """Placement + chargement — DOIT être appelé sous self._lock."""
        nodes_snapshot = [
            state.snapshot(self._failures_threshold)
            for state in self._nodes.values()
        ]

        model_to_place = ModelToPlace(id=model.id, vram_gb=model.vram_gb)
        try:
            chosen_snapshot, eviction_plan = pick_node(model_to_place, nodes_snapshot)
        except NoPlacementError as exc:
            raise RuntimeError(str(exc)) from exc

        chosen_state = self._nodes[chosen_snapshot.node_id]

        # Appliquer le plan d'éviction
        for mid_to_evict in eviction_plan.models_to_evict:
            await self._do_unload(chosen_state, mid_to_evict)

        # Charger le modèle sur le nœud choisi
        try:
            resp = await chosen_state.client.load_model(model.to_dict())
        except (NodeUnreachableError, NodeProtocolError) as exc:
            # Marquer le nœud comme suspicieux mais ne pas le passer offline ici
            # — le heartbeat s'en chargera. Remonter l'erreur au caller.
            raise RuntimeError(
                f"Échec du chargement de '{model.id}' sur '{chosen_state.node_id}' : {exc}"
            ) from exc

        info = _LoadedInfo(
            node_id=chosen_state.node_id,
            llama_url=resp.llama_url,
            internal_api_key=resp.internal_api_key,
            vram_gb=model.vram_gb,
        )
        chosen_state.loaded[model.id] = info
        self._placement[model.id] = chosen_state.node_id

        # Mettre à jour used_vram dans le NodeHealth local pour que le prochain
        # snapshot soit correct AVANT le prochain heartbeat.
        if chosen_state.last_health is not None:
            h = chosen_state.last_health
            chosen_state.last_health = h.model_copy(
                update={
                    "used_vram_gb": h.used_vram_gb + model.vram_gb,
                    "available_vram_gb": max(0.0, h.available_vram_gb - model.vram_gb),
                    "loaded_model_ids": h.loaded_model_ids + [model.id],
                    "free_ports": max(0, h.free_ports - 1),
                }
            )

        log.info(
            "Modèle '%s' chargé sur nœud '%s' (%.1f GB VRAM, url=%s)",
            model.id, chosen_state.node_id, model.vram_gb, resp.llama_url,
        )
        return ClusterModelHandle(info, model)

    # ── Déchargement ──────────────────────────────────────────────────────────

    async def unload_model(self, model_id: str) -> None:
        """Force le déchargement d'un modèle (action admin)."""
        async with self._lock:
            node_id = self._placement.get(model_id)
            if node_id is None:
                return  # Déjà déchargé
            node_state = self._nodes.get(node_id)
            if node_state is None:
                return
            await self._do_unload(node_state, model_id)

    async def _do_unload(self, node_state: _NodeState, model_id: str) -> None:
        """Décharge un modèle sur un nœud précis. Appelé sous _lock."""
        try:
            resp = await node_state.client.unload_model(model_id)
            log.info(
                "Modèle '%s' déchargé de '%s' (libéré %.1f GB VRAM)",
                model_id, node_state.node_id, resp.freed_vram_gb,
            )
        except (NodeUnreachableError, NodeProtocolError) as exc:
            log.warning(
                "Impossible de décharger '%s' de '%s' : %s",
                model_id, node_state.node_id, exc,
            )

        # Nettoyer l'état local même si l'appel réseau a échoué — on ne veut
        # pas bloquer l'éviction à cause d'un nœud flaky.
        info = node_state.loaded.pop(model_id, None)
        self._placement.pop(model_id, None)

        if info and node_state.last_health is not None:
            h = node_state.last_health
            node_state.last_health = h.model_copy(
                update={
                    "used_vram_gb": max(0.0, h.used_vram_gb - info.vram_gb),
                    "available_vram_gb": h.available_vram_gb + info.vram_gb,
                    "loaded_model_ids": [m for m in h.loaded_model_ids if m != model_id],
                    "free_ports": h.free_ports + 1,
                }
            )

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
        try:
            health = await state.client.health()
        except NodeUnreachableError as exc:
            state.consecutive_failures += 1
            if state.online and state.consecutive_failures >= self._failures_threshold:
                state.online = False
                log.warning(
                    "Nœud '%s' marqué OFFLINE après %d échecs consécutifs (%s). "
                    "Les modèles chargés sur ce nœud sont indisponibles.",
                    state.node_id, state.consecutive_failures, exc,
                )
            return
        except NodeProtocolError as exc:
            log.warning("Nœud '%s' heartbeat KO (protocol) : %s", state.node_id, exc)
            state.consecutive_failures += 1
            return

        # Succès
        if not state.online:
            log.info(
                "Nœud '%s' revient ONLINE (était offline depuis %d échecs).",
                state.node_id, state.consecutive_failures,
            )
        state.online = True
        state.consecutive_failures = 0
        state.last_health = health

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
                "loaded_models": [
                    {
                        "model_id": mid,
                        "llama_url": info.llama_url,
                        "vram_gb": info.vram_gb,
                        "idle_seconds": round(info.idle_seconds, 1),
                        "active_requests": info.active_requests,
                    }
                    for mid, info in state.loaded.items()
                ],
            })
        return result

    def status(self) -> dict:
        """
        Retourne l'état agrégé pour /admin/status — même format que LocalModelManager.
        """
        # Reconstruction du VRAM global (agrégé sur tous les nœuds)
        total_gb = sum(
            s.last_health.total_vram_gb for s in self._nodes.values()
            if s.last_health
        )
        used_gb = sum(
            s.last_health.used_vram_gb for s in self._nodes.values()
            if s.last_health
        )

        models_status = []
        for model in self._registry.list_all():
            node_id = self._placement.get(model.id)
            if node_id and node_id in self._nodes:
                state = self._nodes[node_id]
                info = state.loaded.get(model.id)
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
                    "state": "ready",
                    "path": str(model.path),
                    "node": node_id,
                    "llama_url": info.llama_url,
                    "idle_seconds": round(info.idle_seconds, 1),
                    "active_requests": info.active_requests,
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
                    "path": str(model.path),
                    "node": None,
                    "llama_url": None,
                    "idle_seconds": None,
                    "active_requests": 0,
                    "pid": None,
                    "port": None,
                    "uptime_seconds": None,
                    "llama_params": None,
                }
            models_status.append(entry)

        return {
            "vram_budget": {
                "total_gb": round(total_gb, 2),
                "used_gb": round(used_gb, 2),
                "available_gb": round(max(0.0, total_gb - used_gb), 2),
                "nodes": len(self._nodes),
                "nodes_online": sum(1 for s in self._nodes.values() if s.online),
            },
            "models": models_status,
        }

    @property
    def registry(self) -> ModelRegistry:
        return self._registry
