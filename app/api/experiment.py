"""Experiment management API — plan upload, status, data export."""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.experiment.controller import ExperimentController, ExperimentPlan, ExperimentTrial
from app.experiment.learner import BoundaryLearner
from app.experiment.store import ExperimentStore

logger = logging.getLogger(__name__)

experiment_router = APIRouter(prefix="/api/v1/experiment", tags=["experiment"])


class TrialSpec(BaseModel):
    subtask_match: dict[str, Any] = Field(default_factory=dict)
    forced_collaboration: str | None = None
    forced_bt_pattern: str | None = None
    forced_human_involvement: float | None = None
    forced_robot: str | None = None
    forced_human: str | None = None
    description: str = ""


class PlanUpload(BaseModel):
    name: str = "unnamed"
    description: str = ""
    trials: list[TrialSpec] = Field(default_factory=list)
    repeat_count: int = 1


class DataQuery(BaseModel):
    capability: str | None = None
    task_id: str | None = None
    limit: int = 500
    offset: int = 0


def _get_controller(request: Request) -> ExperimentController:
    ctrl = getattr(request.app.state, "experiment_controller", None)
    if ctrl is None:
        raise HTTPException(503, "ExperimentController not initialised")
    return ctrl


def _get_store(request: Request) -> ExperimentStore:
    store = getattr(request.app.state, "experiment_store", None)
    if store is None:
        raise HTTPException(503, "ExperimentStore not initialised")
    return store


@experiment_router.post("/plan")
async def upload_plan(body: PlanUpload, request: Request) -> dict:
    ctrl = _get_controller(request)
    trials = [
        ExperimentTrial(**t.model_dump())
        for t in body.trials
    ]
    plan = ExperimentPlan(
        name=body.name,
        description=body.description,
        trials=trials,
        repeat_count=body.repeat_count,
    )
    ctrl.load_plan(plan)
    ctrl.start()
    return {"plan_id": plan.plan_id, "trials": len(trials), "status": plan.status}


@experiment_router.get("/status")
async def experiment_status(request: Request) -> dict:
    ctrl = _get_controller(request)
    return ctrl.get_status()


@experiment_router.post("/abort")
async def abort_experiment(request: Request) -> dict:
    ctrl = _get_controller(request)
    ctrl.abort()
    return {"status": "aborted"}


@experiment_router.get("/data")
async def experiment_data(
    request: Request,
    capability: str | None = None,
    task_id: str | None = None,
    limit: int = 500,
    offset: int = 0,
) -> dict:
    store = _get_store(request)
    if capability:
        records = store.query_by_capability(capability)
    elif task_id:
        records = store.query_by_task(task_id)
    else:
        records = store.query_all()
    total = len(records)
    page = records[offset: offset + limit]
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "records": [r.model_dump() for r in page],
    }


@experiment_router.post("/export")
async def export_csv(request: Request) -> FileResponse:
    store = _get_store(request)
    tmp = Path(tempfile.mktemp(suffix=".csv"))
    count = store.export_csv(tmp)
    if count == 0:
        raise HTTPException(404, "No experiment data to export")
    return FileResponse(
        path=str(tmp),
        filename="experiment_data.csv",
        media_type="text/csv",
    )


@experiment_router.get("/count")
async def experiment_count(request: Request) -> dict:
    store = _get_store(request)
    return {"count": store.count()}


# ── Learning endpoints (Sprint 3) ─────────────────────────────────────────────

class LearnRequest(BaseModel):
    safety_threshold: float = 0.95


class PredictRequest(BaseModel):
    features: dict[str, float] = Field(default_factory=dict)
    safety_threshold: float = 0.95


class HeatmapRequest(BaseModel):
    dim_x: str = "complexity"
    dim_y: str = "urgency"
    resolution: int = 20
    fixed_features: dict[str, float] | None = None


def _get_registry(request: Request):
    reg = getattr(request.app.state, "capability_registry", None)
    if reg is None:
        raise HTTPException(503, "CapabilityRegistry not initialised")
    return reg


@experiment_router.post("/learn")
async def trigger_learning(body: LearnRequest, request: Request) -> dict:
    """Trigger Phase A piecewise linear learning. Returns the result."""
    store = _get_store(request)
    if store.count() < 3:
        raise HTTPException(400, f"Insufficient data: {store.count()} records (need ≥3)")

    learner = BoundaryLearner(store)
    result = learner.fit_piecewise_linear()

    registry = _get_registry(request)
    updates = learner.write_back_to_graph(registry)

    return {
        "overall_r_squared": result.overall_r_squared,
        "sample_count": result.sample_count,
        "segments": {
            str(k): v.model_dump() for k, v in result.segments.items()
        },
        "boundary_equations": result.boundary_equations,
        "graph_updates": updates,
    }


@experiment_router.get("/boundary")
async def get_boundary(request: Request) -> dict:
    """Return current boundary equations for all mode segments."""
    store = _get_store(request)
    if store.count() < 3:
        return {"equations": {}, "message": "insufficient data"}

    learner = BoundaryLearner(store)
    learner.fit_piecewise_linear()

    equations = {}
    for mode_idx in range(1, 6):
        equations[str(mode_idx)] = learner.get_boundary_equation(mode_idx)

    return {"equations": equations, "sample_count": store.count()}


