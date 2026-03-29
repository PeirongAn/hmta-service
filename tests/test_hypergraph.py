"""T1 — Hypergraph data structure tests."""

from __future__ import annotations

import pytest

from app.capability.hypergraph import HEdge, HNode, HyperGraph


@pytest.fixture
def graph() -> HyperGraph:
    g = HyperGraph()
    g.add_node(HNode(id="robot_A", kind="entity", attrs={"type": "robot"}))
    g.add_node(HNode(id="robot_B", kind="entity", attrs={"type": "robot"}))
    g.add_node(HNode(id="move", kind="capability"))
    g.add_node(HNode(id="detect", kind="capability"))
    g.add_node(HNode(id="task_1", kind="task"))

    g.add_edge(HEdge(id="hc_A_move", kind="has_capability", nodes=frozenset(["robot_A", "move"]), weight=0.9))
    g.add_edge(HEdge(id="hc_A_detect", kind="has_capability", nodes=frozenset(["robot_A", "detect"]), weight=0.7))
    g.add_edge(HEdge(id="hc_B_move", kind="has_capability", nodes=frozenset(["robot_B", "move"]), weight=0.6))
    g.add_edge(HEdge(id="req_task1_move", kind="requires", nodes=frozenset(["task_1", "move"]), weight=1.0))
    return g


class TestNodeOperations:
    def test_add_and_remove_node(self, graph: HyperGraph):
        assert "robot_A" in graph.nodes
        graph.remove_node("robot_A")
        assert "robot_A" not in graph.nodes
        assert "hc_A_move" not in graph.edges
        assert "hc_A_detect" not in graph.edges

    def test_remove_nonexistent_node(self, graph: HyperGraph):
        graph.remove_node("nonexistent")
        assert len(graph.nodes) == 5


class TestEdgeOperations:
    def test_add_and_remove_edge(self, graph: HyperGraph):
        assert "hc_A_move" in graph.edges
        graph.remove_edge("hc_A_move")
        assert "hc_A_move" not in graph.edges

    def test_remove_nonexistent_edge(self, graph: HyperGraph):
        graph.remove_edge("nonexistent")
        assert len(graph.edges) == 4


class TestQueries:
    def test_neighbors(self, graph: HyperGraph):
        neighbors = graph.neighbors("robot_A")
        assert "move" in neighbors
        assert "detect" in neighbors

    def test_neighbors_filtered_by_kind(self, graph: HyperGraph):
        neighbors = graph.neighbors("robot_A", edge_kind="has_capability")
        assert "move" in neighbors
        assert "detect" in neighbors
        assert "task_1" not in neighbors

    def test_entities_with_capability(self, graph: HyperGraph):
        results = graph.entities_with_capability("move")
        assert len(results) == 2
        assert results[0] == ("robot_A", 0.9)
        assert results[1] == ("robot_B", 0.6)

    def test_capabilities_for_task(self, graph: HyperGraph):
        results = graph.capabilities_for_task("task_1")
        assert len(results) == 1
        assert results[0] == ("move", 1.0)

    def test_find_collaborations(self, graph: HyperGraph):
        graph.add_edge(HEdge(
            id="collab_1",
            kind="collaborates",
            nodes=frozenset(["robot_A", "robot_B", "task_1"]),
            weight=0.8,
        ))
        collabs = graph.find_collaborations("task_1")
        assert len(collabs) == 1
        assert collabs[0].weight == 0.8

    def test_subgraph(self, graph: HyperGraph):
        sub = graph.subgraph({"robot_A", "move"})
        assert "robot_A" in sub.nodes
        assert "move" in sub.nodes
        assert "robot_B" not in sub.nodes
        assert "hc_A_move" in sub.edges


class TestSerialization:
    def test_roundtrip(self, graph: HyperGraph):
        d = graph.to_dict()
        g2 = HyperGraph.from_dict(d)
        assert set(g2.nodes.keys()) == set(graph.nodes.keys())
        assert set(g2.edges.keys()) == set(graph.edges.keys())


class TestEdgeCases:
    def test_empty_graph_queries(self):
        g = HyperGraph()
        assert g.neighbors("nope") == set()
        assert g.entities_with_capability("move") == []
        assert g.capabilities_for_task("t") == []
        assert g.find_collaborations("t") == []
