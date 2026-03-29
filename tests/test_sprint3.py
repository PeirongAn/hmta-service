"""Sprint 3 tests — BoundaryLearner, bayesian_update, allocator optimal_x, learning API."""

from __future__ import annotations

import random
import tempfile
from pathlib import Path

import pytest


def _make_store_with_data(n: int = 50, db_path=None):
    """Create a store and populate with synthetic experiment records."""
    from app.experiment.store import ExperimentRecord, ExperimentStore

    store = ExperimentStore(db_path=db_path or Path(tempfile.mktemp(suffix=".db")))
    random.seed(42)

    for i in range(n):
        mode_idx = random.choice([1, 2, 3, 4, 5])
        x = random.uniform(*{1: (0, 0.1), 2: (0.1, 0.3), 3: (0.3, 0.5), 4: (0.5, 0.8), 5: (0.8, 1.0)}[mode_idx])
        complexity = random.uniform(0, 1)
        urgency = random.uniform(0, 1)
        risk = random.uniform(0, 1)

        # Synthetic performance: higher x + lower complexity → better performance
        perf = 0.5 + 0.3 * x - 0.2 * complexity + 0.1 * random.gauss(0, 1)
        perf = max(0, min(1, perf))
        safety = max(0.5, min(1.0, 0.95 - 0.1 * risk + 0.05 * random.gauss(0, 1)))

        store.save(ExperimentRecord(
            task_id=f"task-{i // 5}",
            subtask_id=f"st-{i}",
            timestamp=float(i),
            complexity=complexity,
            urgency=urgency,
            risk=risk,
            ambiguity=random.uniform(0, 0.5),
            time_pressure=random.uniform(0, 1),
            cognitive_switch_cost=random.uniform(0, 0.3),
            collaboration_mode=["task_based", "task_based", "partner", "partner", "proxy"][mode_idx - 1],
            collaboration_mode_idx=mode_idx,
            bt_pattern=["autonomous", "autonomous", "human_plan_execute", "human_plan_execute", "human_plan_execute"][mode_idx - 1],
            human_involvement=x,
            human_supervisor="operator-01" if mode_idx > 2 else None,
            assigned_robot="robot-01",
            performance_obj=perf,
            safety_score=safety,
            outcome_success=perf > 0.4,
            actual_duration_ms=random.uniform(5000, 30000),
            primary_capability="detect",
            required_capabilities=["detect"],
            robot_proficiency=0.85,
        ))
    return store


class TestBoundaryLearner:
    def test_fit_piecewise_linear(self):
        from app.experiment.learner import BoundaryLearner
        store = _make_store_with_data(50)
        learner = BoundaryLearner(store)
        result = learner.fit_piecewise_linear()
        assert result.sample_count == 50
        assert len(result.segments) > 0
        assert result.overall_r_squared is not None

    def test_fit_with_insufficient_data(self):
        from app.experiment.learner import BoundaryLearner
        store = _make_store_with_data(0)
        learner = BoundaryLearner(store)
        result = learner.fit_piecewise_linear()
        assert result.sample_count == 0
        assert len(result.segments) == 0

    def test_predict(self):
        from app.experiment.learner import BoundaryLearner
        store = _make_store_with_data(50)
        learner = BoundaryLearner(store)
        learner.fit_piecewise_linear()
        features = {"complexity": 0.5, "urgency": 0.3, "risk": 0.2}
        pred = learner.predict(features, x=0.5, mode_idx=4)
        assert isinstance(pred, float)

    def test_predict_unfitted_mode(self):
        from app.experiment.learner import BoundaryLearner
        store = _make_store_with_data(50)
        learner = BoundaryLearner(store)
        learner.fit_piecewise_linear()
        pred = learner.predict({}, x=0.5, mode_idx=99)
        assert pred == 0.5

    def test_suggest_optimal_x(self):
        from app.experiment.learner import BoundaryLearner
        store = _make_store_with_data(50)
        learner = BoundaryLearner(store)
        learner.fit_piecewise_linear()
        result = learner.suggest_optimal_x_constrained(
            {"complexity": 0.5, "urgency": 0.3, "risk": 0.2},
            safety_threshold=0.8,
        )
        assert 0 <= result.optimal_x <= 1
        assert isinstance(result.predicted_obj, float)
        assert isinstance(result.safety_feasible, bool)

    def test_boundary_equation(self):
        from app.experiment.learner import BoundaryLearner
        store = _make_store_with_data(50)
        learner = BoundaryLearner(store)
        learner.fit_piecewise_linear()
        eq = learner.get_boundary_equation(1)
        assert "mode_idx" in eq
        assert isinstance(eq.get("equation", ""), str)

    def test_export_weights(self):
        from app.experiment.learner import BoundaryLearner
        store = _make_store_with_data(50)
        learner = BoundaryLearner(store)
        learner.fit_piecewise_linear()
        weights = learner.export_weights()
        assert isinstance(weights, dict)
        assert len(weights) > 0

    def test_heatmap_data(self):
        from app.experiment.learner import BoundaryLearner
        store = _make_store_with_data(50)
        learner = BoundaryLearner(store)
        learner.fit_piecewise_linear()
        hm = learner.generate_heatmap_data(resolution=5)
        assert hm["dim_x"] == "complexity"
        assert hm["dim_y"] == "urgency"
        assert len(hm["z_optimal_x"]) == 5
        assert len(hm["z_optimal_x"][0]) == 5


