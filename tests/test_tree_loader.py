"""Unit tests for tree_loader — JSON → py_trees conversion."""

import py_trees
import pytest

from app.execution.tree_loader import load_tree


SIMPLE_BT = {
    "tree_id": "bt_simple",
    "root_id": "seq_root",
    "nodes": {
        "seq_root": {
            "node_id": "seq_root",
            "type": "sequence",
            "name": "Root Sequence",
            "children": ["cond_check", "action_move"],
        },
        "cond_check": {
            "node_id": "cond_check",
            "type": "condition",
            "name": "Zone clear?",
            "children": [],
            "key": "zones/zone_a/cleared",
            "expected": True,
        },
        "action_move": {
            "node_id": "action_move",
            "type": "action",
            "name": "Move to Zone A",
            "children": [],
            "intent": "navigation.move_to",
            "entity": "robot_1",
            "params": {},
        },
    },
}


def test_load_simple_tree():
    tree = load_tree(SIMPLE_BT)
    assert isinstance(tree, py_trees.trees.BehaviourTree)
    assert tree.root.name == "Root Sequence"
    assert len(tree.root.children) == 2


def test_root_is_sequence():
    tree = load_tree(SIMPLE_BT)
    assert isinstance(tree.root, py_trees.composites.Sequence)


def test_action_node_type():
    from app.execution.behaviours.command_action import CommandAction
    tree = load_tree(SIMPLE_BT)
    action = tree.root.children[1]
    assert isinstance(action, CommandAction)
    assert action.intent == "navigation.move_to"
    assert action.entity_id == "robot_1"


def test_invalid_root_raises():
    bad_bt = {"root_id": "nonexistent", "nodes": {}}
    with pytest.raises(ValueError, match="root_id"):
        load_tree(bad_bt)


def test_parallel_node():
    bt = {
        "tree_id": "bt_par",
        "root_id": "par_root",
        "nodes": {
            "par_root": {
                "node_id": "par_root",
                "type": "parallel",
                "name": "Parallel Tasks",
                "children": ["a1", "a2"],
                "policy": "wait_all",
            },
            "a1": {"node_id": "a1", "type": "action", "name": "A1", "children": [],
                   "intent": "x", "entity": "e", "params": {}},
            "a2": {"node_id": "a2", "type": "action", "name": "A2", "children": [],
                   "intent": "y", "entity": "e", "params": {}},
        },
    }
    tree = load_tree(bt)
    assert isinstance(tree.root, py_trees.composites.Parallel)
    assert len(tree.root.children) == 2


def test_timeout_decorator():
    bt = {
        "tree_id": "bt_timeout",
        "root_id": "to_node",
        "nodes": {
            "to_node": {
                "node_id": "to_node",
                "type": "timeout",
                "name": "With Timeout",
                "children": ["inner"],
                "timeout_sec": 30,
            },
            "inner": {"node_id": "inner", "type": "action", "name": "Inner", "children": [],
                      "intent": "x", "entity": "e", "params": {}},
        },
    }
    tree = load_tree(bt)
    assert isinstance(tree.root, py_trees.decorators.Timeout)


def test_cycle_raises():
    bt = {
        "tree_id": "bt_cycle",
        "root_id": "root",
        "nodes": {
            "root": {"node_id": "root", "type": "sequence", "name": "Root", "children": ["root"]},
        },
    }
    with pytest.raises(ValueError, match="Cycle detected"):
        load_tree(bt)


def test_invalid_timeout_child_count_raises():
    bt = {
        "tree_id": "bt_timeout_bad",
        "root_id": "to_node",
        "nodes": {
            "to_node": {
                "node_id": "to_node",
                "type": "timeout",
                "name": "Timeout",
                "children": [],
            },
        },
    }
    with pytest.raises(ValueError, match="exactly one child"):
        load_tree(bt)


def test_node_id_mismatch_raises():
    bt = {
        "tree_id": "bt_bad_node_id",
        "root_id": "root",
        "nodes": {
            "root": {"node_id": "wrong", "type": "sequence", "name": "Root", "children": []},
        },
    }
    with pytest.raises(ValueError, match="does not match"):
        load_tree(bt)
