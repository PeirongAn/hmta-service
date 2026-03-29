"""Focused tests for advanced BT structure validation."""

from __future__ import annotations

from app.generation.validators.structure_check import check_structure_integrity


def test_detects_cycle_and_unreachable_node() -> None:
    bt = {
        "root_id": "root",
        "nodes": {
            "root": {"node_id": "root", "type": "sequence", "name": "Root", "children": ["a"]},
            "a": {"node_id": "a", "type": "sequence", "name": "A", "children": ["root"]},
            "orphan": {"node_id": "orphan", "type": "action", "name": "Orphan", "children": []},
        },
    }

    violations = check_structure_integrity(bt)
    rules = {item["rule"] for item in violations}

    assert "cycle_detected" in rules
    assert "unreachable_node" in rules


def test_detects_node_id_mismatch_and_invalid_decorator_arity() -> None:
    bt = {
        "root_id": "root",
        "nodes": {
            "root": {"node_id": "wrong_root", "type": "timeout", "name": "Root", "children": ["a", "b"]},
            "a": {"node_id": "a", "type": "action", "name": "A", "children": []},
            "b": {"node_id": "b", "type": "action", "name": "B", "children": []},
        },
    }

    violations = check_structure_integrity(bt)
    rules = {item["rule"] for item in violations}

    assert "node_id_mismatch" in rules
    assert "invalid_decorator_arity" in rules
