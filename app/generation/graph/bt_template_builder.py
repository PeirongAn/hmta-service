"""BT Template Builder — pure Python, no LLM.

Generates a fixed-structure Generic HMTA BT JSON from the entity list.
The output has ~10 nodes regardless of mission complexity. All task
logic lives in the Blackboard task queue (populated by fsm_bb_init).

Generated structure:
    repeat(forever)
      └── selector
            ├── sequence  [mission_monitor → report_to_operator]
            ├── parallel(success_on_all)
            │     ├── entity_worker(robot1)
            │     ├── entity_worker(robot2)
            │     └── human_monitor(operator-01)
            └── action(wait 1s)   ← always succeeds, prevents selector FAILURE
"""

from __future__ import annotations

import logging
from typing import Any

from app.generation.graph.progress import publish_step
from app.generation.graph.state import GenerationState

logger = logging.getLogger(__name__)


def _build_generic_bt(
    task_id: str,
    entities: list[dict],
    mission_goal: dict | None = None,
) -> dict[str, Any]:
    """
    Construct the fixed BT JSON dict.

    Parameters
    ----------
    task_id:  mission / BT identifier
    entities: list of entity dicts (from GenerationState)
    """
    robots: list[dict] = []
    humans: list[dict] = []

    for e in entities:
        etype = (e.get("entity_type") or e.get("type", "robot")).lower()
        if etype == "human":
            humans.append(e)
        else:
            robots.append(e)

    primary_supervisor = humans[0]["entity_id"] if humans else "operator-01"

    nodes: dict[str, Any] = {}

    # ── Root: repeat forever (max_retries=99999 ≈ forever) ────────────────────
    nodes["root_repeat"] = {
        "node_id": "root_repeat",
        "name": "Root (Repeat)",
        "type": "repeat",
        "max_retries": 99999,
        "children": ["main_sel"],
    }

    # ── Main selector ─────────────────────────────────────────────────────────
    nodes["main_sel"] = {
        "node_id": "main_sel",
        "name": "Main Selector",
        "type": "selector",
        "children": ["seq_done", "par_workers", "wait_idle"],
    }

    # ── Mission-done sequence: MissionMonitor + report ─────────────────────────
    nodes["seq_done"] = {
        "node_id": "seq_done",
        "name": "Mission Done",
        "type": "sequence",
        "children": ["cond_done", "a_report"],
    }
    nodes["cond_done"] = {
        "node_id": "cond_done",
        "name": "All Tasks Complete?",
        "type": "mission_monitor",
        "params": {},
    }
    nodes["a_report"] = {
        "node_id": "a_report",
        "name": "Report to Operator",
        "type": "action",
        "intent": "report",
        "entity": primary_supervisor,
        "params": {"message": "Mission complete"},
    }

    # ── Parallel workers ───────────────────────────────────────────────────────
    worker_children: list[str] = []

    for robot in robots:
        rid = robot["entity_id"]
        nid = f"w_{rid}"
        nodes[nid] = {
            "node_id": nid,
            "name": f"Worker [{rid}]",
            "type": "entity_worker",
            "entity": rid,
            "human_supervisor": primary_supervisor,
        }
        worker_children.append(nid)

    for human in humans:
        hid = human["entity_id"]
        nid = f"h_{hid}"
        nodes[nid] = {
            "node_id": nid,
            "name": f"HumanMonitor [{hid}]",
            "type": "human_monitor",
            "entity": hid,
        }
        worker_children.append(nid)

    # GoalConfirmationGate: add when mission_goal.requires_confirmation is true
    if mission_goal and mission_goal.get("requires_confirmation"):
        nodes["goal_confirm"] = {
            "node_id": "goal_confirm",
            "name": "Goal Confirmation",
            "type": "goal_confirmation",
            "entity": primary_supervisor,
            "timeout_sec": 120.0,
        }
        worker_children.append("goal_confirm")

    if not worker_children:
        nodes["w_placeholder"] = {
            "node_id": "w_placeholder",
            "name": "Placeholder Worker",
            "type": "action",
            "intent": "wait",
            "entity": "system",
            "params": {"duration_sec": 1},
        }
        worker_children.append("w_placeholder")

    nodes["par_workers"] = {
        "node_id": "par_workers",
        "name": "Workers (Parallel)",
        "type": "parallel",
        "policy": "success_on_all",
        "children": worker_children,
    }

    # ── Safety fallback: always-succeed wait ──────────────────────────────────
    nodes["wait_idle"] = {
        "node_id": "wait_idle",
        "name": "Wait (idle tick)",
        "type": "action",
        "intent": "wait",
        "entity": "system",
        "params": {"duration_sec": 1},
    }

    return {
        "tree_id": task_id,
        "root_id": "root_repeat",
        "metadata": {
            "builder": "bt_template_builder",
            "robot_count": len(robots),
            "human_count": len(humans),
        },
        "nodes": nodes,
    }


# ── LangGraph node ────────────────────────────────────────────────────────────

def bt_template_builder_node(state: GenerationState) -> dict:
    """LangGraph node: generate a fixed generic HMTA BT without LLM."""
    task_id = state.get("task_id", "hmta_generic")
    entities = state.get("entities", [])
    mission_goal = state.get("mission_goal")

    logger.info("[%s] bt_template_builder started — %d entities", task_id, len(entities))
    publish_step("bt_template_builder", "started", message="生成通用 HMTA 行为树模板…")

    bt_json = _build_generic_bt(task_id=task_id, entities=entities, mission_goal=mission_goal)

    node_count = len(bt_json.get("nodes", {}))
    logger.info("[%s] bt_template_builder done — %d nodes", task_id, node_count)
    publish_step(
        "bt_template_builder", "completed",
        message=f"通用 BT 已生成: {node_count} 个节点, {len(entities)} 个实体",
    )

    return {
        "behavior_tree": bt_json,
        "generation_trace": [
            *state.get("generation_trace", []),
            {
                "node": "bt_template_builder",
                "node_count": node_count,
                "entity_count": len(entities),
            },
        ],
    }
