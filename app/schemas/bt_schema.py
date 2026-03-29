"""Behavior Tree JSON schema (Pydantic)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


NodeType = Literal[
    "sequence", "selector", "parallel",
    "condition", "action", "humanGate",
    "timeout", "retry",
]


class BTNode(BaseModel):
    node_id: str
    type: NodeType
    name: str
    children: list[str] = []

    # type-specific fields (optional)
    intent: str | None = None            # action / humanGate
    entity: str | None = None           # action / humanGate
    params: dict[str, Any] = {}
    key: str | None = None              # condition
    expected: Any = None                # condition
    policy: str | None = None           # parallel
    timeout_sec: float | None = None    # timeout / humanGate
    max_retries: int | None = None      # retry


class BehaviorTree(BaseModel):
    tree_id: str
    root_id: str
    nodes: dict[str, BTNode]            # node_id → BTNode
    metadata: dict[str, Any] = {}


class BlackboardInit(BaseModel):
    """Initial key-value pairs for the py_trees blackboard."""
    entries: dict[str, Any] = {}


class ValidationReport(BaseModel):
    validation_result: Literal["PASSED", "FAILED"] = "PASSED"
    violations: list[dict[str, Any]] = []
