"""Sprint 4 tests — Constrained GP, device degradation, auto mode, GP APIs."""

from __future__ import annotations

import random
import tempfile
from pathlib import Path

import pytest


def _make_store_with_data(n: int = 60, db_path=None):
    from app.experiment.store import ExperimentRecord, ExperimentStore
    store = ExperimentStore(db_path=db_path or Path(tempfile.mktemp(suffix=".db")))
    random.seed(42)
    for i in range(n):
        mode_idx = random.choice([1, 2, 3, 4, 5])
        x = random.uniform(*{1: (0, 0.1), 2: (0.1, 0.3), 3: (0.3, 0.5), 4: (0.5, 0.8), 5: (0.8, 1.0)}[mode_idx])
        complexity = random.uniform(0, 1)
        risk = random.uniform(0, 1)
        perf = max(0, min(1, 0.5 + 0.3 * x - 0.2 * complexity + 0.1 * random.gauss(0, 1)))
        safety = max(0.5, min(1.0, 0.95 - 0.1 * risk + 0.05 * random.gauss(0, 1)))
        store.save(ExperimentRecord(
            task_id=f"task-{i // 5}", subtask_id=f"st-{i}", timestamp=float(i),
            complexity=complexity, urgency=random.uniform(0, 1), risk=risk,
            ambiguity=random.uniform(0, 0.5), time_pressure=random.uniform(0, 1),
            cognitive_switch_cost=random.uniform(0, 0.3),
            collaboration_mode=["task_based", "task_based", "partner", "partner", "proxy"][mode_idx - 1],
            collaboration_mode_idx=mode_idx,
            bt_pattern=["autonomous", "autonomous", "human_plan_execute", "human_plan_execute", "human_plan_execute"][mode_idx - 1],
            human_involvement=x,
            human_supervisor="operator-01" if mode_idx > 2 else None,
            assigned_robot="robot-01",
            operator_cognitive_load=random.uniform(0.1, 0.7),
            operator_fatigue=random.uniform(0, 0.5),
            performance_obj=perf, safety_score=safety,
            outcome_success=perf > 0.4,
            actual_duration_ms=random.uniform(5000, 30000),
            primary_capability="detect",
            required_capabilities=["detect"],
            robot_proficiency=0.85,
        ))
    return store


class TestConstrainedGP:
    def test_fit_constrained_gp(self):
        from app.experiment.learner import BoundaryLearner
        store = _make_store_with_data(60)
        learner = BoundaryLearner(store)
        learner.fit_piecewise_linear()
        result = learner.fit_constrained_gp()
        assert result.sample_count == 60
        assert len(result.mode_similarity) == 5
        assert all(len(row) == 5 for row in result.mode_similarity)

    def test_suggest_next_experiment(self):
        from app.experiment.learner import BoundaryLearner
        store = _make_store_with_data(60)
        learner = BoundaryLearner(store)
        learner.fit_piecewise_linear()
        learner.fit_constrained_gp()
        suggestion = learner.suggest_next_experiment(safety_threshold=0.8)
        assert 0 <= suggestion.suggested_x <= 1
        assert suggestion.suggested_mode_idx >= 1
        assert isinstance(suggestion.rationale, str)
        assert len(suggestion.rationale) > 0

    def test_boundary_surface(self):
        from app.experiment.learner import BoundaryLearner
        store = _make_store_with_data(60)
        learner = BoundaryLearner(store)
        learner.fit_piecewise_linear()
        learner.fit_constrained_gp()
        surface = learner.get_boundary_surface(resolution=5)
        assert "points" in surface
        assert len(surface["points"]) > 0
        point = surface["points"][0]
        assert "predicted_obj" in point
        assert "safety_probability" in point
        assert "feasible" in point

    def test_uncertainty_map(self):
        from app.experiment.learner import BoundaryLearner
        store = _make_store_with_data(60)
        learner = BoundaryLearner(store)
        learner.fit_piecewise_linear()
        learner.fit_constrained_gp()
        umap = learner.get_uncertainty_map(resolution=5)
        assert "z_obj_mean" in umap
        assert "z_obj_std" in umap
        assert "z_safety_mean" in umap
        assert len(umap["z_obj_mean"]) == 5

    def test_mode_similarity_matrix(self):
        from app.experiment.learner import BoundaryLearner
        store = _make_store_with_data(60)
        learner = BoundaryLearner(store)
        result = learner.get_mode_similarity_matrix()
        assert "matrix" in result
        assert "labels" in result
        assert len(result["matrix"]) == 5
        # Diagonal should be 1.0
        for i in range(5):
            assert result["matrix"][i][i] == pytest.approx(1.0, abs=0.01)


