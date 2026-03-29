"""LangGraph State for the BT generation pipeline."""

from __future__ import annotations

from typing import Any, TypedDict


class GenerationState(TypedDict, total=False):
    # ── Inputs ────────────────────────────────────────────────────────────────
    task_id: str
    entities: list[dict]
    environment: dict
    task_context: dict

    # ── Intermediate artifacts ────────────────────────────────────────────────
    mission_goal: dict | None          # Goal Extractor output
    task_plan: dict | None             # Task Planner output
    capability_graph: dict | None      # Allocator: serialised hypergraph snapshot
    allocation_trace: list[dict]       # Allocator: per-subtask scoring & rationale
    behavior_tree: dict | None         # BT Builder output
    validation_report: dict | None     # Constraint Validator output
    violations: list[dict] | None      # Violation list (None = no violations yet)

    # ── Phase control (§11: per-phase BT generation) ────────────────────────
    current_phase_id: str | None       # Which phase to build BT for (None = all)
    total_phases: int                  # Total number of phases in task_plan

    # ── Iteration control ─────────────────────────────────────────────────────
    iteration_count: int
    max_iterations: int

    # ── Final artifacts ───────────────────────────────────────────────────────
    fsm_definitions: list[dict] | None
    blackboard_init: dict | None

    # ── Meta ──────────────────────────────────────────────────────────────────
    llm_model: str
    planner_model: str
    builder_model: str
    fixer_model: str
    error: str | None
    generation_trace: list[dict]       # per-node input/output records
