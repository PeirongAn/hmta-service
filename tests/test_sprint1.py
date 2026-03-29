"""Sprint 1 verification — ExperimentStore, PerformanceCollector, capability API."""

from __future__ import annotations

import tempfile
import time
from pathlib import Path

import pytest

from app.experiment.store import ExperimentRecord, ExperimentStore
from app.experiment.collector import PerformanceCollector


# ---------------------------------------------------------------------------
# ExperimentStore
# ---------------------------------------------------------------------------

class TestExperimentStore:
    def _make_store(self, tmp_path: Path) -> ExperimentStore:
        return ExperimentStore(db_path=tmp_path / "test.db")

    def test_save_and_count(self, tmp_path):
        store = self._make_store(tmp_path)
        assert store.count() == 0
        store.save(ExperimentRecord(task_id="t1", subtask_id="s1"))
        assert store.count() == 1

    def test_query_all_returns_records(self, tmp_path):
        store = self._make_store(tmp_path)
        store.save(ExperimentRecord(task_id="t1", subtask_id="s1", complexity=0.5))
        store.save(ExperimentRecord(task_id="t1", subtask_id="s2", complexity=0.8))
        records = store.query_all()
        assert len(records) == 2
        assert records[0].complexity == pytest.approx(0.5)

    def test_query_by_capability(self, tmp_path):
        store = self._make_store(tmp_path)
        store.save(ExperimentRecord(task_id="t1", subtask_id="s1", primary_capability="patrol"))
        store.save(ExperimentRecord(task_id="t1", subtask_id="s2", primary_capability="detect"))
        assert len(store.query_by_capability("patrol")) == 1
        assert len(store.query_by_capability("detect")) == 1
        assert len(store.query_by_capability("move")) == 0

    def test_query_by_task(self, tmp_path):
        store = self._make_store(tmp_path)
        store.save(ExperimentRecord(task_id="t1", subtask_id="s1"))
        store.save(ExperimentRecord(task_id="t2", subtask_id="s2"))
        assert len(store.query_by_task("t1")) == 1

    def test_boolean_roundtrip(self, tmp_path):
        store = self._make_store(tmp_path)
        store.save(ExperimentRecord(
            task_id="t1", subtask_id="s1",
            needs_human_input=True, outcome_success=True, resource_feasible=False,
        ))
        r = store.query_all()[0]
        assert r.needs_human_input is True
        assert r.outcome_success is True
        assert r.resource_feasible is False

    def test_list_roundtrip(self, tmp_path):
        store = self._make_store(tmp_path)
        store.save(ExperimentRecord(
            task_id="t1", subtask_id="s1",
            required_capabilities=["patrol", "detect"],
        ))
        r = store.query_all()[0]
        assert r.required_capabilities == ["patrol", "detect"]

    def test_export_csv(self, tmp_path):
        store = self._make_store(tmp_path)
        store.save(ExperimentRecord(task_id="t1", subtask_id="s1"))
        csv_path = tmp_path / "export.csv"
        count = store.export_csv(csv_path)
        assert count == 1
        assert csv_path.exists()
        lines = csv_path.read_text().strip().split("\n")
        assert len(lines) == 2  # header + 1 row

    def test_upsert_on_duplicate(self, tmp_path):
        store = self._make_store(tmp_path)
        r = ExperimentRecord(record_id="dup1", task_id="t1", subtask_id="s1", complexity=0.3)
        store.save(r)
        r.complexity = 0.9
        store.save(r)
        assert store.count() == 1
        assert store.query_all()[0].complexity == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# PerformanceCollector
# ---------------------------------------------------------------------------

