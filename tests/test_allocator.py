"""T5 — Quantitative allocator tests."""

from __future__ import annotations

import pytest

from app.capability.allocator import allocator_node


def _make_state(
    entities: list[dict] | None = None,
    task_plan: dict | None = None,
) -> dict:
    default_entities = [
        {
            "entity_id": "robot_A",
            "type": "robot",
            "status": "idle",
            "battery": 80,
            "capabilities": ["move", "detect"],
            "structured_capabilities": [
                {"name": "move", "mode": "autonomous", "proficiency": 0.9},
                {"name": "detect", "mode": "supervised", "proficiency": 0.7},
            ],
        },
        {
            "entity_id": "robot_B",
            "type": "robot",
            "status": "idle",
            "battery": 60,
            "capabilities": ["move"],
            "structured_capabilities": [
                {"name": "move", "mode": "remote_control", "proficiency": 0.5},
            ],
        },
    ]
    default_plan = {
        "subtasks": [
            {"task_id": "t1", "name": "Patrol zone A", "required_capabilities": ["move"]},
            {"task_id": "t2", "name": "Scan zone B", "required_capabilities": ["detect"]},
        ]
    }
    return {
        "task_id": "test",
        "entities": entities or default_entities,
        "task_plan": task_plan or default_plan,
        "environment": {},
        "generation_trace": [],
        "allocation_trace": [],
    }


class TestBasicAllocation:
    def test_assigns_entities(self):
        result = allocator_node(_make_state())
        plan = result["task_plan"]
        for st in plan["subtasks"]:
            assert "assigned_entity_ids" in st
            assert len(st["assigned_entity_ids"]) > 0

    def test_higher_proficiency_preferred(self):
        result = allocator_node(_make_state())
        plan = result["task_plan"]
        move_task = [s for s in plan["subtasks"] if s["task_id"] == "t1"][0]
        assert "robot_A" in move_task["assigned_entity_ids"]

    def test_allocation_trace(self):
        result = allocator_node(_make_state())
        assert len(result["allocation_trace"]) == 2
        for entry in result["allocation_trace"]:
            assert "subtask_id" in entry
            assert "robot_candidates" in entry
            assert "robot_scores" in entry


class TestHardConstraints:
    def test_missing_capability_filtered(self):
        result = allocator_node(_make_state())
        plan = result["task_plan"]
        detect_task = [s for s in plan["subtasks"] if s["task_id"] == "t2"][0]
        assert "robot_B" not in detect_task["assigned_entity_ids"]

    def test_offline_entity_filtered(self):
        entities = [
            {
                "entity_id": "robot_A",
                "type": "robot",
                "status": "offline",
                "capabilities": ["move"],
                "structured_capabilities": [{"name": "move", "proficiency": 0.9}],
            },
            {
                "entity_id": "robot_B",
                "type": "robot",
                "status": "idle",
                "capabilities": ["move"],
                "structured_capabilities": [{"name": "move", "proficiency": 0.5}],
            },
        ]
        plan = {"subtasks": [{"task_id": "t1", "required_capabilities": ["move"]}]}
        result = allocator_node(_make_state(entities=entities, task_plan=plan))
        st = result["task_plan"]["subtasks"][0]
        assert "robot_A" not in st["assigned_entity_ids"]


class TestNoTaskPlan:
    def test_no_plan_returns_empty(self):
        state = _make_state()
        state["task_plan"] = None
        result = allocator_node(state)
        assert result == {}


class TestCapabilityGraph:
    def test_graph_snapshot_in_output(self):
        result = allocator_node(_make_state())
        assert "capability_graph" in result
        assert "nodes" in result["capability_graph"]
