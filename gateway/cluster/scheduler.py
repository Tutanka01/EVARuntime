"""
Scheduler — logique pure de placement des modèles sur les nœuds.

Aucun I/O, aucune dépendance asyncio, aucune mutation d'état externe.
Toutes les fonctions sont déterministes et facilement testables.

Le ClusterManager appelle ces fonctions pour décider :
  - Quel nœud doit accueillir un nouveau modèle (pick_node).
  - Quels modèles évincer sur le nœud retenu si la VRAM manque
    (build_eviction_plan).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LoadedModelSnapshot:
    """État d'un modèle chargé sur un nœud, vu par le scheduler."""
    id: str
    vram_gb: float
    # Plus l'idle_seconds est grand, plus le modèle est candidat à l'éviction LRU.
    # active_requests > 0 → modèle pinné (non évincible).
    idle_seconds: float = 0.0
    active_requests: int = 0

    @property
    def is_evictable(self) -> bool:
        return self.active_requests == 0


@dataclass(frozen=True)
class NodeSnapshot:
    """Vue figée d'un nœud à un instant T, fournie en entrée du scheduler."""
    node_id: str
    online: bool
    draining: bool = False
    total_vram_gb: float = 0.0
    used_vram_gb: float = 0.0
    free_ports: int = 0
    loaded_models: tuple[LoadedModelSnapshot, ...] = field(default_factory=tuple)

    @property
    def available_vram_gb(self) -> float:
        return max(0.0, self.total_vram_gb - self.used_vram_gb)

    @property
    def is_candidate(self) -> bool:
        """Eligible pour accueillir un nouveau modèle."""
        return self.online and not self.draining


@dataclass(frozen=True)
class ModelToPlace:
    """Demande de placement."""
    id: str
    vram_gb: float
    # Si non None, le modèle doit aller exactement sur ce nœud (pin).
    pin_to_node: str | None = None


@dataclass(frozen=True)
class EvictionPlan:
    """Plan d'éviction calculé pour un nœud précis."""
    node_id: str
    models_to_evict: tuple[str, ...]
    freed_vram_gb: float


class NoPlacementError(RuntimeError):
    """Aucun placement n'est possible — message explicatif côté `args`."""


# ── Calculs utilitaires ───────────────────────────────────────────────────────

def max_potential_vram(node: NodeSnapshot) -> float:
    """
    VRAM maximale qu'on peut récupérer sur ce nœud APRÈS éviction de tous les
    modèles évincibles (non pinnés). Utilisé pour savoir si un nœud serait
    capable d'accueillir un modèle après éviction maximale.
    """
    evictable_vram = sum(m.vram_gb for m in node.loaded_models if m.is_evictable)
    return node.available_vram_gb + evictable_vram


def build_eviction_plan(node: NodeSnapshot, vram_needed: float) -> EvictionPlan:
    """
    Calcule la liste minimale de modèles à évincer (par LRU desc) pour libérer
    `vram_needed` GB sur ce nœud. Lève NoPlacementError si même l'éviction
    maximale ne suffit pas.

    Stratégie LRU : on évince d'abord les modèles dont idle_seconds est le plus
    grand (les moins récemment utilisés). Pas de pinned (active_requests > 0).
    """
    if node.available_vram_gb >= vram_needed:
        return EvictionPlan(node_id=node.node_id, models_to_evict=(), freed_vram_gb=0.0)

    if max_potential_vram(node) < vram_needed:
        raise NoPlacementError(
            f"Nœud '{node.node_id}' ne peut libérer que {max_potential_vram(node):.1f} GB "
            f"(besoin {vram_needed:.1f} GB) — modèles pinnés bloquent l'éviction."
        )

    # Trier les évincibles par LRU desc (le plus inactif en premier)
    evictables = sorted(
        (m for m in node.loaded_models if m.is_evictable),
        key=lambda m: m.idle_seconds,
        reverse=True,
    )

    to_evict: list[str] = []
    freed = 0.0
    remaining = vram_needed - node.available_vram_gb

    for model in evictables:
        if freed >= remaining:
            break
        to_evict.append(model.id)
        freed += model.vram_gb

    return EvictionPlan(
        node_id=node.node_id,
        models_to_evict=tuple(to_evict),
        freed_vram_gb=freed,
    )