class TestWriteBackToGraph:
    def test_write_back_updates_edges(self):
        from app.capability.hypergraph import HEdge, HNode, HyperGraph
        from app.capability.registry import CapabilityRegistry
        from app.experiment.learner import BoundaryLearner

        store = _make_store_with_data(50)

        registry = CapabilityRegistry()
        registry.register_entity({
            "entity_id": "robot-01",
            "entity_type": "robot",
            "structured_capabilities": [{"name": "detect", "proficiency": 0.8}],
        })

        learner = BoundaryLearner(store)
        learner.fit_piecewise_linear()
        updates = learner.write_back_to_graph(registry)

        assert isinstance(updates, dict)
        graph = registry.get_graph_ref()
        edge = graph.edges.get("hc_robot-01_detect")
        if edge and "robot-01/detect" in updates:
            assert "optimal_x" in edge.attrs
            assert "optimal_x_confidence" in edge.attrs
            assert "last_learned_at" in edge.attrs

    def test_write_back_empty_store(self):
        from app.capability.registry import CapabilityRegistry
        from app.experiment.learner import BoundaryLearner

        store = _make_store_with_data(0)
        registry = CapabilityRegistry()
        learner = BoundaryLearner(store)
        updates = learner.write_back_to_graph(registry)
        assert updates == {}


class TestWriteBackToOntology:
    def test_write_and_read_yaml(self):
        import yaml
        from app.experiment.learner import BoundaryLearner

        store = _make_store_with_data(50)
        learner = BoundaryLearner(store)
        learner.fit_piecewise_linear()

        tmp = Path(tempfile.mktemp(suffix=".yaml"))
        tmp.write_text("capabilities:\n  detect:\n    weight: 1.0\n", encoding="utf-8")

        learner.write_back_to_ontology(tmp)

        with open(tmp, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert "learned_weights" in data
        assert "_overall_r_squared" in data["learned_weights"]


class TestBayesianUpdate:
    def test_insufficient_data_returns_original(self):
        from app.capability.utility import bayesian_update
        weights = {"cap_match": 0.5, "proficiency": 0.3}
        result = bayesian_update(weights, [])
        assert result == weights

    def test_returns_dict(self):
        from app.capability.utility import bayesian_update
        result = bayesian_update({"a": 1.0}, [])
        assert isinstance(result, dict)


class TestAllocatorOptimalX:
    def test_x_to_collaboration_mode(self):
        from app.capability.allocator import _x_to_collaboration_mode
        assert _x_to_collaboration_mode(0.05) == ("task_based", "autonomous")
        assert _x_to_collaboration_mode(0.3) == ("partner", "human_plan_execute")
        assert _x_to_collaboration_mode(0.7) == ("proxy", "human_plan_execute")


class TestLearningAPIs:
    def test_api_imports(self):
        from app.api.experiment import experiment_router
        paths = [r.path for r in experiment_router.routes]
        assert "/api/v1/experiment/learn" in paths
        assert "/api/v1/experiment/boundary" in paths
        assert "/api/v1/experiment/boundary/predict" in paths
        assert "/api/v1/experiment/boundary/heatmap" in paths
        assert "/api/v1/experiment/apply" in paths

    def test_learner_import(self):
        from app.experiment.learner import (
            BoundaryLearner, ConstrainedOptimalX, PiecewiseLinearResult, SegmentResult,
        )
        assert BoundaryLearner is not None
        assert PiecewiseLinearResult is not None
        assert ConstrainedOptimalX is not None
        assert SegmentResult is not None
