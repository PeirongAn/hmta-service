"""Sprint 0 verification — proficiency updates, fatigue modulation, resolver overrides."""

from __future__ import annotations

import pytest

from app.capability.effective_resolver import EffectiveCapabilityResolver
from app.capability.hypergraph import HEdge, HNode, HyperGraph
from app.capability.registry import CapabilityRegistry
from app.capability.utility import compute_human_utility


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_registry_with_robot() -> CapabilityRegistry:
    reg = CapabilityRegistry()
    reg.register_entity({
        "entity_id": "robot_A",
        "entity_type": "robot",
        "status": "online",
        "structured_capabilities": [
            {"name": "patrol", "mode": "autonomous", "proficiency": 0.9},
            {"name": "detect", "mode": "supervised", "proficiency": 0.85},
        ],
    })
    return reg


def _make_human_entity_node(
    entity_id: str = "operator-01",
    accuracy: float = 0.85,
    response_sec: float = 8.0,
    fatigue: float = 0.0,
) -> HNode:
    return HNode(
        id=entity_id,
        kind="entity",
        attrs={
            "entity_type": "human",
            "decision_accuracy": accuracy,
            "avg_response_sec": response_sec,
            "fatigue_level": fatigue,
            "authority_level": "operator",
            "max_concurrent_tasks": 3,
            "current_task_count": 0,
        },
    )


def _make_task_node(task_id: str = "t1") -> HNode:
    return HNode(id=task_id, kind="task", attrs={})


# ---------------------------------------------------------------------------
# Tests: update_proficiency
# ---------------------------------------------------------------------------

class TestUpdateProficiency:
    def test_update_changes_edge_weight(self):
        reg = _make_registry_with_robot()
        reg.update_proficiency("robot_A", "patrol", 0.75)

        graph = reg.get_graph_ref()
        edge = graph.edges.get("hc_robot_A_patrol")
        assert edge is not None
        assert edge.weight == pytest.approx(0.75)

    def test_update_records_history(self):
        reg = _make_registry_with_robot()
        reg.update_proficiency("robot_A", "patrol", 0.75)
        reg.update_proficiency("robot_A", "patrol", 0.80)

        edge = reg.get_graph_ref().edges["hc_robot_A_patrol"]
        history = edge.attrs.get("proficiency_history", [])
        assert len(history) == 2
        assert history[0]["value"] == pytest.approx(0.9)
        assert history[1]["value"] == pytest.approx(0.75)

    def test_update_clamps_to_01(self):
        reg = _make_registry_with_robot()
        reg.update_proficiency("robot_A", "patrol", 1.5)
        assert reg.get_graph_ref().edges["hc_robot_A_patrol"].weight == 1.0

        reg.update_proficiency("robot_A", "patrol", -0.3)
        assert reg.get_graph_ref().edges["hc_robot_A_patrol"].weight == 0.0

    def test_update_nonexistent_edge_is_noop(self):
        reg = _make_registry_with_robot()
        reg.update_proficiency("robot_A", "nonexistent_cap", 0.5)

    def test_alias_resolution(self):
        reg = _make_registry_with_robot()
        reg.update_proficiency("robot_A", "巡逻", 0.70)
        edge = reg.get_graph_ref().edges.get("hc_robot_A_patrol")
        assert edge is not None
        assert edge.weight == pytest.approx(0.70)


# ---------------------------------------------------------------------------
# Tests: update_cognitive_profile
# ---------------------------------------------------------------------------

class TestUpdateCognitiveProfile:
    def test_update_flattens_to_attrs(self):
        reg = _make_registry_with_robot()
        reg.register_entity({
            "entity_id": "op1",
            "entity_type": "human",
            "cognitive_profile": {"decision_accuracy": 0.85, "avg_response_sec": 8.0},
        })
        reg.update_cognitive_profile("op1", {"decision_accuracy": 0.90, "avg_response_sec": 6.0})

        node = reg.get_graph_ref().nodes["op1"]
        assert node.attrs["decision_accuracy"] == pytest.approx(0.90)
        assert node.attrs["avg_response_sec"] == pytest.approx(6.0)
        assert node.attrs["cognitive_profile"]["decision_accuracy"] == pytest.approx(0.90)

    def test_update_nonexistent_entity_is_noop(self):
        reg = _make_registry_with_robot()
        reg.update_cognitive_profile("ghost", {"decision_accuracy": 0.5})


