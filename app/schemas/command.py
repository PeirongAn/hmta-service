"""L1–L3 command layer Pydantic models."""

from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field


# ── L1: Abstract Command ──────────────────────────────────────────────────────

class AbstractCommand(BaseModel):
    command_id: str = Field(default_factory=lambda: str(uuid4()))
    intent: str                           # e.g. "navigation.move_to"
    entity_id: str
    params: dict[str, Any] = {}
    priority: Literal["normal", "urgent", "critical"] = "normal"
    context: dict[str, Any] | None = None
    timeout_sec: float | None = None
    node_id: str = ""                     # py_trees behaviour node ID (for UE traceability)


# ── L2: Typed Commands ────────────────────────────────────────────────────────

class SuccessCriteria(BaseModel):
    position_tolerance: float = 0.5
    timeout_sec: float = 60.0


class AbortCondition(BaseModel):
    type: str
    timeout_sec: float | None = None
    threshold: float | None = None
    duration_sec: float | None = None


class RobotCommand(BaseModel):
    command_id: str
    entity_id: str
    command_type: str                     # NAVIGATE / SCAN / MARK / DISARM / HALT
    node_id: str = ""                     # py_trees behaviour node ID
    execution_params: dict[str, Any] = {}
    success_criteria: SuccessCriteria = Field(default_factory=SuccessCriteria)
    abort_conditions: list[AbortCondition] = []
    fallback_behavior: str = "standby"


# ── L2: Human Directive ───────────────────────────────────────────────────────

class DecisionOption(BaseModel):
    option_id: str
    label: str
    description: str = ""
    risk_level: str = "low"


class HumanDirective(BaseModel):
    directive_id: str
    entity_id: str
    directive_type: str                   # approval / override / report
    task_description: str
    situation_briefing: dict[str, Any] = {}
    recommended_action: str = ""
    decision_options: list[DecisionOption] = []
    constraints_reminder: list[str] = []
    urgency: str = "normal"
    expected_response: dict[str, Any] = {}
    timeout_sec: float = 120.0


# ── L3: Human Response ────────────────────────────────────────────────────────

class HumanResponse(BaseModel):
    response_id: str                      # matches directive_id
    entity_id: str
    selected_option: str
    reason: str | None = None
    conditions: str | None = None
    timestamp: float = 0.0
    # Extended fields for non-decision responses
    waypoints: list[dict] | None = None   # for path_planning directives
    response_data: dict | None = None     # for observation / other custom responses


# ── Command status tracking ───────────────────────────────────────────────────

class CommandStatus(BaseModel):
    command_id: str = ""
    entity_id: str = ""
    node_id: str = ""
    state: Literal["dispatched", "running", "executing", "completed", "failed", "cancelled", "unknown"] = "unknown"
    error: str | None = None
    result: dict[str, Any] | None = None


class ResolveResult(BaseModel):
    command_id: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None
