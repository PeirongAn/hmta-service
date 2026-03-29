"""Health check and debug API endpoints."""

from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Request

from app.generation.graph.coordinator import get_graph
from app.generation.graph.node_registry import get_registry
from app.generation.service import build_initial_state, build_task_record
from app.schemas.api import (
    GenerationPipelineResponse,
    GenerationRequest,
    GenerationTaskRecord,
    HealthResponse,
    PipelineStepDesc,
    TaskListResponse,
)

router = APIRouter(prefix="/api/v1", tags=["health"])

_start_time = time.time()


@router.get("/generation/pipeline", response_model=GenerationPipelineResponse)
async def get_generation_pipeline() -> GenerationPipelineResponse:
    """Return current LangGraph generation pipeline topology (YAML + registry)."""
    raw = get_registry().describe_pipeline()
    steps = [PipelineStepDesc.model_validate(s) for s in raw.get("ordered_steps", [])]
    return GenerationPipelineResponse(
        ordered_steps=steps,
        execution_edges=list(raw.get("execution_edges", [])),
        retry_edges=list(raw.get("retry_edges", [])),
    )


@router.get("/health", response_model=HealthResponse)
async def health(request: Request) -> HealthResponse:
    state = request.app.state
    return HealthResponse(
        uptime_sec=round(time.time() - _start_time, 1),
        zenoh_connected=state.zenoh_bridge.session is not None,
        engine_running=getattr(state.engine, "_running", False),
        fsm_instances=list(state.fsm_manager.instances.keys()),
    )


@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks(request: Request) -> TaskListResponse:
    """List recent generation task traces (debug)."""
    state = request.app.state
    return TaskListResponse(
        tasks=[GenerationTaskRecord.model_validate(task) for task in state.task_registry.values()]
    )


@router.get("/tasks/{task_id}", response_model=GenerationTaskRecord)
async def get_task(task_id: str, request: Request) -> GenerationTaskRecord:
    state = request.app.state
    task = state.task_registry.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    return GenerationTaskRecord.model_validate(task)


@router.post("/generate", response_model=GenerationTaskRecord)
async def generate_http(payload: GenerationRequest, request: Request) -> GenerationTaskRecord:
    """
    HTTP fallback entry-point for BT generation (when Zenoh is unavailable).
    Runs the full LangGraph pipeline synchronously and loads the BT on success.
    """
    state = request.app.state
    task_id = payload.task_id or f"http_{int(time.time())}"
    initial = build_initial_state(payload, task_id)
    record: GenerationTaskRecord

    try:
        graph = get_graph()
        final_state = await graph.ainvoke(initial)
        record = build_task_record(task_id, final_state)

        if record.status == "loaded":
            try:
                if state.engine is None:
                    raise RuntimeError("Execution engine is not available")
                state.engine.load(
                    bt_json=record.behavior_tree or {},
                    bb_init=record.blackboard_init,
                    fsm_defs=record.fsm_definitions,
                )
            except Exception as exc:
                record = build_task_record(task_id, final_state, error=str(exc))
    except Exception as exc:
        record = build_task_record(task_id, {}, error=str(exc))

    state.task_registry[task_id] = record.model_dump()
    return record