# ---------------------------------------------------------------------------
# Tests: _resolve_proficiency (replaces hardcoded 0.9)
# ---------------------------------------------------------------------------

class TestResolveProficiency:
    def test_default_base_proficiency(self):
        resolver = EffectiveCapabilityResolver()
        graph = HyperGraph()
        graph.add_node(HNode(id="op1", kind="entity", attrs={"entity_type": "human"}))
        prof = resolver._resolve_proficiency("op1", "approve", graph)
        assert prof == pytest.approx(0.9)

    def test_override_from_entity_attrs(self):
        resolver = EffectiveCapabilityResolver()
        graph = HyperGraph()
        graph.add_node(HNode(
            id="op1", kind="entity",
            attrs={"entity_type": "human", "proficiency_overrides": {"approve": 0.95, "observe": 0.80}},
        ))
        assert resolver._resolve_proficiency("op1", "approve", graph) == pytest.approx(0.95)
        assert resolver._resolve_proficiency("op1", "observe", graph) == pytest.approx(0.80)
        assert resolver._resolve_proficiency("op1", "path_planning", graph) == pytest.approx(0.9)

    def test_missing_entity_returns_base(self):
        resolver = EffectiveCapabilityResolver()
        graph = HyperGraph()
        assert resolver._resolve_proficiency("ghost", "approve", graph) == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# Tests: fatigue modulation in compute_human_utility
# ---------------------------------------------------------------------------

class TestFatigueModulation:
    def test_zero_fatigue_no_degradation(self):
        graph = HyperGraph()
        entity = _make_human_entity_node(fatigue=0.0)
        graph.add_node(entity)
        task = _make_task_node()
        graph.add_node(task)

        score = compute_human_utility(entity, task, graph)
        assert score.breakdown["decision_accuracy"] == pytest.approx(0.85)
        assert score.breakdown["fatigue"] == pytest.approx(0.0)

    def test_high_fatigue_degrades_accuracy(self):
        graph = HyperGraph()
        entity = _make_human_entity_node(fatigue=0.8)
        graph.add_node(entity)
        task = _make_task_node()
        graph.add_node(task)

        score = compute_human_utility(entity, task, graph)
        expected_accuracy = 0.85 * (1.0 - 0.3 * 0.8)
        assert score.breakdown["decision_accuracy"] == pytest.approx(expected_accuracy)

    def test_full_fatigue_lowers_total_score(self):
        graph = HyperGraph()
        entity_fresh = _make_human_entity_node(fatigue=0.0)
        entity_tired = _make_human_entity_node(fatigue=1.0, entity_id="op-tired")
        graph.add_node(entity_fresh)
        graph.add_node(entity_tired)
        task = _make_task_node()
        graph.add_node(task)

        score_fresh = compute_human_utility(entity_fresh, task, graph)
        score_tired = compute_human_utility(entity_tired, task, graph)
        assert score_tired.total < score_fresh.total

    def test_fatigue_slows_response_speed(self):
        graph = HyperGraph()
        entity = _make_human_entity_node(fatigue=1.0, response_sec=10.0)
        graph.add_node(entity)
        task = _make_task_node()
        graph.add_node(task)

        score = compute_human_utility(entity, task, graph)
        # At fatigue=1.0, response_sec inflates by 50%: 10 * 1.5 = 15
        # _normalise_response_speed(15) = max(1 - 15/30, 0) = 0.5
        assert score.breakdown["response_speed"] == pytest.approx(0.5)