@experiment_router.post("/boundary/predict")
async def predict_boundary(body: PredictRequest, request: Request) -> dict:
    """Given task features, predict optimal allocation and performance."""
    store = _get_store(request)
    if store.count() < 3:
        raise HTTPException(400, "Insufficient data for prediction")

    learner = BoundaryLearner(store)
    learner.fit_piecewise_linear()
    optimal = learner.suggest_optimal_x_constrained(
        body.features, body.safety_threshold,
    )
    return optimal.model_dump()


@experiment_router.get("/boundary/heatmap")
async def get_heatmap(
    request: Request,
    dim_x: str = "complexity",
    dim_y: str = "urgency",
    resolution: int = 20,
) -> dict:
    """Return a 2D heatmap of optimal_x across two feature dimensions."""
    store = _get_store(request)
    if store.count() < 3:
        raise HTTPException(400, "Insufficient data for heatmap")

    learner = BoundaryLearner(store)
    learner.fit_piecewise_linear()
    return learner.generate_heatmap_data(dim_x, dim_y, resolution)


@experiment_router.post("/apply")
async def apply_learned_weights(request: Request) -> dict:
    """Write learned weights back to ontology YAML and reload."""
    store = _get_store(request)
    if store.count() < 3:
        raise HTTPException(400, "Insufficient data to apply")

    learner = BoundaryLearner(store)
    result = learner.fit_piecewise_linear()

    if result.overall_r_squared < 0.1:
        raise HTTPException(
            400,
            f"Model fit too poor (R²={result.overall_r_squared:.3f}), not applying",
        )

    ontology_path = Path(__file__).resolve().parents[2] / "configs" / "capability-ontology.yaml"
    learner.write_back_to_ontology(ontology_path)

    registry = _get_registry(request)
    updates = learner.write_back_to_graph(registry)

    return {
        "applied": True,
        "overall_r_squared": result.overall_r_squared,
        "ontology_path": str(ontology_path),
        "graph_updates": updates,
    }


# ── Phase B GP endpoints (Sprint 4) ───────────────────────────────────────────

@experiment_router.post("/gp/fit")
async def fit_gp(request: Request) -> dict:
    """Fit constrained dual GP (objective + safety)."""
    store = _get_store(request)
    if store.count() < 5:
        raise HTTPException(400, f"Insufficient data: {store.count()} records (need ≥5)")
    learner = BoundaryLearner(store)
    learner.fit_piecewise_linear()
    result = learner.fit_constrained_gp()
    return result.model_dump()


@experiment_router.post("/gp/suggest")
async def suggest_experiment(
    request: Request, safety_threshold: float = 0.95,
) -> dict:
    """Get GP-based experiment suggestion with safety constraint."""
    store = _get_store(request)
    if store.count() < 5:
        raise HTTPException(400, "Insufficient data for GP suggestion")
    learner = BoundaryLearner(store)
    learner.fit_piecewise_linear()
    learner.fit_constrained_gp()
    suggestion = learner.suggest_next_experiment(safety_threshold)
    return suggestion.model_dump()


@experiment_router.get("/gp/boundary-surface")
async def gp_boundary_surface(
    request: Request, resolution: int = 12, safety_threshold: float = 0.95,
) -> dict:
    """Export GP boundary surface data."""
    store = _get_store(request)
    if store.count() < 5:
        raise HTTPException(400, "Insufficient data")
    learner = BoundaryLearner(store)
    learner.fit_piecewise_linear()
    learner.fit_constrained_gp()
    return learner.get_boundary_surface(resolution, safety_threshold)


@experiment_router.get("/gp/uncertainty")
async def gp_uncertainty(
    request: Request,
    dim_x: str = "complexity", dim_y: str = "urgency",
    resolution: int = 12, mode_idx: int = 3,
) -> dict:
    """Export dual uncertainty heatmap (objective + safety)."""
    store = _get_store(request)
    if store.count() < 5:
        raise HTTPException(400, "Insufficient data")
    learner = BoundaryLearner(store)
    learner.fit_piecewise_linear()
    learner.fit_constrained_gp()
    return learner.get_uncertainty_map(dim_x, dim_y, resolution, mode_idx)


@experiment_router.get("/gp/mode-similarity")
async def mode_similarity(request: Request) -> dict:
    """Export 5x5 collaboration mode similarity matrix."""
    store = _get_store(request)
    learner = BoundaryLearner(store)
    return learner.get_mode_similarity_matrix()


@experiment_router.post("/auto")
async def enable_auto_mode(
    request: Request, safety_threshold: float = 0.95,
) -> dict:
    """Enable GP-based auto experiment mode."""
    store = _get_store(request)
    ctrl = _get_controller(request)
    if store.count() < 5:
        raise HTTPException(400, "Insufficient data for auto mode")
    learner = BoundaryLearner(store)
    learner.fit_piecewise_linear()
    learner.fit_constrained_gp()
    zenoh = getattr(request.app.state, "zenoh_bridge", None)
    return ctrl.enable_auto_mode(learner, safety_threshold, zenoh=zenoh)


@experiment_router.post("/auto/confirm")
async def confirm_auto(request: Request) -> dict:
    """Confirm a pending auto-mode experiment."""
    ctrl = _get_controller(request)
    confirmed = ctrl.confirm_pending()
    return {"confirmed": confirmed}