# ── Sélection du nœud ─────────────────────────────────────────────────────────

def _filter_candidates(model: ModelToPlace, nodes: list[NodeSnapshot]) -> list[NodeSnapshot]:
    """Filtre les nœuds éligibles : online, pas en drain, contrainte de pin respectée."""
    candidates = [n for n in nodes if n.is_candidate]
    if model.pin_to_node is not None:
        candidates = [n for n in candidates if n.node_id == model.pin_to_node]
    return candidates


def pick_node(model: ModelToPlace, nodes: list[NodeSnapshot]) -> tuple[NodeSnapshot, EvictionPlan]:
    """
    Choisit le meilleur nœud pour accueillir `model`.

    Stratégie :
      1. Nœuds avec capacité IMMÉDIATE (VRAM libre + port libre) → best-fit
         (minimise le résidu, optimise le packing).
      2. Sinon : nœuds qui peuvent l'accueillir APRÈS éviction LRU → on choisit
         celui qui doit évincer le MOINS de VRAM.
      3. Sinon : NoPlacementError.

    Retourne (nœud_choisi, plan_d_eviction). Le plan est vide si capacité immédiate.

    Note : la décision est calculée à partir d'un instantané — l'appelant
    (ClusterManager) doit verrouiller son état avant d'appliquer le plan, puis
    re-vérifier que la situation n'a pas changé entre-temps.
    """
    candidates = _filter_candidates(model, nodes)
    if not candidates:
        if model.pin_to_node is not None:
            raise NoPlacementError(
                f"Modèle '{model.id}' épinglé au nœud '{model.pin_to_node}' "
                f"qui est offline ou en drain."
            )
        raise NoPlacementError(
            f"Aucun nœud joignable pour charger '{model.id}'. "
            f"Vérifier /admin/cluster."
        )

    # 1) Capacité immédiate : VRAM libre ≥ besoin ET au moins un port libre
    immediate_fit = [
        n for n in candidates
        if n.available_vram_gb >= model.vram_gb and n.free_ports > 0
    ]
    if immediate_fit:
        # Best-fit : minimise le résidu pour optimiser le packing.
        # Tie-break par node_id pour rester déterministe.
        chosen = min(
            immediate_fit,
            key=lambda n: (n.available_vram_gb - model.vram_gb, n.node_id),
        )
        return chosen, EvictionPlan(node_id=chosen.node_id, models_to_evict=(), freed_vram_gb=0.0)

    # 2) Éviction nécessaire — qui peut le faire et avec quel coût ?
    eviction_candidates: list[tuple[NodeSnapshot, EvictionPlan]] = []
    for node in candidates:
        # Sans port libre on ne peut PAS charger, même avec éviction de VRAM
        # (sauf si l'éviction libère un port — ce qui est le cas : chaque
        # modèle évincé rend son port).
        try:
            plan = build_eviction_plan(node, model.vram_gb)
        except NoPlacementError:
            continue

        # Après le plan d'éviction, le nœud disposera-t-il d'un port libre ?
        # Chaque modèle évincé libère son port → free_ports + len(models_to_evict).
        ports_after = node.free_ports + len(plan.models_to_evict)
        if ports_after < 1:
            continue

        eviction_candidates.append((node, plan))

    if not eviction_candidates:
        raise NoPlacementError(
            f"Aucun nœud ne peut accueillir '{model.id}' (besoin {model.vram_gb:.1f} GB). "
            f"Tous les nœuds candidats sont saturés et leurs modèles pinnés. "
            f"Réessayer plus tard ou décharger manuellement un modèle via "
            f"POST /admin/models/{{id}}/unload."
        )

    # Préférer le nœud qui doit évincer le MOINS de VRAM (moins de churn).
    # Tie-break par node_id pour déterminisme.
    chosen_node, chosen_plan = min(
        eviction_candidates,
        key=lambda np: (np[1].freed_vram_gb, np[0].node_id),
    )
    return chosen_node, chosen_plan
