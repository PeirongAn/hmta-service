"""Tree Loader — converts BT JSON from LangGraph into a py_trees node tree."""

from __future__ import annotations

import logging

import py_trees

from app.execution.behaviours.command_action import CommandAction
from app.execution.behaviours.entity_worker import EntityWorker
from app.execution.behaviours.goal_confirmation_gate import GoalConfirmationGate
from app.execution.behaviours.human_gate import HumanGate
from app.execution.behaviours.human_monitor import HumanMonitor
from app.execution.behaviours.mission_monitor import MissionMonitor
from app.execution.behaviours.precondition_check import PreconditionCheck
from app.execution.behaviours.supervised_fallback import SupervisedFallbackDecorator
from app.execution.behaviours.zenoh_condition import ZenohCondition

logger = logging.getLogger(__name__)


def _make_sequence(node: dict) -> py_trees.behaviour.Behaviour:
    return py_trees.composites.Sequence(name=node["name"], memory=True)


def _make_selector(node: dict) -> py_trees.behaviour.Behaviour:
    return py_trees.composites.Selector(name=node["name"], memory=True)


_SUCCESS_ON_ONE_ALIASES = frozenset({
    "wait_any", "success_on_one", "SuccessOnOne", "race", "any",
})


def _make_parallel(node: dict) -> py_trees.behaviour.Behaviour:
    raw_policy = node.get("policy") or node.get("parallel_policy") or ""
    if raw_policy in _SUCCESS_ON_ONE_ALIASES:
        policy = py_trees.common.ParallelPolicy.SuccessOnOne()
    else:
        policy = py_trees.common.ParallelPolicy.SuccessOnAll()
    return py_trees.composites.Parallel(name=node["name"], policy=policy)


def _make_condition(node: dict) -> py_trees.behaviour.Behaviour:
    params = node.get("params") or {}
    key = node.get("key") or params.get("key") or "undefined"
    expected = node.get("expected")
    if expected is None:
        expected = params.get("expected")
    if key != "undefined" and expected is not None:
        return ZenohCondition(name=node["name"], key=key, expected=expected)
    return py_trees.behaviours.Success(name=node["name"])


def _make_action(node: dict) -> CommandAction:
    return CommandAction(
        name=node["name"],
        intent=node.get("intent", "system.halt"),
        entity_id=node.get("entity", "unknown"),
        params=node.get("params", {}),
    )


def _make_human_gate(node: dict) -> HumanGate:
    return HumanGate(
        name=node["name"],
        entity_id=node.get("entity", "unknown"),
        intent=node.get("intent", "command.approve"),
        params=node.get("params", {}),
        timeout_sec=node.get("timeout_sec", 120.0),
    )


def _make_precondition(node: dict) -> PreconditionCheck:
    return PreconditionCheck(
        name=node["name"],
        skill_name=node.get("intent", node.get("skill_name", "")),
        entity_id=node.get("entity", "unknown"),
    )


def _make_entity_worker(node: dict) -> EntityWorker:
    return EntityWorker(
        name=node["name"],
        entity_id=node.get("entity", "unknown"),
        human_supervisor_id=node.get("human_supervisor", "operator-01"),
        human_fallback_timeout_sec=float(node.get("timeout_sec", 180.0)),
    )


def _make_human_monitor(node: dict) -> HumanMonitor:
    return HumanMonitor(
        name=node["name"],
        entity_id=node.get("entity", "unknown"),
    )


def _make_mission_monitor(node: dict) -> MissionMonitor:
    return MissionMonitor(name=node["name"])


def _make_goal_confirmation(node: dict) -> GoalConfirmationGate:
    return GoalConfirmationGate(
        name=node["name"],
        human_supervisor_id=node.get("entity", "operator-01"),
        timeout_sec=float(node.get("timeout_sec", 120.0)),
    )


_NODE_BUILDERS = {
    "sequence": _make_sequence,
    "selector": _make_selector,
    "parallel": _make_parallel,
    "condition": _make_condition,
    "precondition": _make_precondition,
    "action": _make_action,
    "humanGate": _make_human_gate,
    # Generic HMTA node types
    "entity_worker": _make_entity_worker,
    "human_monitor": _make_human_monitor,
    "mission_monitor": _make_mission_monitor,
    "goal_confirmation": _make_goal_confirmation,
}


