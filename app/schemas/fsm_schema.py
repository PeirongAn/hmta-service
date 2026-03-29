"""FSM schema for generation output."""

from __future__ import annotations

from pydantic import BaseModel


class FSMDefinition(BaseModel):
    entity_id: str
    entity_type: str                  # robot / human
    initial_state: str = "idle"
    extra_transitions: list[dict] = []   # optional custom transitions for this entity
