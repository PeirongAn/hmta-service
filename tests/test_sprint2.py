"""Sprint 2 tests — ExperimentController, allocation override, reallocation, metrics."""

from __future__ import annotations

import pytest


# ── ExperimentController ──────────────────────────────────────────────────────

class TestExperimentController:
    def _make_controller(self):
        from app.experiment.controller import (
            ExperimentController, ExperimentPlan, ExperimentTrial,
        )
        ctrl = ExperimentController()
        trials = [
            ExperimentTrial(
                subtask_match={"capability": "detect"},
                forced_collaboration="partner",
                forced_bt_pattern="human_plan_execute",
                description="force partner mode on detect tasks",
            ),
            ExperimentTrial(
                subtask_match={"capability": "navigate"},
                forced_collaboration="task_based",
                description="force autonomous on navigate tasks",
            ),
            ExperimentTrial(
                subtask_match={},
                forced_collaboration="proxy",
                description="fallback: force proxy on anything else",
            ),
        ]
        plan = ExperimentPlan(name="bomb_search_test", trials=trials, repeat_count=1)
        ctrl.load_plan(plan)
        ctrl.start()
        return ctrl

    def test_load_and_start(self):
        ctrl = self._make_controller()
        status = ctrl.get_status()
        assert status["status"] == "running"
        assert status["trials_total"] == 3
        assert status["trials_pending"] == 3

    def test_override_matching(self):
        ctrl = self._make_controller()
        subtask_detect = {
            "task_id": "st-01",
            "required_capabilities": [{"name": "detect"}],
        }
        override = ctrl.get_override_for_subtask(subtask_detect)
        assert override is not None
        assert override["collaboration"] == "partner"
        assert override["bt_pattern"] == "human_plan_execute"

    def test_override_no_match_uses_fallback(self):
        ctrl = self._make_controller()
        subtask_other = {
            "task_id": "st-99",
            "required_capabilities": [{"name": "something_else"}],
        }
        # "detect" trial won't match; "navigate" won't match; empty match → proxy
        override = ctrl.get_override_for_subtask(subtask_other)
        assert override is not None
        assert override["collaboration"] == "proxy"

    def test_trial_status_updates(self):
        ctrl = self._make_controller()
        subtask = {"task_id": "st-01", "required_capabilities": [{"name": "detect"}]}
        override = ctrl.get_override_for_subtask(subtask)
        trial_id = override["trial_id"]
        ctrl.mark_trial_complete(trial_id, success=True)
        status = ctrl.get_status()
        assert status["trials_completed"] == 1
        assert status["trials_pending"] == 2

    def test_plan_completion(self):
        ctrl = self._make_controller()
        # Drain all trials — each call to get_override consumes the first pending trial
        # We need 3 calls since there are 3 trials; each with an empty-match subtask
        # so that trials match in order: detect, navigate, fallback
        subtasks = [
            {"task_id": "st-0", "required_capabilities": [{"name": "detect"}]},
            {"task_id": "st-1", "required_capabilities": [{"name": "navigate"}]},
            {"task_id": "st-2", "required_capabilities": [{"name": "other"}]},
        ]
        for st in subtasks:
            ov = ctrl.get_override_for_subtask(st)
            assert ov is not None, f"Expected override for {st['task_id']}"
            ctrl.mark_trial_complete(ov["trial_id"])
        status = ctrl.get_status()
        assert status["status"] == "completed"

    def test_abort(self):
        ctrl = self._make_controller()
        ctrl.abort()
        assert ctrl.active_plan is None
        subtask = {"task_id": "st-01", "required_capabilities": []}
        assert ctrl.get_override_for_subtask(subtask) is None

    def test_repeat_count(self):
        from app.experiment.controller import (
            ExperimentController, ExperimentPlan, ExperimentTrial,
        )
        ctrl = ExperimentController()
        plan = ExperimentPlan(
            name="repeat_test",
            trials=[ExperimentTrial(subtask_match={}, forced_collaboration="partner")],
            repeat_count=2,
        )
        ctrl.load_plan(plan)
        ctrl.start()

        # Complete first round
        ov = ctrl.get_override_for_subtask({"task_id": "a", "required_capabilities": []})
        ctrl.mark_trial_complete(ov["trial_id"])
        status = ctrl.get_status()
        assert status["status"] == "running"
        assert status["current_repeat"] == 1

        # Complete second round
        ov2 = ctrl.get_override_for_subtask({"task_id": "b", "required_capabilities": []})
        ctrl.mark_trial_complete(ov2["trial_id"])
        status = ctrl.get_status()
        assert status["status"] == "completed"


# ── AllocationQualityMetrics ──────────────────────────────────────────────────

