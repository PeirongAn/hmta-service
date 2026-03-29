"""Tests for HTTP fallback API handlers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.api import health
from app.schemas.api import GenerationRequest


def _make_request(state) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=state))


@pytest.mark.asyncio
async def test_generate_http_returns_typed_task_record(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_engine = SimpleNamespace(load=MagicMock())
    state = SimpleNamespace(
        zenoh_bridge=SimpleNamespace(session=None),
        engine=fake_engine,
        fsm_manager=SimpleNamespace(instances={}),
        task_registry={},
    )

    class FakeGraph:
        async def ainvoke(self, initial_state):
            return {
                "behavior_tree": {
                    "tree_id": "bt_http",
                    "root_id": "root",
                    "nodes": {
                        "root": {
                            "node_id": "root",
                            "type": "sequence",
                            "name": "Root",
                            "children": ["action_1"],
                        },
                        "action_1": {
                            "node_id": "action_1",
                            "type": "action",
                            "name": "Act",
                            "children": [],
                            "intent": "navigation.move_to",
                            "entity": "robot_1",
                            "params": {},
                        },
                    },
                },
                "fsm_definitions": [],
                "blackboard_init": {"entries": {}},
                "validation_report": {"validation_result": "PASSED", "violations": []},
                "generation_trace": [],
            }

    monkeypatch.setattr(health, "get_graph", lambda: FakeGraph())

    record = await health.generate_http(
        GenerationRequest(task_id="http_task", entities=[], environment={}, task_context={}),
        _make_request(state),
    )

    assert record.task_id == "http_task"
    assert record.status == "loaded"
    assert record.execution_mode == "load_only"
    assert state.task_registry["http_task"]["task_id"] == "http_task"
    assert fake_engine.load.call_count == 1


@pytest.mark.asyncio
async def test_list_tasks_preserves_task_ids() -> None:
    state = SimpleNamespace(
        zenoh_bridge=SimpleNamespace(session=None),
        engine=None,
        fsm_manager=SimpleNamespace(instances={}),
        task_registry={
            "task_1": {
                "task_id": "task_1",
                "status": "failed",
                "validation_report": {},
                "generation_trace": [],
                "fsm_definitions": [],
                "blackboard_init": {},
                "execution_mode": "load_only",
                "error": "boom",
            }
        },
    )

    response = await health.list_tasks(_make_request(state))

    assert len(response.tasks) == 1
    assert response.tasks[0].task_id == "task_1"