def _build_node(
    nodes: dict[str, dict],
    node_id: str,
    visited: set[str] | None = None,
    stack: set[str] | None = None,
) -> py_trees.behaviour.Behaviour:
    visited = visited or set()
    stack = stack or set()

    if node_id in stack:
        raise ValueError(f"Cycle detected while loading node '{node_id}'")

    node_data = nodes.get(node_id)
    if not node_data:
        raise ValueError(f"Node '{node_id}' not found in BT JSON")
    if node_data.get("node_id") != node_id:
        raise ValueError(
            f"Node key '{node_id}' does not match declared node_id '{node_data.get('node_id')}'"
        )

    node_type = node_data.get("type", "")
    children = node_data.get("children", [])

    stack.add(node_id)
    visited.add(node_id)

    if node_type == "supervised_fallback":
        built_children = [
            _build_node(nodes, cid, visited=visited, stack=stack)
            for cid in children
        ]
        if len(built_children) == 1:
            child = built_children[0]
        else:
            child = py_trees.composites.Sequence(
                name=f"{node_data['name']} (inner)", memory=True,
            )
            for c in built_children:
                child.add_child(c)
        human_entity = (
            node_data.get("human_entity")
            or node_data.get("params", {}).get("human_entity")
            or "operator-01"
        )
        result = SupervisedFallbackDecorator(
            name=node_data["name"],
            child=child,
            human_entity_id=human_entity,
            max_retries=node_data.get("max_retries", 3),
            timeout_sec=node_data.get("timeout_sec", 120.0),
        )
        stack.remove(node_id)
        return result

    if node_type == "timeout":
        if len(children) != 1:
            raise ValueError(f"Timeout node '{node_id}' must have exactly one child")
        child = _build_node(nodes, children[0], visited=visited, stack=stack)
        result = py_trees.decorators.Timeout(
            name=node_data["name"],
            duration=node_data.get("timeout_sec", 60.0),
            child=child,
        )
        stack.remove(node_id)
        return result

    if node_type == "retry":
        if len(children) != 1:
            raise ValueError(f"Retry node '{node_id}' must have exactly one child")
        child = _build_node(nodes, children[0], visited=visited, stack=stack)
        result = py_trees.decorators.Retry(
            name=node_data["name"],
            num_failures=node_data.get("max_retries", 3),
            child=child,
        )
        stack.remove(node_id)
        return result

    if node_type == "repeat":
        if len(children) != 1:
            raise ValueError(f"Repeat node '{node_id}' must have exactly one child")
        child = _build_node(nodes, children[0], visited=visited, stack=stack)
        result = py_trees.decorators.Repeat(
            name=node_data["name"],
            num_success=node_data.get("max_retries", 3),
            child=child,
        )
        stack.remove(node_id)
        return result

    builder = _NODE_BUILDERS.get(node_type)
    if not builder:
        stack.remove(node_id)
        logger.warning("Unknown node type '%s', substituting Failure", node_type)
        return py_trees.behaviours.Failure(name=node_data.get("name", node_id))

    bt_node = builder(node_data)

    # Recursively attach children (composites only)
    if hasattr(bt_node, "add_child"):
        for child_id in children:
            child = _build_node(nodes, child_id, visited=visited, stack=stack)
            bt_node.add_child(child)

    stack.remove(node_id)
    return bt_node


def load_tree(bt_json: dict) -> py_trees.trees.BehaviourTree:
    """
    Convert a BT JSON dict (from LangGraph) into a py_trees BehaviourTree.

    bt_json structure:
        {
          "tree_id": "...",
          "root_id": "<node_id>",
          "nodes": { "<node_id>": { "type": "...", ... }, ... }
        }
    """
    nodes: dict[str, dict] = bt_json.get("nodes") or {}
    root_id: str = bt_json.get("root_id", "")
    if not root_id or root_id not in nodes:
        raise ValueError(f"Invalid BT JSON: root_id='{root_id}' not in nodes")

    root = _build_node(nodes, root_id)
    tree = py_trees.trees.BehaviourTree(root=root)
    logger.info("BT loaded: root='%s', total_nodes=%d", root.name, len(nodes))
    return tree
