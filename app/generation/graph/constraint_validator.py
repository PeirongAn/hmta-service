"""Constraint Validator node — pure rule engine, no LLM."""

from __future__ import annotations

import logging

from app.generation.graph.progress import publish_step
from app.generation.graph.state import GenerationState
from app.generation.validators.capability_check import check_capability_match
from app.generation.validators.safety_check import check_safety_constraints
from app.generation.validators.structure_check import check_structure_integrity

logger = logging.getLogger(__name__)


def validator_node(state: GenerationState) -> dict:
    """LangGraph node: Constraint Validator (synchronous, pure rules)."""
    task_id = state.get("task_id", "unknown")
    bt = state.get("behavior_tree", {})
    entities = state.get("entities", [])
    task_ctx = state.get("task_context", {})

    logger.info("[%s] validator started", task_id)
    iteration = state.get("iteration_count", 0)
    max_iters = state.get("max_iterations", 3)
    publish_step("validator", "started", message="校验行为树约束…",
                 iteration=iteration, max_iterations=max_iters)

    violations: list[dict] = []
    violations += check_capability_match(bt, entities)
    violations += check_safety_constraints(bt, entities, task_ctx)
    violations += check_structure_integrity(bt)

    passed = len(violations) == 0
    result = "PASSED" if passed else "FAILED"
    logger.info("[%s] validator result=%s violations=%d", task_id, result, len(violations))
    for v in violations:
        logger.warning("[%s] VIOLATION [%s] %s", task_id, v.get("rule"), v.get("message"))

    if passed:
        publish_step("validator", "completed", message="校验通过",
                     iteration=iteration, max_iterations=max_iters)
    else:
        publish_step("validator", "completed",
                     message=f"发现 {len(violations)} 个违规, 需修复",
                     iteration=iteration, max_iterations=max_iters)

    report = {
        "validation_result": result,
        "violations": violations,
    }

    trace_entry = {
        "step": "validator",
        "status": "completed",
        "validation_result": result,
        "violation_count": len(violations),
    }

    return {
        "validation_report": report,
        "violations": violations if violations else None,
        "generation_trace": [*state.get("generation_trace", []), trace_entry],
    }
