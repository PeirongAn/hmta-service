"""Persistent experiment store — SQLite-backed (f, x, P) records."""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "experiments.db"


class ExperimentRecord(BaseModel):
    """One (f_i, x_i, P_i) observation from a single subtask execution."""

    record_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    task_id: str = ""
    subtask_id: str = ""
    timestamp: float = 0.0

    # ── Task features  f_i ──
    complexity: float = 0.0
    urgency: float = 0.0
    risk: float = 0.0
    ambiguity: float = 0.0
    time_pressure: float = 0.0
    needs_human_input: bool = False
    cognitive_switch_cost: float = 0.0

    # ── Allocation decision  x_i ──
    collaboration_mode: str = "task_based"
    collaboration_mode_idx: int = 1
    bt_pattern: str = "autonomous"
    human_involvement: float = 0.0
    human_supervisor: str | None = None
    assigned_robot: str = ""

    # ── Operator snapshot (at allocation time) ──
    operator_cognitive_load: float = 0.0
    operator_fatigue: float = 0.0
    operator_task_count: int = 0
    operator_devices_online: int = 0

    # ── Robot snapshot ──
    robot_battery: float = 1.0
    robot_distance_to_target: float = 0.0
    robot_proficiency: float = 1.0

    # ── Performance  P_i  (objective + hard constraints) ──
    outcome_success: bool = False
    actual_duration_ms: float = 0.0
    expected_duration_ms: float | None = None
    human_response_time_ms: float | None = None
    performance_obj: float = 0.0
    safety_score: float = 1.0
    safety_events: int = 0
    battery_consumed: float = 0.0
    resource_feasible: bool = True

    # ── Capability context ──
    required_capabilities: list[str] = Field(default_factory=list)
    primary_capability: str = ""


# ---------------------------------------------------------------------------
# SQLite store
# ---------------------------------------------------------------------------

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS experiments (
    record_id            TEXT PRIMARY KEY,
    task_id              TEXT,
    subtask_id           TEXT,
    timestamp            REAL,
    complexity           REAL,
    urgency              REAL,
    risk                 REAL,
    ambiguity            REAL,
    time_pressure        REAL,
    needs_human_input    INTEGER,
    cognitive_switch_cost REAL,
    collaboration_mode   TEXT,
    collaboration_mode_idx INTEGER,
    bt_pattern           TEXT,
    human_involvement    REAL,
    human_supervisor     TEXT,
    assigned_robot       TEXT,
    operator_cognitive_load  REAL,
    operator_fatigue     REAL,
    operator_task_count  INTEGER,
    operator_devices_online INTEGER,
    robot_battery        REAL,
    robot_distance_to_target REAL,
    robot_proficiency    REAL,
    outcome_success      INTEGER,
    actual_duration_ms   REAL,
    expected_duration_ms REAL,
    human_response_time_ms REAL,
    performance_obj      REAL,
    safety_score         REAL,
    safety_events        INTEGER,
    battery_consumed     REAL,
    resource_feasible    INTEGER,
    required_capabilities TEXT,
    primary_capability   TEXT
)
"""


class ExperimentStore:
    """Zero-dependency SQLite store for experiment records."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._db_path = Path(db_path) if db_path else _DEFAULT_DB
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_CREATE_SQL)
        self._conn.commit()
        logger.info("ExperimentStore opened at %s", self._db_path)

    # ── Write ──

    def save(self, record: ExperimentRecord) -> None:
        cols = list(ExperimentRecord.model_fields.keys())
        placeholders = ", ".join("?" for _ in cols)
        col_names = ", ".join(cols)
        values = []
        for c in cols:
            v = getattr(record, c)
            if isinstance(v, bool):
                v = int(v)
            elif isinstance(v, list):
                v = json.dumps(v)
            values.append(v)
        self._conn.execute(
            f"INSERT OR REPLACE INTO experiments ({col_names}) VALUES ({placeholders})",
            values,
        )
        self._conn.commit()

    def save_batch(self, records: list[ExperimentRecord]) -> None:
        for r in records:
            self.save(r)

    # ── Read ──

    def query_all(self) -> list[ExperimentRecord]:
        rows = self._conn.execute("SELECT * FROM experiments ORDER BY timestamp").fetchall()
        return [self._row_to_record(r) for r in rows]

    def query_by_capability(self, capability: str) -> list[ExperimentRecord]:
        rows = self._conn.execute(
            "SELECT * FROM experiments WHERE primary_capability = ? ORDER BY timestamp",
            (capability,),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def query_by_task(self, task_id: str) -> list[ExperimentRecord]:
        rows = self._conn.execute(
            "SELECT * FROM experiments WHERE task_id = ? ORDER BY timestamp",
            (task_id,),
        ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM experiments").fetchone()[0]

    # ── Export ──

    def export_csv(self, path: str | Path) -> int:
        import csv
        records = self.query_all()
        if not records:
            return 0
        fields = list(ExperimentRecord.model_fields.keys())
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for r in records:
                writer.writerow(r.model_dump())
        return len(records)

    # ── Internal ──

    def _row_to_record(self, row: tuple) -> ExperimentRecord:
        cols = [desc[0] for desc in self._conn.execute("SELECT * FROM experiments LIMIT 0").description]
        d: dict[str, Any] = dict(zip(cols, row))
        d["needs_human_input"] = bool(d.get("needs_human_input", 0))
        d["outcome_success"] = bool(d.get("outcome_success", 0))
        d["resource_feasible"] = bool(d.get("resource_feasible", 1))
        caps = d.get("required_capabilities", "[]")
        d["required_capabilities"] = json.loads(caps) if isinstance(caps, str) else caps
        return ExperimentRecord(**d)

    def close(self) -> None:
        self._conn.close()
