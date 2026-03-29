"""Tests for the HMTA service entrypoint control flow."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app import main


class FakeBridge:
    def __init__(self) -> None:
        self.errors: list[tuple[str, str]] = []
        self.results: list[tuple[str, dict]] = []
        self.progress: list[tuple[str, str, str]] = []

    def publish_generation_error(self, task_id: str, message: str) -> None:
        self.errors.append((task_id, message))

    def publish_generation_result(self, task_id: str, payload: dict) -> None:
        self.results.append((task_id, payload))

    def publish_progress(self, task_id: str, step: str, status: str, **kwargs) -> None:
        self.progress.append((task_id, step, status))


def test_handle_generation_request_uses_threadsafe_handoff(monkeypatch: pytest.MonkeyPatch) -> None:
    scheduled: dict[str, object] = {}
    loop = object()
    main._state.main_loop = loop  # type: ignore[assignment]

    def fake_run_coroutine_threadsafe(coro, current_loop):
        scheduled["loop"] = current_loop
        scheduled["is_coroutine"] = asyncio.iscoroutine(coro)
        coro.close()
        future: Future = Future()
        future.set_result(None)
        return future

    monkeypatch.setattr(main.asyncio, "run_coroutine_threadsafe", fake_run_coroutine_threadsafe)

    main._handle_generation_request({"task_id": "threadsafe"})

    assert scheduled["loop"] is loop
    assert scheduled["is_coroutine"] is True


@pytest.mark.asyncio
async def test_run_generation_does_not_load_failed_bt(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_bridge = FakeBridge()
    fake_engine = SimpleNamespace(load=MagicMock())
    monkeypatch.setattr(main._state, "zenoh_bridge", fake_bridge)
    monkeypatch.setattr(main._state, "engine", fake_engine)
    main._state.task_registry.clear()

    class FakeGraph:
        async def ainvoke(self, initial_state, config=None):
            return {
                "behavior_tree": {
                    "tree_id": "bt_failed",
                    "root_id": "root",
                    "nodes": {
                        "root": {
                            "node_id": "root",
                            "type": "sequence",
                            "name": "Root",
                            "children": [],
                        }
                    },
                },
                "validation_report": {
                    "validation_result": "FAILED",
                    "violations": [{"rule": "empty_composite"}],
                },
                "generation_trace": [{"step": "validator", "validation_result": "FAILED"}],
            }

    monkeypatch.setattr(main, "get_graph", lambda: FakeGraph())

    record = await main._run_generation(
        {
            "task_id": "failed_task",
            "entities": [],
            "environment": {},
            "task_context": {},
            "options": {"max_iterations": 1},
        }
    )

    assert record.status == "failed"
    assert record.validation_report["validation_result"] == "FAILED"
    assert fake_engine.load.call_count == 0
    assert fake_bridge.results == []
    assert fake_bridge.errors
    assert main._state.task_registry["failed_task"]["status"] == "failed"
