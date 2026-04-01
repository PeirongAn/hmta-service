"""Mission persistence store — SQLite-backed (missions, mission_entities, entity_profiles, entity_performance).

Opens the same experiments.db used by ExperimentStore and ProficiencyStore.
All tables use CREATE TABLE IF NOT EXISTS so the store is safe to instantiate
multiple times without schema conflicts.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time as _time
from pathlib import Path
from typing import Any

from app.schemas.mission import (
    EntityPerformanceRecord,
    EntityProfileRecord,
    MissionEntityRecord,
    MissionRecord,
)

logger = logging.getLogger(__name__)

_DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "experiments.db"

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_CREATE_MISSIONS = """
CREATE TABLE IF NOT EXISTS missions (
    mission_id              TEXT PRIMARY KEY,
    task_name               TEXT,
    task_type               TEXT,
    objective               TEXT,
    status                  TEXT NOT NULL DEFAULT 'created',
    started_at              REAL,
    completed_at            REAL,
    outcome                 TEXT,
    duration_ms             REAL,
    generation_request_json TEXT,
    summary_json            TEXT,
    context_json            TEXT
)
"""

_CREATE_MISSION_ENTITIES = """
CREATE TABLE IF NOT EXISTS mission_entities (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id   TEXT    NOT NULL,
    entity_id    TEXT    NOT NULL,
    entity_type  TEXT,
    display_name TEXT,
    role         TEXT,
    joined_at    REAL
)
"""

_CREATE_ENTITY_PROFILES = """
CREATE TABLE IF NOT EXISTS entity_profiles (
    entity_id               TEXT PRIMARY KEY,
    entity_type             TEXT,
    display_name            TEXT,
    role                    TEXT,
    authority_level         TEXT,
    status                  TEXT,
    capabilities_json       TEXT,
    devices_json            TEXT,
    cognitive_profile_json  TEXT,
    metadata_json           TEXT,
    first_seen_at           REAL,
    last_seen_at            REAL
)
"""

_CREATE_ENTITY_PERFORMANCE = """
CREATE TABLE IF NOT EXISTS entity_performance (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id              TEXT    NOT NULL,
    entity_id               TEXT    NOT NULL,
    entity_type             TEXT,
    task_name               TEXT,
    outcome                 TEXT,
    duration_ms             REAL,
    completion_rate         REAL,
    safety_score            REAL,
    intervention_count      INTEGER,
    human_response_time_ms  REAL,
    feedback_score          REAL,
    feedback_tags           TEXT,
    recorded_at             REAL
)
"""


class MissionStore:
    """Zero-dependency SQLite store for mission lifecycle data."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path) if db_path else _DEFAULT_DB
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        for ddl in (_CREATE_MISSIONS, _CREATE_MISSION_ENTITIES, _CREATE_ENTITY_PROFILES, _CREATE_ENTITY_PERFORMANCE):
            self._conn.execute(ddl)
        self._conn.commit()
        logger.info("MissionStore opened at %s", self._db_path)

    # ── Missions ────────────────────────────────────────────────────────────

    def create_mission(
        self,
        mission_id: str,
        task_name: str = "",
        task_type: str = "",
        objective: str = "",
        started_at: float | None = None,
        entities: list[dict[str, Any]] | None = None,
        generation_request_json: str = "",
        context_json: str = "",
    ) -> MissionRecord:
        now = started_at if started_at is not None else _time.time()
        self._conn.execute(
            """INSERT OR IGNORE INTO missions
               (mission_id, task_name, task_type, objective, status, started_at,
                generation_request_json, context_json)
               VALUES (?, ?, ?, ?, 'created', ?, ?, ?)""",
            (mission_id, task_name, task_type, objective, now, generation_request_json, context_json),
        )
        self._conn.commit()

        entity_records: list[MissionEntityRecord] = []
        for e in (entities or []):
            rec = self._add_entity_to_mission_raw(mission_id, e, joined_at=now)
            entity_records.append(rec)

        return MissionRecord(
            mission_id=mission_id,
            task_name=task_name,
            task_type=task_type,
            objective=objective,
            status="created",
            started_at=now,
            entities=entity_records,
        )

    def update_mission(
        self,
        mission_id: str,
        *,
        status: str | None = None,
        outcome: str | None = None,
        completed_at: float | None = None,
        duration_ms: float | None = None,
        summary_json: str | None = None,
    ) -> None:
        sets: list[str] = []
        params: list[Any] = []
        if status is not None:
            sets.append("status = ?"); params.append(status)
        if outcome is not None:
            sets.append("outcome = ?"); params.append(outcome)
        if completed_at is not None:
            sets.append("completed_at = ?"); params.append(completed_at)
        if duration_ms is not None:
            sets.append("duration_ms = ?"); params.append(duration_ms)
        if summary_json is not None:
            sets.append("summary_json = ?"); params.append(summary_json)
        if not sets:
            return
        params.append(mission_id)
        self._conn.execute(f"UPDATE missions SET {', '.join(sets)} WHERE mission_id = ?", params)
        self._conn.commit()

    def get_mission(self, mission_id: str) -> MissionRecord | None:
        row = self._conn.execute(
            "SELECT * FROM missions WHERE mission_id = ?", (mission_id,)
        ).fetchone()
        if not row:
            return None
        record = self._row_to_mission(dict(row))
        record.entities = self._get_mission_entities(mission_id)
        return record

    def list_missions(
        self,
        status: str | None = None,
        entity_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MissionRecord]:
        if entity_id:
            rows = self._conn.execute(
                """SELECT m.* FROM missions m
                   JOIN mission_entities me ON m.mission_id = me.mission_id
                   WHERE me.entity_id = ?
                   {where}
                   ORDER BY m.started_at DESC LIMIT ? OFFSET ?""".format(
                    where="AND m.status = ?" if status else ""
                ),
                (entity_id, status, limit, offset) if status else (entity_id, limit, offset),
            ).fetchall()
        elif status:
            rows = self._conn.execute(
                "SELECT * FROM missions WHERE status = ? ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM missions ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [self._row_to_mission(dict(r)) for r in rows]

    # ── Mission entities ─────────────────────────────────────────────────────

    def add_entity_to_mission(
        self,
        mission_id: str,
        entity_id: str,
        entity_type: str = "robot",
        display_name: str = "",
        role: str = "primary",
    ) -> MissionEntityRecord:
        return self._add_entity_to_mission_raw(
            mission_id,
            {"entity_id": entity_id, "entity_type": entity_type,
             "display_name": display_name, "role": role},
        )

    def get_mission_entities(self, mission_id: str) -> list[MissionEntityRecord]:
        return self._get_mission_entities(mission_id)

    # ── Entity profiles ───────────────────────────────────────────────────────

    def upsert_entity_profile(self, entity_data: dict[str, Any]) -> None:
        now = _time.time()
        entity_id = entity_data.get("entity_id", "")
        if not entity_id:
            return

        existing = self._conn.execute(
            "SELECT first_seen_at FROM entity_profiles WHERE entity_id = ?", (entity_id,)
        ).fetchone()
        first_seen = existing["first_seen_at"] if existing else now

        self._conn.execute(
            """INSERT INTO entity_profiles
               (entity_id, entity_type, display_name, role, authority_level, status,
                capabilities_json, devices_json, cognitive_profile_json, metadata_json,
                first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(entity_id) DO UPDATE SET
                   entity_type             = excluded.entity_type,
                   display_name            = excluded.display_name,
                   role                    = excluded.role,
                   authority_level         = excluded.authority_level,
                   status                  = excluded.status,
                   capabilities_json       = excluded.capabilities_json,
                   devices_json            = excluded.devices_json,
                   cognitive_profile_json  = excluded.cognitive_profile_json,
                   metadata_json           = excluded.metadata_json,
                   last_seen_at            = excluded.last_seen_at""",
            (
                entity_id,
                entity_data.get("entity_type", ""),
                entity_data.get("display_name", ""),
                entity_data.get("role", ""),
                entity_data.get("authority_level", ""),
                entity_data.get("status", "idle"),
                json.dumps(entity_data.get("capabilities", [])),
                json.dumps(entity_data.get("devices", [])),
                json.dumps(entity_data.get("cognitive_profile", {})),
                json.dumps(entity_data.get("metadata", {})),
                first_seen,
                now,
            ),
        )
        self._conn.commit()

    def get_entity_profile(self, entity_id: str) -> EntityProfileRecord | None:
        row = self._conn.execute(
            "SELECT * FROM entity_profiles WHERE entity_id = ?", (entity_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_profile(dict(row))

    def list_entity_profiles(self, entity_type: str | None = None) -> list[EntityProfileRecord]:
        if entity_type:
            rows = self._conn.execute(
                "SELECT * FROM entity_profiles WHERE entity_type = ? ORDER BY last_seen_at DESC",
                (entity_type,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM entity_profiles ORDER BY last_seen_at DESC"
            ).fetchall()
        return [self._row_to_profile(dict(r)) for r in rows]

    # ── Entity performance ────────────────────────────────────────────────────

    def save_performance(self, record: EntityPerformanceRecord) -> None:
        self._conn.execute(
            """INSERT INTO entity_performance
               (mission_id, entity_id, entity_type, task_name, outcome, duration_ms,
                completion_rate, safety_score, intervention_count, human_response_time_ms,
                feedback_score, feedback_tags, recorded_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record.mission_id, record.entity_id, record.entity_type,
                record.task_name, record.outcome, record.duration_ms,
                record.completion_rate, record.safety_score, record.intervention_count,
                record.human_response_time_ms, record.feedback_score,
                record.feedback_tags, _time.time(),
            ),
        )
        self._conn.commit()

    def query_performance_by_entity(self, entity_id: str, limit: int = 50) -> list[EntityPerformanceRecord]:
        rows = self._conn.execute(
            "SELECT * FROM entity_performance WHERE entity_id = ? ORDER BY recorded_at DESC LIMIT ?",
            (entity_id, limit),
        ).fetchall()
        return [self._row_to_performance(dict(r)) for r in rows]

    def query_performance_by_mission(self, mission_id: str) -> list[EntityPerformanceRecord]:
        rows = self._conn.execute(
            "SELECT * FROM entity_performance WHERE mission_id = ? ORDER BY recorded_at",
            (mission_id,),
        ).fetchall()
        return [self._row_to_performance(dict(r)) for r in rows]

    # ── Close ────────────────────────────────────────────────────────────────

    def close(self) -> None:
        self._conn.close()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _add_entity_to_mission_raw(
        self,
        mission_id: str,
        entity: dict[str, Any],
        joined_at: float | None = None,
    ) -> MissionEntityRecord:
        now = joined_at if joined_at is not None else _time.time()
        entity_id = entity.get("entity_id", "")
        entity_type = entity.get("entity_type", "robot")
        display_name = entity.get("display_name", "")
        role = entity.get("role", "primary")
        self._conn.execute(
            """INSERT INTO mission_entities (mission_id, entity_id, entity_type, display_name, role, joined_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (mission_id, entity_id, entity_type, display_name, role, now),
        )
        self._conn.commit()
        return MissionEntityRecord(
            entity_id=entity_id,
            entity_type=entity_type,
            display_name=display_name,
            role=role,
        )

    def _get_mission_entities(self, mission_id: str) -> list[MissionEntityRecord]:
        rows = self._conn.execute(
            "SELECT * FROM mission_entities WHERE mission_id = ? ORDER BY joined_at",
            (mission_id,),
        ).fetchall()
        return [
            MissionEntityRecord(
                entity_id=r["entity_id"],
                entity_type=r["entity_type"] or "robot",
                display_name=r["display_name"] or "",
                role=r["role"] or "primary",
            )
            for r in rows
        ]

    def _row_to_mission(self, d: dict[str, Any]) -> MissionRecord:
        return MissionRecord(
            mission_id=d["mission_id"],
            task_name=d.get("task_name") or "",
            task_type=d.get("task_type") or "",
            objective=d.get("objective") or "",
            status=d.get("status") or "created",
            started_at=d.get("started_at") or 0.0,
            completed_at=d.get("completed_at"),
            outcome=d.get("outcome"),
            duration_ms=d.get("duration_ms"),
        )

    def _row_to_profile(self, d: dict[str, Any]) -> EntityProfileRecord:
        return EntityProfileRecord(
            entity_id=d["entity_id"],
            entity_type=d.get("entity_type") or "",
            display_name=d.get("display_name") or "",
            role=d.get("role") or "",
            authority_level=d.get("authority_level") or "",
            capabilities_json=d.get("capabilities_json") or "[]",
            devices_json=d.get("devices_json") or "[]",
            cognitive_profile_json=d.get("cognitive_profile_json") or "{}",
            metadata_json=d.get("metadata_json") or "{}",
        )

    def _row_to_performance(self, d: dict[str, Any]) -> EntityPerformanceRecord:
        return EntityPerformanceRecord(
            mission_id=d["mission_id"],
            entity_id=d["entity_id"],
            entity_type=d.get("entity_type") or "",
            task_name=d.get("task_name") or "",
            outcome=d.get("outcome") or "",
            duration_ms=d.get("duration_ms") or 0.0,
            completion_rate=d.get("completion_rate") or 0.0,
            safety_score=d.get("safety_score") if d.get("safety_score") is not None else 1.0,
            intervention_count=d.get("intervention_count") or 0,
            human_response_time_ms=d.get("human_response_time_ms"),
            feedback_score=d.get("feedback_score"),
            feedback_tags=d.get("feedback_tags") or "[]",
        )
