"""Mission persistence schemas — Pydantic models for mission lifecycle data."""

from __future__ import annotations

from pydantic import BaseModel


class MissionEntityRecord(BaseModel):
    """A single entity participating in a mission."""

    entity_id: str
    entity_type: str = "robot"
    display_name: str = ""
    role: str = "primary"


class MissionRecord(BaseModel):
    """Top-level mission record persisted when a BT mission is started."""

    mission_id: str
    task_name: str = ""
    task_type: str = ""
    objective: str = ""
    status: str = "created"        # created | planning | executing | completed | failed
    started_at: float = 0.0
    completed_at: float | None = None
    outcome: str | None = None
    duration_ms: float | None = None
    entities: list[MissionEntityRecord] = []


class EntityProfileRecord(BaseModel):
    """Persistent profile for a registered entity (robot or human)."""

    entity_id: str
    entity_type: str
    display_name: str = ""
    role: str = ""
    authority_level: str = ""
    capabilities_json: str = "[]"
    devices_json: str = "[]"
    cognitive_profile_json: str = "{}"
    metadata_json: str = "{}"


class EntityPerformanceRecord(BaseModel):
    """Per-entity performance measurement for a completed mission."""

    mission_id: str
    entity_id: str
    entity_type: str = ""
    task_name: str = ""
    outcome: str = ""
    duration_ms: float = 0.0
    completion_rate: float = 0.0
    safety_score: float = 1.0
    intervention_count: int = 0
    human_response_time_ms: float | None = None
    feedback_score: float | None = None
    feedback_tags: str = "[]"
