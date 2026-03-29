"""Pydantic models for HTTP and in-process generation payloads."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ModelOverrides(BaseModel):
    planner: str | None = None
    builder: str | None = None
    fixer: str | None = None


class GenerationOptions(BaseModel):
    max_iterations: int | None = None
    llm_model: str | None = None
    models: ModelOverrides = Field(default_factory=ModelOverrides)


class GenerationRequest(BaseModel):
    task_id: str | None = None
    entities: list[dict[str, Any]] = Field(default_factory=list)
    environment: dict[str, Any] = Field(default_factory=dict)
    task_context: dict[str, Any] = Field(default_factory=dict)
    options: GenerationOptions = Field(default_factory=GenerationOptions)
    current_phase_id: str | None = None


class GenerationTaskRecord(BaseModel):
    task_id: str
    status: Literal["loaded", "failed"]
    behavior_tree: dict[str, Any] | None = None
    fsm_definitions: list[dict[str, Any]] = Field(default_factory=list)
    blackboard_init: dict[str, Any] = Field(default_factory=dict)
    validation_report: dict[str, Any] = Field(default_factory=dict)
    generation_trace: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None
    execution_mode: Literal["load_only"] = "load_only"
    # Phase-1: allocator / planner artifacts for IDE traceability (Zenoh + HTTP GET /tasks/{id})
    task_plan: dict[str, Any] | None = None
    allocation_trace: list[dict[str, Any]] = Field(default_factory=list)
    allocation_quality: dict[str, Any] = Field(default_factory=dict)
    capability_graph: dict[str, Any] | None = None


class TaskListResponse(BaseModel):
    tasks: list[GenerationTaskRecord] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    uptime_sec: float
    zenoh_connected: bool
    engine_running: bool
    fsm_instances: list[str] = Field(default_factory=list)


class PipelineStepDesc(BaseModel):
    name: str
    node_type: str
    enabled: bool
    retry_target: str | None = None
    max_iterations: int = 3
    registered: bool = True


class GenerationPipelineResponse(BaseModel):
    """LangGraph generation topology (for IDE workflow import)."""

    ordered_steps: list[PipelineStepDesc] = Field(default_factory=list)
    execution_edges: list[dict[str, str]] = Field(default_factory=list)
    retry_edges: list[dict[str, str]] = Field(default_factory=list)