class TestAllocationMetrics:
    def test_empty_trace(self):
        from app.capability.allocation_metrics import compute_allocation_quality
        m = compute_allocation_quality([])
        assert m.coverage_ratio == 0.0
        assert m.avg_score == 0.0

    def test_basic_metrics(self):
        from app.capability.allocation_metrics import compute_allocation_quality
        trace = [
            {
                "subtask_id": "st-1",
                "assigned": ["robot-1"],
                "collaboration": "task_based",
                "robot_scores": [{"entity_id": "robot-1", "total": 0.85}],
            },
            {
                "subtask_id": "st-2",
                "assigned": ["robot-2"],
                "collaboration": "partner",
                "robot_scores": [{"entity_id": "robot-2", "total": 0.70}],
            },
            {
                "subtask_id": "st-3",
                "assigned": [],
                "collaboration": "none",
                "robot_scores": [],
                "reason": "no_candidates",
            },
        ]
        m = compute_allocation_quality(trace)
        assert m.coverage_ratio == pytest.approx(2 / 3, abs=0.01)
        assert m.avg_score == pytest.approx(0.775, abs=0.01)
        assert m.mode_distribution["task_based"] == 1
        assert m.mode_distribution["partner"] == 1

    def test_attention_utilization(self):
        from app.capability.allocation_metrics import compute_allocation_quality
        trace = [{"subtask_id": "s1", "assigned": ["r1"], "collaboration": "partner", "robot_scores": []}]
        att = {"op-01": 0.3, "op-02": 0.1}
        m = compute_allocation_quality(trace, attention_summary=att, attention_budget=1.0)
        assert m.attention_utilization == pytest.approx(0.4, abs=0.01)

    def test_to_dict(self):
        from app.capability.allocation_metrics import AllocationQualityMetrics
        m = AllocationQualityMetrics(coverage_ratio=0.8, avg_score=0.75)
        d = m.to_dict()
        assert d["coverage_ratio"] == 0.8
        assert isinstance(d, dict)


# ── Allocator experiment override integration ─────────────────────────────────

class TestAllocatorOverride:
    def test_set_experiment_controller(self):
        from app.capability.allocator import set_experiment_controller, _experiment_controller
        from app.experiment.controller import ExperimentController
        ctrl = ExperimentController()
        set_experiment_controller(ctrl)
        # Module-level global should be set
        from app.capability import allocator
        assert allocator._experiment_controller is ctrl
        set_experiment_controller(None)

    def test_reallocate_subtask_basic(self):
        from app.capability.allocator import reallocate_subtask
        from app.capability.hypergraph import HEdge, HNode, HyperGraph

        graph = HyperGraph()
        graph.add_node(HNode(id="r1", kind="entity", attrs={"entity_type": "robot", "status": "online"}))
        graph.add_node(HNode(id="r2", kind="entity", attrs={"entity_type": "robot", "status": "online"}))
        graph.add_node(HNode(id="detect", kind="capability", attrs={}))
        graph.add_edge(HEdge(id="hc_r1_detect", kind="has_capability", nodes=frozenset(["r1", "detect"]), weight=0.9))
        graph.add_edge(HEdge(id="hc_r2_detect", kind="has_capability", nodes=frozenset(["r2", "detect"]), weight=0.8))

        subtask = {"task_id": "st-1", "required_capabilities": ["detect"]}
        entities = [
            {"entity_id": "r1", "status": "online", "capabilities": [{"name": "detect"}]},
            {"entity_id": "r2", "status": "online", "capabilities": [{"name": "detect"}]},
        ]

        result = reallocate_subtask(subtask, entities, graph, {}, exclude_entities=["r1"])
        assert result is not None
        assert result["assigned_entity_ids"] == ["r2"]
        assert result["reallocated"] is True

    def test_reallocate_no_candidates(self):
        from app.capability.allocator import reallocate_subtask
        from app.capability.hypergraph import HNode, HyperGraph

        graph = HyperGraph()
        graph.add_node(HNode(id="r1", kind="entity", attrs={"entity_type": "robot"}))
        subtask = {"task_id": "st-1", "required_capabilities": ["detect"]}
        entities = [{"entity_id": "r1", "status": "online"}]
        result = reallocate_subtask(subtask, entities, graph, {}, exclude_entities=["r1"])
        assert result is None


# ── Engine reallocation triggers ──────────────────────────────────────────────

class TestEngineReallocation:
    def test_reallocation_callback_set(self):
        """Engine accepts a reallocation callback without error."""
        from unittest.mock import MagicMock
        from app.execution.engine import ExecutionEngine

        mock_zenoh = MagicMock()
        mock_cmd = MagicMock()
        mock_fsm = MagicMock()
        engine = ExecutionEngine(mock_zenoh, mock_cmd, mock_fsm)
        callback = MagicMock()
        engine.set_reallocation_callback(callback)
        assert engine._reallocation_callback is callback


# ── API import checks ─────────────────────────────────────────────────────────

class TestAPIImports:
    def test_experiment_api_import(self):
        from app.api.experiment import experiment_router
        assert experiment_router is not None

    def test_experiment_router_routes(self):
        from app.api.experiment import experiment_router
        paths = [r.path for r in experiment_router.routes]
        assert "/api/v1/experiment/plan" in paths
        assert "/api/v1/experiment/status" in paths
        assert "/api/v1/experiment/data" in paths
        assert "/api/v1/experiment/export" in paths
        assert "/api/v1/experiment/count" in paths
        assert "/api/v1/experiment/abort" in paths
