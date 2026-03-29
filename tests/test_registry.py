"""T4 — Capability registry tests."""

from __future__ import annotations

import pytest

from app.capability.registry import CapabilityRegistry


@pytest.fixture
def registry() -> CapabilityRegistry:
    r = CapabilityRegistry()
    r.register_entity({
        "entity_id": "robot_A",
        "type": "robot",
        "structured_capabilities": [
            {"name": "move", "mode": "autonomous", "proficiency": 0.9},
            {"name": "detect", "mode": "supervised", "proficiency": 0.7},
        ],
    })
    return r


class TestEntityRegistration:
    def test_register(self, registry: CapabilityRegistry):
        graph = registry.get_graph_ref()
        assert "robot_A" in graph.nodes
        assert "move" in graph.nodes
        assert "detect" in graph.nodes

    def test_duplicate_register_updates(self, registry: CapabilityRegistry):
        registry.register_entity({
            "entity_id": "robot_A",
            "type": "robot",
            "structured_capabilities": [
                {"name": "move", "mode": "autonomous", "proficiency": 0.95},
            ],
        })
        results = registry.query_entities_for_capability("move")
        assert len(results) == 1
        assert results[0][1] == 0.95

    def test_unregister(self, registry: CapabilityRegistry):
        registry.unregister_entity("robot_A")
        assert "robot_A" not in registry.get_graph_ref().nodes

    def test_flat_capabilities_fallback(self):
        r = CapabilityRegistry()
        r.register_entity({
            "entity_id": "robot_B",
            "type": "robot",
            "capabilities": ["move", "scan"],
        })
        assert len(r.query_entities_for_capability("move")) == 1


class TestQueries:
    def test_query_min_proficiency(self, registry: CapabilityRegistry):
        results = registry.query_entities_for_capability("move", min_proficiency=0.5)
        assert len(results) == 1

        results = registry.query_entities_for_capability("move", min_proficiency=0.95)
        assert len(results) == 0

    def test_all_entity_ids(self, registry: CapabilityRegistry):
        ids = registry.all_entity_ids()
        assert "robot_A" in ids


class TestSnapshot:
    def test_snapshot_is_copy(self, registry: CapabilityRegistry):
        snap = registry.get_graph_snapshot()
        snap.remove_node("robot_A")
        assert "robot_A" in registry.get_graph_ref().nodes
