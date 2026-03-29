"""T7 — Human interaction layer tests."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from app.execution.human_interaction.edit_handler import EditHandler
from app.execution.human_interaction.intervention_handler import InterventionHandler


class _FakeZenoh:
    def __init__(self):
        self._callback = None
        self.published: list[tuple[str, dict]] = []

    def subscribe(self, topic, callback):
        self._callback = callback
        return MagicMock()

    def publish(self, topic, data):
        self.published.append((topic, data))

    def simulate_message(self, data: dict):
        sample = MagicMock()
        sample.payload = json.dumps(data).encode()
        self._callback(sample)


class _FakeEngine:
    def __init__(self):
        self.paused = False
        self.resumed = False
        self.takeover_target = None
        self.abort_target = None
        self.bb_updates = {}
        self.reassigned = {}
        self.added_nodes = []
        self.removed_nodes = []
        self.modified_nodes = {}

    def pause(self):
        self.paused = True

    def resume(self):
        self.resumed = True

    def takeover_entity(self, entity_id):
        self.takeover_target = entity_id

    def abort_entity(self, entity_id):
        self.abort_target = entity_id

    def set_blackboard(self, key, value):
        self.bb_updates[key] = value

    def reassign_node(self, node_id, new_entity):
        self.reassigned[node_id] = new_entity

    def add_bt_node(self, node_def, parent_id):
        self.added_nodes.append((node_def, parent_id))

    def remove_bt_node(self, node_id):
        self.removed_nodes.append(node_id)

    def modify_bt_node(self, node_id, updates):
        self.modified_nodes[node_id] = updates


class TestInterventionHandler:
    def test_pause(self):
        z = _FakeZenoh()
        e = _FakeEngine()
        h = InterventionHandler(z, e)
        h.start()
        z.simulate_message({"intervention_type": "pause", "operator_id": "op1"})
        assert e.paused is True

    def test_resume(self):
        z = _FakeZenoh()
        e = _FakeEngine()
        h = InterventionHandler(z, e)
        h.start()
        z.simulate_message({"intervention_type": "resume", "operator_id": "op1"})
        assert e.resumed is True

    def test_takeover(self):
        z = _FakeZenoh()
        e = _FakeEngine()
        h = InterventionHandler(z, e)
        h.start()
        z.simulate_message({
            "intervention_type": "takeover",
            "operator_id": "op1",
            "target_entity_id": "robot_A",
        })
        assert e.takeover_target == "robot_A"

    def test_modify_blackboard(self):
        z = _FakeZenoh()
        e = _FakeEngine()
        h = InterventionHandler(z, e)
        h.start()
        z.simulate_message({
            "intervention_type": "modify_blackboard",
            "operator_id": "op1",
            "params": {"key": "/some/key", "value": 42},
        })
        assert e.bb_updates["/some/key"] == 42

    def test_unknown_type_acked_as_failure(self):
        z = _FakeZenoh()
        e = _FakeEngine()
        h = InterventionHandler(z, e)
        h.start()
        z.simulate_message({"intervention_type": "fly_away", "operator_id": "op1"})
        assert any(not pub[1].get("success") for pub in z.published)

    def test_invalid_payload(self):
        z = _FakeZenoh()
        e = _FakeEngine()
        h = InterventionHandler(z, e)
        h.start()
        sample = MagicMock()
        sample.payload = b"not json"
        z._callback(sample)


class TestEditHandler:
    def test_reassign(self):
        z = _FakeZenoh()
        e = _FakeEngine()
        h = EditHandler(z, e)
        h.start()
        z.simulate_message({
            "edit_type": "reassign",
            "operator_id": "op1",
            "params": {"node_id": "n1", "new_entity": "robot_B"},
        })
        assert e.reassigned.get("n1") == "robot_B"

    def test_remove_node(self):
        z = _FakeZenoh()
        e = _FakeEngine()
        h = EditHandler(z, e)
        h.start()
        z.simulate_message({
            "edit_type": "remove_node",
            "operator_id": "op1",
            "params": {"node_id": "n2"},
        })
        assert "n2" in e.removed_nodes

    def test_regenerate_callback(self):
        z = _FakeZenoh()
        e = _FakeEngine()
        called = []
        h = EditHandler(z, e, regenerate_callback=lambda: called.append(True))
        h.start()
        z.simulate_message({
            "edit_type": "regenerate",
            "operator_id": "op1",
            "params": {},
        })
        assert len(called) == 1

    def test_regenerate_no_callback(self):
        z = _FakeZenoh()
        e = _FakeEngine()
        h = EditHandler(z, e)
        h.start()
        z.simulate_message({
            "edit_type": "regenerate",
            "operator_id": "op1",
            "params": {},
        })
        assert any(not pub[1].get("success") for pub in z.published)
