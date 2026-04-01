"""Mission & entity management API — CRUD for missions, entity profiles, and performance."""

from __future__ import annotations

import logging
import time as _time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.experiment.mission_store import MissionStore
from app.schemas.mission import EntityPerformanceRecord, MissionRecord

logger = logging.getLogger(__name__)

mission_router = APIRouter(prefix="/api/v1/missions", tags=["missions"])
entity_router = APIRouter(prefix="/api/v1/entities", tags=["entities"])


# ── Store accessor ────────────────────────────────────────────────────────────

def _get_store(request: Request) -> MissionStore:
    store = getattr(request.app.state, "mission_store", None)
    if store is None:
        raise HTTPException(503, "MissionStore not initialised")
    return store


# ── Request bodies ────────────────────────────────────────────────────────────

class CreateMissionBody(BaseModel):
    mission_id: str
    task_name: str = ""
    task_type: str = ""
    objective: str = ""
    started_at: float | None = None
    entities: list[dict[str, Any]] = Field(default_factory=list)
    generation_request_json: str = ""
    context_json: str = ""


class UpdateMissionBody(BaseModel):
    status: str | None = None
    outcome: str | None = None
    completed_at: float | None = None
    duration_ms: float | None = None
    summary_json: str | None = None


class AddEntityBody(BaseModel):
    entity_id: str
    entity_type: str = "robot"
    display_name: str = ""
    role: str = "primary"


class SavePerformanceBody(BaseModel):
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


class UpsertEntityBody(BaseModel):
    entity_id: str
    entity_type: str = ""
    display_name: str = ""
    role: str = ""
    authority_level: str = ""
    status: str = "idle"
    capabilities: list[Any] = Field(default_factory=list)
    devices: list[Any] = Field(default_factory=list)
    cognitive_profile: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


# ── Mission endpoints (/api/v1/missions) ──────────────────────────────────────

@mission_router.post("/", response_model=dict)
async def create_mission(body: CreateMissionBody, request: Request) -> dict:
    store = _get_store(request)
    record = store.create_mission(
        mission_id=body.mission_id,
        task_name=body.task_name,
        task_type=body.task_type,
        objective=body.objective,
        started_at=body.started_at,
        entities=body.entities,
        generation_request_json=body.generation_request_json,
        context_json=body.context_json,
    )
    return record.model_dump()


@mission_router.get("/", response_model=dict)
async def list_missions(
    request: Request,
    status: str | None = None,
    entity_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    store = _get_store(request)
    records = store.list_missions(status=status, entity_id=entity_id, limit=limit, offset=offset)
    return {
        "total": len(records),
        "offset": offset,
        "limit": limit,
        "missions": [r.model_dump() for r in records],
    }


@mission_router.get("/{mission_id}", response_model=dict)
async def get_mission(mission_id: str, request: Request) -> dict:
    store = _get_store(request)
    record = store.get_mission(mission_id)
    if record is None:
        raise HTTPException(404, f"Mission '{mission_id}' not found")
    result = record.model_dump()
    result["performance"] = [p.model_dump() for p in store.query_performance_by_mission(mission_id)]
    return result


@mission_router.put("/{mission_id}", response_model=dict)
async def update_mission(mission_id: str, body: UpdateMissionBody, request: Request) -> dict:
    store = _get_store(request)
    if store.get_mission(mission_id) is None:
        raise HTTPException(404, f"Mission '{mission_id}' not found")
    store.update_mission(
        mission_id,
        status=body.status,
        outcome=body.outcome,
        completed_at=body.completed_at,
        duration_ms=body.duration_ms,
        summary_json=body.summary_json,
    )
    updated = store.get_mission(mission_id)
    return updated.model_dump() if updated else {}


@mission_router.get("/{mission_id}/entities", response_model=dict)
async def get_mission_entities(mission_id: str, request: Request) -> dict:
    store = _get_store(request)
    if store.get_mission(mission_id) is None:
        raise HTTPException(404, f"Mission '{mission_id}' not found")
    entities = store.get_mission_entities(mission_id)
    return {"mission_id": mission_id, "entities": [e.model_dump() for e in entities]}


@mission_router.post("/{mission_id}/entities", response_model=dict)
async def add_entity_to_mission(mission_id: str, body: AddEntityBody, request: Request) -> dict:
    store = _get_store(request)
    if store.get_mission(mission_id) is None:
        raise HTTPException(404, f"Mission '{mission_id}' not found")
    record = store.add_entity_to_mission(
        mission_id=mission_id,
        entity_id=body.entity_id,
        entity_type=body.entity_type,
        display_name=body.display_name,
        role=body.role,
    )
    return record.model_dump()


@mission_router.get("/{mission_id}/performance", response_model=dict)
async def get_mission_performance(mission_id: str, request: Request) -> dict:
    store = _get_store(request)
    if store.get_mission(mission_id) is None:
        raise HTTPException(404, f"Mission '{mission_id}' not found")
    records = store.query_performance_by_mission(mission_id)
    return {"mission_id": mission_id, "performance": [r.model_dump() for r in records]}


@mission_router.post("/{mission_id}/performance", response_model=dict)
async def save_mission_performance(mission_id: str, body: SavePerformanceBody, request: Request) -> dict:
    store = _get_store(request)
    if store.get_mission(mission_id) is None:
        raise HTTPException(404, f"Mission '{mission_id}' not found")
    record = EntityPerformanceRecord(
        mission_id=mission_id,
        **body.model_dump(),
    )
    store.save_performance(record)
    return {"saved": True, "mission_id": mission_id, "entity_id": body.entity_id}


# ── Entity endpoints (/api/v1/entities) ───────────────────────────────────────

@entity_router.post("/", response_model=dict)
async def upsert_entity(body: UpsertEntityBody, request: Request) -> dict:
    store = _get_store(request)
    store.upsert_entity_profile(body.model_dump())
    profile = store.get_entity_profile(body.entity_id)
    return profile.model_dump() if profile else {}


@entity_router.get("/", response_model=dict)
async def list_entities(
    request: Request,
    entity_type: str | None = None,
) -> dict:
    store = _get_store(request)
    profiles = store.list_entity_profiles(entity_type=entity_type)
    return {"total": len(profiles), "entities": [p.model_dump() for p in profiles]}


@entity_router.get("/{entity_id}", response_model=dict)
async def get_entity(entity_id: str, request: Request) -> dict:
    store = _get_store(request)
    profile = store.get_entity_profile(entity_id)
    if profile is None:
        raise HTTPException(404, f"Entity '{entity_id}' not found")
    return profile.model_dump()


@entity_router.get("/{entity_id}/missions", response_model=dict)
async def get_entity_missions(
    entity_id: str,
    request: Request,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    store = _get_store(request)
    if store.get_entity_profile(entity_id) is None:
        raise HTTPException(404, f"Entity '{entity_id}' not found")
    missions = store.list_missions(entity_id=entity_id, limit=limit, offset=offset)
    return {
        "entity_id": entity_id,
        "total": len(missions),
        "missions": [m.model_dump() for m in missions],
    }


@entity_router.get("/{entity_id}/performance", response_model=dict)
async def get_entity_performance(
    entity_id: str,
    request: Request,
    limit: int = 50,
) -> dict:
    store = _get_store(request)
    records = store.query_performance_by_entity(entity_id, limit=limit)
    return {
        "entity_id": entity_id,
        "total": len(records),
        "performance": [r.model_dump() for r in records],
    }
