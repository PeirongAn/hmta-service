"""Unit tests for the constraint validator rule engine."""

from app.generation.validators.capability_check import check_capability_match
from app.generation.validators.safety_check import check_safety_constraints
from app.generation.validators.structure_check import check_structure_integrity


# ── Fixtures ──────────────────────────────────────────────────────────────────

ENTITIES = [
    {
        "entity_id": "robot_1",
        "entity_type": "robot",
        "status": "idle",
        "capabilities": [
            {"name": "navigation.move_to"},
            {"name": "perception.scan_zone"},
        ],
    },
    {
        "entity_id": "human_1",
        "entity_type": "human",
        "status": "idle",
        "capabilities": [{"name": "command.approve"}],
    },
]

VALID_BT = {
    "tree_id": "bt_test",
    "root_id": "seq_root",
    "nodes": {
        "seq_root": {
            "node_id": "seq_root",
            "type": "sequence",
            "name": "Root Sequence",
            "children": ["action_move", "action_scan"],
        },
        "action_move": {
            "node_id": "action_move",
            "type": "action",
            "name": "Move to Zone A",
            "children": [],
            "intent": "navigation.move_to",
            "entity": "robot_1",
            "params": {"target_zone": "zone_a"},
        },
        "action_scan": {
            "node_id": "action_scan",
            "type": "action",
            "name": "Scan Zone A",
            "children": [],
            "intent": "perception.scan_zone",
            "entity": "robot_1",
            "params": {"zone_id": "zone_a"},
        },
    },
}


# ── capability_check ──────────────────────────────────────────────────────────

def test_capability_check_passes_on_valid_bt():
    violations = check_capability_match(VALID_BT, ENTITIES)
    assert violations == []


def test_capability_check_detects_mismatch():
    bad_bt = {
        "nodes": {
            "a1": {
                "node_id": "a1",
                "type": "action",
                "name": "Disarm",
                "children": [],
                "intent": "manipulation.disarm",  # robot_1 does NOT have this
                "entity": "robot_1",
                "params": {},
            }
        }
    }
    violations = check_capability_match(bad_bt, ENTITIES)
    assert len(violations) == 1
    assert violations[0]["rule"] == "capability_mismatch"


# ── safety_check ──────────────────────────────────────────────────────────────

def test_safety_check_passes_normal_priority():
    violations = check_safety_constraints(VALID_BT, ENTITIES, {"priority": "normal"})
    assert violations == []


def test_safety_check_critical_requires_human_gate():
    violations = check_safety_constraints(VALID_BT, ENTITIES, {"priority": "critical"})
    assert any(v["rule"] == "missing_human_gate_for_critical" for v in violations)


def test_safety_check_offline_entity():
    offline_entities = [
        {**ENTITIES[0], "status": "offline"},
        ENTITIES[1],
    ]
    violations = check_safety_constraints(VALID_BT, offline_entities, {})
    assert any(v["rule"] == "offline_entity_assigned" for v in violations)


# ── structure_check ───────────────────────────────────────────────────────────

def test_structure_check_passes_on_valid_bt():
    violations = check_structure_integrity(VALID_BT)
    assert violations == []


def test_structure_check_missing_root_id():
    bad_bt = {"nodes": {}, "root_id": ""}
    violations = check_structure_integrity(bad_bt)
    assert any(v["rule"] == "missing_root_id" for v in violations)


def test_structure_check_unresolved_child():
    bad_bt = {
        "root_id": "seq_root",
        "nodes": {
            "seq_root": {
                "node_id": "seq_root",
                "type": "sequence",
                "name": "Root",
                "children": ["missing_node"],
            }
        },
    }
    violations = check_structure_integrity(bad_bt)
    assert any(v["rule"] == "unresolved_child" for v in violations)


def test_structure_check_empty_composite():
    bad_bt = {
        "root_id": "seq_root",
        "nodes": {
            "seq_root": {
                "node_id": "seq_root",
                "type": "sequence",
                "name": "Empty Sequence",
                "children": [],
            }
        },
    }
    violations = check_structure_integrity(bad_bt)
    assert any(v["rule"] == "empty_composite" for v in violations)
