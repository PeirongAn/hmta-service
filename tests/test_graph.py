"""Integration test for the LangGraph generation pipeline (mocked LLM)."""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch

from app.generation.service import build_initial_state
from app.schemas.api import GenerationRequest


ENTITIES = [
    {
        "entity_id": "robot_1",
        "entity_type": "robot",
        "display_name": "Scout Robot",
        "status": "idle",
        "comm_status": "online",
        "capabilities": [
            {"name": "navigation.move_to"},
            {"name": "perception.scan_zone"},
        ],
        "extensions": {},
    },
]

ENVIRONMENT = {
    "map_id": "test_map",
    "zones": {"zone_a": {"type": "field", "area_sqm": 100}},
    "obstacles": [],
}

TASK_CONTEXT = {
    "task_id": "test_task_001",
    "objective": "Scout zone_a",
    "doctrine": "",
    "constraints": [],
    "priority": "normal",
}

MOCK_TASK_PLAN = {
    "phases": [{"phase_id": "p1", "name": "Scout", "description": "Move and scan", "subtask_ids": ["st1"]}],
    "subtasks": [
        {
            "subtask_id": "st1",
            "description": "Move to zone_a and scan",
            "assigned_entity_ids": ["robot_1"],
            "required_capabilities": ["navigation.move_to", "perception.scan_zone"],
            "dependencies": [],
        }
    ],
    "constraints": [],
    "doctrine_notes": "",
}

MOCK_BT = {
    "tree_id": "bt_test_task_001",
    "root_id": "seq_root",
    "nodes": {
        "seq_root": {
            "node_id": "seq_root",
            "type": "sequence",
            "name": "Scout Zone A",
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


@pytest.mark.asyncio
async def test_full_generation_pipeline():
    """End-to-end test with mocked LLM chains."""
    from app.generation.graph import coordinator

    # Mock the two LLM-backed chains
    with (
        patch("app.generation.graph.task_planner._build_chain") as mock_planner_chain,
        patch("app.generation.graph.bt_builder._build_chain") as mock_builder_chain,
    ):
        mock_planner_chain.return_value = AsyncMock(
            ainvoke=AsyncMock(return_value=MOCK_TASK_PLAN)
        )
        mock_builder_chain.return_value = AsyncMock(
            ainvoke=AsyncMock(return_value=MOCK_BT)
        )

        # Force rebuild graph with fresh mocks
        coordinator._graph = None
        graph = coordinator.get_graph()

        initial = {
            "task_id": "test_task_001",
            "entities": ENTITIES,
            "environment": ENVIRONMENT,
            "task_context": TASK_CONTEXT,
            "iteration_count": 0,
            "max_iterations": 3,
            "llm_model": "gpt-4o",
            "generation_trace": [],
            "error": None,
        }

        result = await graph.ainvoke(initial)

    assert result["behavior_tree"] is not None
    assert result["behavior_tree"]["root_id"] == "seq_root"
    assert result["validation_report"]["validation_result"] == "PASSED"
    assert result["fsm_definitions"] is not None
    assert len(result["fsm_definitions"]) == len(ENTITIES)
    assert result["blackboard_init"] is not None
    assert len(result["generation_trace"]) >= 3   # planner, builder, validator, fsm_bb_init


@pytest.mark.asyncio
async def test_validator_triggers_repair_then_passes():
    """Test retry loop: first BT fails validation, second BT passes."""
    from app.generation.graph import coordinator

    call_count = {"n": 0}

    BAD_BT = {
        "tree_id": "bt_bad",
        "root_id": "seq_root",
        "nodes": {
            "seq_root": {
                "node_id": "seq_root",
                "type": "sequence",
                "name": "Bad BT",
                "children": ["action_wrong"],
            },
            "action_wrong": {
                "node_id": "action_wrong",
                "type": "action",
                "name": "Wrong action",
                "children": [],
                "intent": "manipulation.disarm",   # robot_1 lacks this → FAILED
                "entity": "robot_1",
                "params": {},
            },
        },
    }

    async def bt_side_effect(context):
        call_count["n"] += 1
        return BAD_BT if call_count["n"] == 1 else MOCK_BT

    with (
        patch("app.generation.graph.task_planner._build_chain") as mock_planner_chain,
        patch("app.generation.graph.bt_builder._build_chain") as mock_builder_chain,
    ):
        mock_planner_chain.return_value = AsyncMock(ainvoke=AsyncMock(return_value=MOCK_TASK_PLAN))
        mock_builder_chain.return_value = AsyncMock(ainvoke=AsyncMock(side_effect=bt_side_effect))

        coordinator._graph = None
        graph = coordinator.get_graph()

        initial = {
            "task_id": "repair_test",
            "entities": ENTITIES,
            "environment": ENVIRONMENT,
            "task_context": TASK_CONTEXT,
            "iteration_count": 0,
            "max_iterations": 3,
            "llm_model": "gpt-4o",
            "generation_trace": [],
            "error": None,
        }

        result = await graph.ainvoke(initial)

    assert call_count["n"] == 2   # bt_builder called twice (build + repair)
    assert result["validation_report"]["validation_result"] == "PASSED"


@pytest.mark.asyncio
async def test_global_llm_override_applies_to_planner_builder_and_fixer():
    """A single request-level llm_model should flow through all LLM-backed nodes."""
    from app.generation.graph import coordinator

    call_count = {"n": 0}

    BAD_BT = {
        "tree_id": "bt_bad",
        "root_id": "seq_root",
        "nodes": {
            "seq_root": {
                "node_id": "seq_root",
                "type": "sequence",
                "name": "Bad BT",
                "children": ["action_wrong"],
            },
            "action_wrong": {
                "node_id": "action_wrong",
                "type": "action",
                "name": "Wrong action",
                "children": [],
                "intent": "manipulation.disarm",
                "entity": "robot_1",
                "params": {},
            },
        },
    }

    async def bt_side_effect(context):
        call_count["n"] += 1
        return BAD_BT if call_count["n"] == 1 else MOCK_BT

    with (
        patch("app.generation.graph.task_planner._build_chain") as mock_planner_chain,
        patch("app.generation.graph.bt_builder._build_chain") as mock_builder_chain,
    ):
        mock_planner_chain.return_value = AsyncMock(ainvoke=AsyncMock(return_value=MOCK_TASK_PLAN))
        mock_builder_chain.return_value = AsyncMock(ainvoke=AsyncMock(side_effect=bt_side_effect))

        coordinator._graph = None
        graph = coordinator.get_graph()
        initial = build_initial_state(
            GenerationRequest(
                task_id="override_test",
                entities=ENTITIES,
                environment=ENVIRONMENT,
                task_context=TASK_CONTEXT,
                options={"llm_model": "override-model", "max_iterations": 3},
            ),
            "override_test",
        )

        result = await graph.ainvoke(initial)

    assert result["validation_report"]["validation_result"] == "PASSED"
    assert mock_planner_chain.call_args.args[0] == "override-model"
    assert mock_builder_chain.call_args_list[0].args[0] == "override-model"
    assert mock_builder_chain.call_args_list[1].args[0] == "override-model"
