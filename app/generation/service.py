"""Shared helpers for normalizing generation requests and results."""

from __future__ import annotations

from typing import Any

from app.config import settings
from app.schemas.api import GenerationRequest, GenerationTaskRecord


def build_initial_state(payload: GenerationRequest, task_id: str) -> dict[str, Any]:
    options = payload.options
    models = options.models
    general_override = options.llm_model

    planner_model = models.planner or general_override or settings.planner_model
    builder_model = models.builder or general_override or settings.builder_model
    fixer_model = models.fixer or general_override or settings.fixer_model

    return {
        "task_id": task_id,
        "entities": payload.entities,
        "environment": payload.environment,
        "task_context": payload.task_context,
        "current_phase_id": payload.current_phase_id,
        "total_phases": 0,
        "iteration_count": 0,
        "max_iterations": options.max_iterations or settings.default_max_iterations,
        "llm_model": general_override or planner_model,
        "planner_model": planner_model,
        "builder_model": builder_model,
        "fixer_model": fixer_model,
        "generation_trace": [],
        "error": None,
    }


def build_task_record(task_id: str, final_state: dict[str, Any], error: str | None = None) -> GenerationTaskRecord:
    validation_report = final_state.get("validation_report") or {}
    behavior_tree = final_state.get("behavior_tree")
    # If no validation was run (validator disabled / bt_template_builder used),
    # treat the result as passed — a missing report is not a failure.
    passed = not validation_report or validation_report.get("validation_result") == "PASSED"

    if error is None and not behavior_tree:
        error = "Pipeline completed without a behavior tree"

    if error is None and not passed:
        violations = validation_report.get("violations") or []
        error = (
            f"Validation failed after generation ({len(violations)} violation(s)); "
            "behavior tree was not loaded"
        )

    return GenerationTaskRecord(
        task_id=task_id,
        status="loaded" if error is None else "failed",
        behavior_tree=behavior_tree,
        fsm_definitions=final_state.get("fsm_definitions", []) or [],
        blackboard_init=final_state.get("blackboard_init", {}) or {},
        validation_report=validation_report,
        generation_trace=final_state.get("generation_trace", []) or [],
        error=error,
        task_plan=final_state.get("task_plan"),
        allocation_trace=list(final_state.get("allocation_trace") or []),
        allocation_quality=dict(final_state.get("allocation_quality") or {}),
        capability_graph=final_state.get("capability_graph"),
    )


def build_result_payload(record: GenerationTaskRecord) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task_id": record.task_id,
        "behavior_tree": record.behavior_tree,
        "fsm_definitions": record.fsm_definitions,
        "blackboard_init": record.blackboard_init,
        "validation_report": record.validation_report,
        "generation_trace": record.generation_trace,
        "execution_mode": record.execution_mode,
        "task_plan": record.task_plan,
        "allocation_trace": record.allocation_trace,
        "allocation_quality": record.allocation_quality,
        "capability_graph": record.capability_graph,
    }
    if record.task_plan:
        phases = record.task_plan.get("phases", [])
        payload["phases"] = phases
        payload["total_phases"] = len(phases)
    return payload