class TestDeviceDegradation:
    def test_channel_quality_online(self):
        from app.capability.hypergraph import HNode
        from app.capability.registry import CapabilityRegistry
        node = HNode(id="d1", kind="device", attrs={"status": "online", "constraints": {"battery": 0.9}})
        assert CapabilityRegistry._channel_quality(node) == 1.0

    def test_channel_quality_low_battery(self):
        from app.capability.hypergraph import HNode
        from app.capability.registry import CapabilityRegistry
        node = HNode(id="d1", kind="device", attrs={"status": "online", "constraints": {"battery": 0.2}})
        assert CapabilityRegistry._channel_quality(node) == 0.7

    def test_channel_quality_critical_battery(self):
        from app.capability.hypergraph import HNode
        from app.capability.registry import CapabilityRegistry
        node = HNode(id="d1", kind="device", attrs={"status": "online", "constraints": {"battery": 0.1}})
        assert CapabilityRegistry._channel_quality(node) == 0.3

    def test_channel_quality_offline(self):
        from app.capability.hypergraph import HNode
        from app.capability.registry import CapabilityRegistry
        node = HNode(id="d1", kind="device", attrs={"status": "offline"})
        assert CapabilityRegistry._channel_quality(node) == 0.0

    def test_provides_edge_weight_degraded(self):
        from app.capability.registry import CapabilityRegistry
        registry = CapabilityRegistry()
        registry.register_human_with_devices({
            "entity_id": "op-01",
            "entity_type": "human",
            "devices": [{
                "device_id": "headset-01",
                "type": "ar_headset",
                "status": "online",
                "constraints": {"battery": 0.2},
                "channels": ["visual", "audio"],
            }],
        })
        graph = registry.get_graph_ref()
        pv_edge = graph.edges.get("pv_headset-01_visual")
        assert pv_edge is not None
        assert pv_edge.weight == pytest.approx(0.7, abs=0.01)

    def test_update_device_status_refreshes_weight(self):
        from app.capability.registry import CapabilityRegistry
        registry = CapabilityRegistry()
        registry.register_human_with_devices({
            "entity_id": "op-01",
            "entity_type": "human",
            "devices": [{
                "device_id": "headset-01",
                "type": "ar_headset",
                "status": "online",
                "channels": ["visual"],
            }],
        })
        graph = registry.get_graph_ref()
        edge_before = graph.edges.get("pv_headset-01_visual")
        assert edge_before.weight == 1.0

        registry.update_device_status("op-01", [
            {"device_id": "headset-01", "status": "online", "battery": 0.1},
        ])
        edge_after = graph.edges.get("pv_headset-01_visual")
        assert edge_after.weight == pytest.approx(0.3, abs=0.01)


class TestChannelWeightedProficiency:
    def test_no_devices_returns_base(self):
        from app.capability.effective_resolver import EffectiveCapabilityResolver
        from app.capability.hypergraph import HNode, HyperGraph
        graph = HyperGraph()
        graph.add_node(HNode(id="op-01", kind="entity", attrs={"entity_type": "human"}))
        resolver = EffectiveCapabilityResolver()
        prof = resolver._channel_weighted_proficiency("op-01", "detect", graph)
        assert prof == resolver._BASE_PROFICIENCY

    def test_degraded_device_lowers_proficiency(self):
        from app.capability.effective_resolver import EffectiveCapabilityResolver
        from app.capability.hypergraph import HEdge, HNode, HyperGraph
        graph = HyperGraph()
        graph.add_node(HNode(id="op-01", kind="entity", attrs={"entity_type": "human"}))
        graph.add_node(HNode(id="d1", kind="device", attrs={"status": "online"}))
        graph.add_node(HNode(id="visual", kind="channel", attrs={}))
        graph.add_edge(HEdge(id="eq_op-01_d1", kind="equips", nodes=frozenset(["op-01", "d1"]), weight=1.0))
        graph.add_edge(HEdge(id="pv_d1_visual", kind="provides", nodes=frozenset(["d1", "visual"]), weight=0.3))
        resolver = EffectiveCapabilityResolver()
        prof = resolver._channel_weighted_proficiency("op-01", "detect", graph)
        assert prof == pytest.approx(0.9 * 0.3, abs=0.01)


class TestAutoMode:
    def test_enable_auto_mode(self):
        from app.experiment.controller import ExperimentController
        from app.experiment.learner import BoundaryLearner
        store = _make_store_with_data(60)
        learner = BoundaryLearner(store)
        learner.fit_piecewise_linear()
        learner.fit_constrained_gp()
        ctrl = ExperimentController()
        result = ctrl.enable_auto_mode(learner, safety_threshold=0.7)
        assert "status" in result
        assert result["status"] in ("auto", "needs_confirmation")
        assert "suggestion" in result

    def test_confirm_pending_no_suggestion(self):
        from app.experiment.controller import ExperimentController
        ctrl = ExperimentController()
        assert ctrl.confirm_pending() is False


class TestGPAPIRoutes:
    def test_gp_routes_registered(self):
        from app.api.experiment import experiment_router
        paths = [r.path for r in experiment_router.routes]
        assert "/api/v1/experiment/gp/fit" in paths
        assert "/api/v1/experiment/gp/suggest" in paths
        assert "/api/v1/experiment/gp/boundary-surface" in paths
        assert "/api/v1/experiment/gp/uncertainty" in paths
        assert "/api/v1/experiment/gp/mode-similarity" in paths
        assert "/api/v1/experiment/auto" in paths
        assert "/api/v1/experiment/auto/confirm" in paths