class TestPerformanceCollector:
    def _make_collector(self, tmp_path: Path) -> tuple[PerformanceCollector, ExperimentStore]:
        store = ExperimentStore(db_path=tmp_path / "test.db")
        collector = PerformanceCollector(store=store)
        return collector, store

    def _make_final_state(self) -> dict:
        return {
            "task_plan": {
                "subtasks": [
                    {
                        "task_id": "sub1",
                        "required_capabilities": ["patrol"],
                        "priority": "normal",
                        "assigned_entity_ids": ["robot_A"],
                        "interaction": {
                            "collaboration": "task_based",
                            "bt_pattern": "autonomous",
                            "human_supervisor": None,
                            "attention_cost": 0.0,
                        },
                    },
                    {
                        "task_id": "sub2",
                        "required_capabilities": ["detect", "observe"],
                        "priority": "urgent",
                        "assigned_entity_ids": ["robot_B"],
                        "interaction": {
                            "collaboration": "partner",
                            "bt_pattern": "human_approve_execute",
                            "human_supervisor": "op1",
                            "attention_cost": 0.15,
                        },
                    },
                ],
            },
            "entities": [
                {"entity_id": "robot_A", "type": "robot", "battery": 0.9, "structured_capabilities": [{"proficiency": 0.85}]},
                {"entity_id": "robot_B", "type": "robot", "battery": 0.7, "structured_capabilities": [{"proficiency": 0.75}]},
                {"entity_id": "op1", "type": "human", "cognitive_load": 0.3, "fatigue_level": 0.1, "current_task_count": 1, "devices": [{"status": "online"}]},
            ],
            "allocation_trace": [
                {"subtask_id": "sub1"},
                {"subtask_id": "sub2"},
            ],
            "environment": {},
        }

    def test_generation_creates_pending_records(self, tmp_path):
        collector, store = self._make_collector(tmp_path)
        collector.on_generation_complete("task_1", self._make_final_state())
        assert len(collector._pending) == 2
        assert "sub1" in collector._pending
        assert "sub2" in collector._pending

    def test_features_extracted_correctly(self, tmp_path):
        collector, store = self._make_collector(tmp_path)
        collector.on_generation_complete("task_1", self._make_final_state())
        r1 = collector._pending["sub1"]
        assert r1.urgency == pytest.approx(0.3)
        assert r1.collaboration_mode == "task_based"
        assert r1.human_involvement == pytest.approx(0.0)
        assert r1.assigned_robot == "robot_A"

        r2 = collector._pending["sub2"]
        assert r2.urgency == pytest.approx(0.6)
        assert r2.collaboration_mode == "partner"
        assert r2.human_involvement == pytest.approx(0.4)
        assert r2.human_supervisor == "op1"

    def test_cognitive_switch_cost(self, tmp_path):
        collector, store = self._make_collector(tmp_path)
        collector.on_generation_complete("task_1", self._make_final_state())
        r1 = collector._pending["sub1"]
        r2 = collector._pending["sub2"]
        assert r1.cognitive_switch_cost == pytest.approx(0.0)
        assert r2.cognitive_switch_cost > 0  # different caps → nonzero switch cost

    def test_execution_complete_saves_to_store(self, tmp_path):
        collector, store = self._make_collector(tmp_path)
        collector.on_generation_complete("task_1", self._make_final_state())

        profiler_data = {
            "subtasks": {
                "sub1": {"status": "SUCCESS", "duration_ms": 5000},
                "sub2": {"status": "FAILURE", "duration_ms": 12000, "safety_events": 1},
            },
        }
        completed = collector.on_execution_complete("task_1", profiler_data, {})

        assert len(completed) == 2
        assert store.count() == 2
        assert len(collector._pending) == 0

        records = store.query_all()
        sub1 = next(r for r in records if r.subtask_id == "sub1")
        sub2 = next(r for r in records if r.subtask_id == "sub2")

        assert sub1.outcome_success is True
        assert sub1.actual_duration_ms == pytest.approx(5000.0)
        assert sub1.performance_obj > 0

        assert sub2.outcome_success is False
        assert sub2.safety_events == 1
        assert sub2.safety_score < 1.0

    def test_execution_without_profiler_data_still_saves(self, tmp_path):
        collector, store = self._make_collector(tmp_path)
        collector.on_generation_complete("task_1", self._make_final_state())
        completed = collector.on_execution_complete("task_1", {}, {})
        assert len(completed) == 2
        assert store.count() == 2


# ---------------------------------------------------------------------------
# Capability API (unit tests — no live server needed)
# ---------------------------------------------------------------------------

class TestCapabilityAPI:
    def test_api_module_imports(self):
        from app.api.capability import router
        assert len(router.routes) >= 3

    def test_graph_stats_logic(self):
        from app.capability.registry import CapabilityRegistry
        reg = CapabilityRegistry()
        reg.register_entity({
            "entity_id": "robot_A",
            "entity_type": "robot",
            "capabilities": ["move", "detect"],
        })
        graph = reg.get_graph_ref()
        node_kinds: dict[str, int] = {}
        for n in graph.nodes.values():
            node_kinds[n.kind] = node_kinds.get(n.kind, 0) + 1
        assert node_kinds.get("entity", 0) >= 1
        assert node_kinds.get("capability", 0) >= 1
