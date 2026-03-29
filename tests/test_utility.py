"""T3 — Utility calculation tests."""

from __future__ import annotations

import pytest

from app.capability.hypergraph import HEdge, HNode, HyperGraph
from app.capability.utility import (
    AllocationScore,
    bayesian_update,
    compute_human_utility,
    compute_robot_utility,
    reload_weights,
)


@pytest.fixture
def graph() -> HyperGraph:
    g = HyperGraph()
    g.add_node(HNode(id="robot_A", kind="entity", attrs={
        "type": "robot", "battery": 80, "status": "idle", "mode": "autonomous",
    }))
    g.add_node(HNode(id="robot_B", kind="entity", attrs={
        "type": "robot", "battery": 40, "status": "idle", "mode": "remote_control",
    }))
    g.add_node(HNode(id="human_1", kind="entity", attrs={
        "type": "human", "decision_accuracy": 0.9, "avg_response_sec": 5.0,
        "authority_level": True, "max_concurrent_tasks": 3, "current_task_count": 1,
    }))
    g.add_node(HNode(id="move", kind="capability"))
    g.add_node(HNode(id="task_1", kind="task"))

    g.add_edge(HEdge(id="hc_A_move", kind="has_capability",
                      nodes=frozenset(["robot_A", "move"]), weight=0.9))
    g.add_edge(HEdge(id="hc_B_move", kind="has_capability",
                      nodes=frozenset(["robot_B", "move"]), weight=0.5))
    g.add_edge(HEdge(id="req_task1_move", kind="requires",
                      nodes=frozenset(["task_1", "move"]), weight=1.0))
    return g


class TestRobotUtility:
    def test_basic_score(self, graph: HyperGraph):
        score = compute_robot_utility(graph.nodes["robot_A"], graph.nodes["task_1"], graph)
        assert isinstance(score, AllocationScore)
        assert 0.0 <= score.total <= 1.5

    def test_higher_proficiency_higher_score(self, graph: HyperGraph):
        score_a = compute_robot_utility(graph.nodes["robot_A"], graph.nodes["task_1"], graph)
        score_b = compute_robot_utility(graph.nodes["robot_B"], graph.nodes["task_1"], graph)
        assert score_a.total > score_b.total

    def test_mode_preference(self, graph: HyperGraph):
        score_a = compute_robot_utility(graph.nodes["robot_A"], graph.nodes["task_1"], graph)
        score_b = compute_robot_utility(graph.nodes["robot_B"], graph.nodes["task_1"], graph)
        assert score_a.breakdown["mode_preference"] > score_b.breakdown["mode_preference"]

    def test_breakdown_present(self, graph: HyperGraph):
        score = compute_robot_utility(graph.nodes["robot_A"], graph.nodes["task_1"], graph)
        assert "proficiency" in score.breakdown
        assert "energy" in score.breakdown
        assert "availability" in score.breakdown


class TestHumanUtility:
    def test_basic_score(self, graph: HyperGraph):
        score = compute_human_utility(graph.nodes["human_1"], graph.nodes["task_1"], graph)
        assert isinstance(score, AllocationScore)
        assert score.total > 0.0

    def test_cognitive_load_impact(self, graph: HyperGraph):
        node = graph.nodes["human_1"]
        score_low = compute_human_utility(node, graph.nodes["task_1"], graph)
        node.attrs["current_task_count"] = 3
        score_high = compute_human_utility(node, graph.nodes["task_1"], graph)
        assert score_low.total > score_high.total


class TestBayesianUpdate:
    def test_returns_weights_unchanged(self):
        w = {"proficiency": 0.3, "proximity": 0.2}
        result = bayesian_update(w, [])
        assert result == w
