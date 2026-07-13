"""
Tests unitaires du scheduler — logique pure, aucune I/O.

Couvre :
  - Best-fit immédiat (minimisation du résidu, tie-break déterministe).
  - Refus quand le modèle est trop gros pour TOUS les nœuds.
  - Préférence du nœud qui doit évincer le MOINS de VRAM.
  - Exclusion des nœuds offline / en drain.
  - Respect du pin_to_node.
  - Éviction LRU (idle_seconds desc, modèles pinned ignorés).
  - Cas où l'éviction libère aussi un port (free_ports = 0 au départ).
"""
from __future__ import annotations

import pytest

from cluster.scheduler import (
    EvictionPlan,
    LoadedModelSnapshot,
    ModelToPlace,
    NoPlacementError,
    NodeSnapshot,
    build_eviction_plan,
    max_potential_vram,
    pick_node,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def make_node(
    node_id: str,
    *,
    total: float = 48.0,
    used: float = 0.0,
    available: float | None = None,
    free_ports: int = 5,
    online: bool = True,
    draining: bool = False,
    models: tuple[LoadedModelSnapshot, ...] = (),
) -> NodeSnapshot:
    return NodeSnapshot(
        node_id=node_id,
        online=online,
        draining=draining,
        total_vram_gb=total,
        used_vram_gb=used,
        reported_available_vram_gb=available,
        free_ports=free_ports,
        loaded_models=models,
    )


def make_model(model_id: str, vram_gb: float, *, pin: str | None = None) -> ModelToPlace:
    return ModelToPlace(id=model_id, vram_gb=vram_gb, pin_to_node=pin)


def loaded(
    model_id: str, vram: float, *, idle: float = 0.0, active: int = 0
) -> LoadedModelSnapshot:
    return LoadedModelSnapshot(
        id=model_id, vram_gb=vram, idle_seconds=idle, active_requests=active
    )


# ── Best-fit immédiat ────────────────────────────────────────────────────────

class TestPickNodeImmediateFit:
    def test_single_empty_node_accepts_model(self):
        node = make_node("a", total=48.0, used=0.0)
        chosen, plan = pick_node(make_model("m1", 20.0), [node])

        assert chosen.node_id == "a"
        assert plan.models_to_evict == ()
        assert plan.freed_vram_gb == 0.0

    def test_best_fit_minimizes_residue(self):
        """Le nœud avec le moins de VRAM résiduelle après chargement est préféré."""
        small = make_node("small", total=24.0, used=0.0)   # résidu : 24-20 = 4
        medium = make_node("medium", total=48.0, used=0.0)  # résidu : 48-20 = 28
        large = make_node("large", total=80.0, used=0.0)   # résidu : 80-20 = 60

        chosen, plan = pick_node(make_model("m", 20.0), [large, medium, small])

        assert chosen.node_id == "small"
        assert plan.models_to_evict == ()

    def test_best_fit_tie_break_by_node_id(self):
        """Deux nœuds avec le même résidu : on prend le node_id le plus petit."""
        n1 = make_node("zzz", total=48.0, used=0.0)
        n2 = make_node("aaa", total=48.0, used=0.0)

        chosen, _ = pick_node(make_model("m", 20.0), [n1, n2])
        assert chosen.node_id == "aaa"

    def test_node_without_free_ports_excluded_from_immediate(self):
        """Pas de port libre → pas de fit immédiat même si VRAM dispo."""
        no_port = make_node("a", total=48.0, used=0.0, free_ports=0)
        good = make_node("b", total=48.0, used=0.0, free_ports=2)

        chosen, _ = pick_node(make_model("m", 10.0), [no_port, good])
        assert chosen.node_id == "b"

    def test_advertised_effective_capacity_overrides_physical_subtraction(self):
        physical_large = make_node(
            "physical-large", total=120.0, used=0.0, available=10.0
        )
        effective_fit = make_node(
            "effective-fit", total=48.0, used=0.0, available=30.0
        )

        chosen, _ = pick_node(
            make_model("m", 20.0), [physical_large, effective_fit]
        )

        assert physical_large.available_vram_gb == 10.0
        assert chosen.node_id == "effective-fit"


# ── Exclusion offline / drain / pin ─────────────────────────────────────────

class TestPickNodeFiltering:
    def test_offline_nodes_excluded(self):
        offline = make_node("a", total=48.0, used=0.0, online=False)
        online = make_node("b", total=48.0, used=0.0)

        chosen, _ = pick_node(make_model("m", 10.0), [offline, online])
        assert chosen.node_id == "b"

    def test_draining_nodes_excluded(self):
        draining = make_node("a", total=48.0, used=0.0, draining=True)
        normal = make_node("b", total=48.0, used=0.0)

        chosen, _ = pick_node(make_model("m", 10.0), [draining, normal])
        assert chosen.node_id == "b"

    def test_no_candidates_raises(self):
        offline = make_node("a", online=False)
        draining = make_node("b", draining=True)

        with pytest.raises(NoPlacementError, match="Aucun nœud joignable"):
            pick_node(make_model("m", 10.0), [offline, draining])

    def test_empty_node_list_raises(self):
        with pytest.raises(NoPlacementError):
            pick_node(make_model("m", 10.0), [])

    def test_pin_to_node_respected(self):
        a = make_node("a", total=48.0, used=0.0)
        b = make_node("b", total=80.0, used=0.0)  # meilleur best-fit normalement

        chosen, _ = pick_node(make_model("m", 10.0, pin="a"), [a, b])
        assert chosen.node_id == "a"

    def test_pin_to_offline_node_raises(self):
        a = make_node("a", total=48.0, online=False)
        b = make_node("b", total=48.0)

        with pytest.raises(NoPlacementError, match="épinglé"):
            pick_node(make_model("m", 10.0, pin="a"), [a, b])

    def test_pin_to_draining_node_raises(self):
        a = make_node("a", total=48.0, draining=True)

        with pytest.raises(NoPlacementError, match="épinglé"):
            pick_node(make_model("m", 10.0, pin="a"), [a])

    def test_pin_to_unknown_node_raises(self):
        a = make_node("a", total=48.0)

        with pytest.raises(NoPlacementError, match="épinglé"):
            pick_node(make_model("m", 10.0, pin="zzz"), [a])


# ── Refus global ─────────────────────────────────────────────────────────────

class TestPickNodeRejection:
    def test_model_too_large_for_all_nodes(self):
        a = make_node("a", total=48.0, used=0.0)
        b = make_node("b", total=48.0, used=0.0)

        with pytest.raises(NoPlacementError, match="saturés"):
            pick_node(make_model("huge", 200.0), [a, b])

    def test_all_full_with_pinned_models_rejected(self):
        """Tous les nœuds saturés avec des modèles non évincibles."""
        pinned_a = loaded("p1", 40.0, active=1)
        pinned_b = loaded("p2", 40.0, active=1)
        a = make_node("a", total=48.0, used=40.0, free_ports=0, models=(pinned_a,))
        b = make_node("b", total=48.0, used=40.0, free_ports=0, models=(pinned_b,))

        with pytest.raises(NoPlacementError, match="saturés"):
            pick_node(make_model("m", 20.0), [a, b])


# ── Éviction simulée ────────────────────────────────────────────────────────

class TestPickNodeWithEviction:
    def test_prefers_node_with_least_eviction(self):
        """Entre deux nœuds qui doivent évincer, on choisit celui qui libère le moins."""
        # Nœud a : il faut évincer 30 GB pour libérer 20 GB nécessaires
        a = make_node(
            "a", total=48.0, used=48.0, free_ports=0,
            models=(loaded("a1", 30.0, idle=100.0), loaded("a2", 18.0, idle=50.0)),
        )
        # Nœud b : il faut évincer 20 GB seulement
        b = make_node(
            "b", total=48.0, used=48.0, free_ports=0,
            models=(loaded("b1", 20.0, idle=200.0), loaded("b2", 28.0, idle=50.0)),
        )

        chosen, plan = pick_node(make_model("m", 20.0), [a, b])

        assert chosen.node_id == "b"
        assert plan.models_to_evict == ("b1",)
        assert plan.freed_vram_gb == 20.0

    def test_eviction_unlocks_port(self):
        """free_ports = 0 mais l'éviction libère des ports → placement OK."""
        node = make_node(
            "a", total=48.0, used=48.0, free_ports=0,
            models=(loaded("old", 30.0, idle=300.0),),
        )

        chosen, plan = pick_node(make_model("m", 20.0), [node])

        assert chosen.node_id == "a"
        assert plan.models_to_evict == ("old",)

    def test_eviction_unlocks_port_even_when_vram_is_already_sufficient(self):
        node = make_node(
            "a",
            total=80.0,
            used=20.0,
            available=50.0,
            free_ports=0,
            models=(loaded("old", 20.0, idle=300.0),),
        )

        chosen, plan = pick_node(make_model("m", 10.0), [node])

        assert chosen.node_id == "a"
        assert plan.models_to_evict == ("old",)

    def test_eviction_lru_order(self):
        """Les modèles les plus inactifs sont évincés en premier."""
        node = make_node(
            "a", total=48.0, used=48.0, free_ports=0,
            models=(
                loaded("recent", 20.0, idle=10.0),
                loaded("old", 20.0, idle=500.0),
                loaded("medium", 8.0, idle=100.0),
            ),
        )

        chosen, plan = pick_node(make_model("m", 20.0), [node])

        assert chosen.node_id == "a"
        # On a besoin de 20 GB → 'old' (500s idle, 20 GB) suffit seul.
        assert plan.models_to_evict == ("old",)
        assert plan.freed_vram_gb == 20.0

    def test_eviction_multiple_models_needed(self):
        """Il faut cumuler plusieurs évictions pour libérer assez de VRAM."""
        node = make_node(
            "a", total=48.0, used=48.0, free_ports=0,
            models=(
                loaded("oldest", 10.0, idle=500.0),
                loaded("old", 15.0, idle=300.0),
                loaded("recent", 23.0, idle=10.0),
            ),
        )

        # Besoin 20 GB → 'oldest' (10) puis 'old' (15) = 25 GB cumulés, suffisant
        chosen, plan = pick_node(make_model("m", 20.0), [node])

        assert chosen.node_id == "a"
        assert plan.models_to_evict == ("oldest", "old")
        assert plan.freed_vram_gb == 25.0

    def test_pinned_models_not_evicted(self):
        """Un modèle avec active_requests > 0 ne peut PAS être évincé."""
        node = make_node(
            "a", total=48.0, used=48.0, free_ports=0,
            models=(
                loaded("active", 30.0, idle=999.0, active=1),  # idle énorme mais pinned
                loaded("idle", 18.0, idle=10.0),
            ),
        )

        # On a besoin de 20 GB. Seul 'idle' est évincible (18 GB) → pas assez.
        with pytest.raises(NoPlacementError, match="saturés"):
            pick_node(make_model("m", 20.0), [node])

    def test_prefers_immediate_fit_over_eviction(self):
        """Un nœud avec capacité immédiate gagne face à un nœud qui doit évincer."""
        empty = make_node("empty", total=48.0, used=0.0)
        full = make_node(
            "full", total=48.0, used=48.0, free_ports=0,
            models=(loaded("x", 30.0, idle=500.0),),
        )

        chosen, plan = pick_node(make_model("m", 20.0), [full, empty])

        assert chosen.node_id == "empty"
        assert plan.models_to_evict == ()

    def test_eviction_tie_break_by_node_id(self):
        """Deux nœuds doivent évincer la même quantité → tie-break alphabétique."""
        a = make_node(
            "zzz", total=48.0, used=48.0, free_ports=0,
            models=(loaded("z1", 20.0, idle=300.0),),
        )
        b = make_node(
            "aaa", total=48.0, used=48.0, free_ports=0,
            models=(loaded("b1", 20.0, idle=300.0),),
        )

        chosen, _ = pick_node(make_model("m", 20.0), [a, b])
        assert chosen.node_id == "aaa"


# ── build_eviction_plan ──────────────────────────────────────────────────────

class TestBuildEvictionPlan:
    def test_no_eviction_when_enough_free(self):
        node = make_node("a", total=48.0, used=10.0)
        plan = build_eviction_plan(node, vram_needed=20.0)

        assert plan.models_to_evict == ()
        assert plan.freed_vram_gb == 0.0

    def test_raises_when_max_potential_insufficient(self):
        node = make_node(
            "a", total=48.0, used=40.0,
            models=(loaded("pinned", 40.0, active=2),),
        )
        with pytest.raises(NoPlacementError, match="bloquent"):
            build_eviction_plan(node, vram_needed=30.0)

    def test_lru_order_strict(self):
        node = make_node(
            "a", total=48.0, used=48.0,
            models=(
                loaded("m1", 10.0, idle=50.0),
                loaded("m2", 10.0, idle=200.0),
                loaded("m3", 10.0, idle=100.0),
            ),
        )

        # Besoin de 15 GB → m2 (200s) puis m3 (100s) = 20 GB cumulés
        plan = build_eviction_plan(node, vram_needed=15.0)
        assert plan.models_to_evict == ("m2", "m3")
        assert plan.freed_vram_gb == 20.0


# ── max_potential_vram ──────────────────────────────────────────────────────

class TestMaxPotentialVram:
    def test_sum_of_evictable_plus_free(self):
        node = make_node(
            "a", total=48.0, used=40.0,
            models=(
                loaded("ev1", 15.0, idle=100.0),
                loaded("ev2", 25.0, idle=50.0),
            ),
        )
        # free = 8, evictables = 15 + 25 = 40 → 48 total
        assert max_potential_vram(node) == 48.0

    def test_ignores_pinned(self):
        node = make_node(
            "a", total=48.0, used=40.0,
            models=(
                loaded("pinned", 30.0, active=1),
                loaded("free", 10.0, idle=100.0),
            ),
        )
        # free = 8, evictables = 10 → 18
        assert max_potential_vram(node) == 18.0

    def test_empty_node(self):
        node = make_node("a", total=48.0, used=0.0)
        assert max_potential_vram(node) == 48.0


# ── Snapshot invariants ──────────────────────────────────────────────────────

class TestSnapshotProperties:
    def test_available_vram_never_negative(self):
        # Cas pathologique : used > total (peut arriver sur GB10 avec mémoire unifiée
        # si le calcul d'overhead a été trop optimiste).
        node = make_node("a", total=48.0, used=60.0)
        assert node.available_vram_gb == 0.0

    def test_is_candidate_requires_online_and_not_draining(self):
        assert make_node("a", online=True, draining=False).is_candidate is True
        assert make_node("a", online=False).is_candidate is False
        assert make_node("a", draining=True).is_candidate is False

    def test_is_evictable_requires_zero_active(self):
        assert loaded("m", 10.0, active=0).is_evictable is True
        assert loaded("m", 10.0, active=1).is_evictable is False


# ── EvictionPlan dataclass ───────────────────────────────────────────────────

class TestEvictionPlan:
    def test_immutable(self):
        plan = EvictionPlan(node_id="a", models_to_evict=("x",), freed_vram_gb=10.0)
        with pytest.raises(Exception):  # FrozenInstanceError
            plan.node_id = "b"  # type: ignore[misc]
