"""Unified proficiency persistence — extends ExperimentStore's SQLite DB.

Opens the same experiments.db and manages tables:
  proficiency_log       — append-only audit trail of proposals & confirmations
  proficiency_current   — materialized current proficiency per (entity, capability)
  bottleneck_history    — per-mission bottleneck analysis archive
  oracle_judgments      — ground-truth capability judgments (detection accuracy, etc.)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time as _time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "experiments.db"

_CREATE_PROFICIENCY_LOG = """
CREATE TABLE IF NOT EXISTS proficiency_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       REAL    NOT NULL,
    task_id         TEXT,
    entity_id       TEXT    NOT NULL,
    capability_id   TEXT    NOT NULL,
    previous_value  REAL,
    proposed_value  REAL,
    confirmed_value REAL,
    accepted        INTEGER,
    source          TEXT,
    reason          TEXT,
    metrics_json    TEXT
)
"""

_CREATE_PROFICIENCY_CURRENT = """
CREATE TABLE IF NOT EXISTS proficiency_current (
    entity_id       TEXT NOT NULL,
    capability_id   TEXT NOT NULL,
    proficiency     REAL NOT NULL DEFAULT 1.0,
    updated_at      REAL,
    update_count    INTEGER DEFAULT 0,
    PRIMARY KEY (entity_id, capability_id)
)
"""

_CREATE_BOTTLENECK_HISTORY = """
CREATE TABLE IF NOT EXISTS bottleneck_history (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id           TEXT    NOT NULL,
    timestamp         REAL    NOT NULL,
    health_score      REAL,
    total_duration_ms REAL,
    bottlenecks_json  TEXT,
    summary           TEXT
)
"""


_CREATE_ORACLE_JUDGMENTS = """
CREATE TABLE IF NOT EXISTS oracle_judgments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       REAL    NOT NULL,
    task_id         TEXT,
    judgment_id     TEXT    NOT NULL,
    entity_id       TEXT    NOT NULL,
    capability_id   TEXT    NOT NULL,
    entity_type     TEXT,
    judgment_type   TEXT    NOT NULL,
    outcome         TEXT    NOT NULL,
    source          TEXT,
    confidence      REAL,
    details_json    TEXT
)
"""


class ProficiencyStore:
    """Unified proficiency persistence, extending ExperimentStore's SQLite DB.

    Opens the same experiments.db file and creates tables
    (proficiency_log, proficiency_current, bottleneck_history, oracle_judgments)
    if absent.  Thread-safe with WAL mode.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        self._db_path = Path(db_path) if db_path else _DEFAULT_DB
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_CREATE_PROFICIENCY_LOG)
        self._conn.execute(_CREATE_PROFICIENCY_CURRENT)
        self._conn.execute(_CREATE_BOTTLENECK_HISTORY)
        self._conn.execute(_CREATE_ORACLE_JUDGMENTS)
        self._conn.commit()
        logger.info("ProficiencyStore opened at %s", self._db_path)

    # ── Startup loading ───────────────────────────────────────────────────────

    def load_all_current(self) -> dict[tuple[str, str], float]:
        """Return {(entity_id, capability_id): proficiency} for startup CapabilityRegistry seeding.

        Reading strategy (two-tier, both from DB):

        Tier 1 — ``proficiency_current``:
            Human-confirmed, authoritative. Written only when a proficiency
            proposal is explicitly accepted.  May be empty if no confirmation
            has ever happened.

        Tier 2 — latest pending entry in ``proficiency_log``:
            When no confirmed value exists for an (entity, capability) pair,
            fall back to the most recent *proposed* value (accepted IS NULL).
            This ensures learned signal from completed missions is not lost
            across restarts even when the frontend confirmation flow has not
            been exercised yet.

        Confirmed values always override pending ones.
        """
        result: dict[tuple[str, str], float] = {}

        # Tier 2 first (lower priority) — latest pending proposal per pair
        pending_rows = self._conn.execute(
            """
            SELECT entity_id, capability_id, proposed_value
            FROM proficiency_log
            WHERE accepted IS NULL
              AND proposed_value IS NOT NULL
            GROUP BY entity_id, capability_id
            HAVING timestamp = MAX(timestamp)
            """
        ).fetchall()
        for entity_id, cap_id, proposed in pending_rows:
            result[(entity_id, cap_id)] = float(proposed)

        pending_count = len(result)

        # Tier 1 (higher priority) — confirmed, materialised state
        confirmed_rows = self._conn.execute(
            "SELECT entity_id, capability_id, proficiency FROM proficiency_current"
        ).fetchall()
        for entity_id, cap_id, prof in confirmed_rows:
            result[(entity_id, cap_id)] = float(prof)

        confirmed_count = len(confirmed_rows)
        logger.info(
            "ProficiencyStore.load_all_current: %d confirmed + %d pending-fallback = %d total",
            confirmed_count, pending_count - confirmed_count, len(result),
        )
        return result

    # ── Feedback pipeline writes ──────────────────────────────────────────────

    def log_proposal(
        self,
        task_id: str,
        entity_id: str,
        capability_id: str,
        previous_value: float,
        proposed_value: float,
        reason: str,
        metrics: dict[str, Any],
        source: str = "feedback_pipeline",
    ) -> int:
        """INSERT a pending proposal into proficiency_log (accepted=NULL).

        Returns the row id for later confirmation via confirm_proposal().
        """
        cursor = self._conn.execute(
            """
            INSERT INTO proficiency_log
                (timestamp, task_id, entity_id, capability_id,
                 previous_value, proposed_value,
                 confirmed_value, accepted, source, reason, metrics_json)
            VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?, ?)
            """,
            (
                _time.time(),
                task_id,
                entity_id,
                capability_id,
                previous_value,
                proposed_value,
                source,
                reason,
                json.dumps(metrics),
            ),
        )
        self._conn.commit()
        row_id: int = cursor.lastrowid  # type: ignore[assignment]
        logger.debug(
            "ProficiencyStore.log_proposal: id=%d %s/%s %.4f→%.4f",
            row_id, entity_id, capability_id, previous_value, proposed_value,
        )
        return row_id

    def save_bottleneck(
        self,
        task_id: str,
        health_score: float,
        total_duration_ms: float,
        bottlenecks: list[dict],
        summary: str | None = None,
    ) -> None:
        """INSERT a bottleneck analysis result into bottleneck_history."""
        self._conn.execute(
            """
            INSERT INTO bottleneck_history
                (task_id, timestamp, health_score, total_duration_ms,
                 bottlenecks_json, summary)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                _time.time(),
                health_score,
                total_duration_ms,
                json.dumps(bottlenecks),
                summary,
            ),
        )
        self._conn.commit()
        logger.debug(
            "ProficiencyStore.save_bottleneck: task_id=%s health=%.3f bottlenecks=%d",
            task_id, health_score, len(bottlenecks),
        )

    # ── Human confirmation ────────────────────────────────────────────────────

    def confirm_proposal(
        self,
        task_id: str,
        entity_id: str,
        capability_id: str,
        confirmed_value: float,
        accepted: bool,
    ) -> None:
        """Resolve the most recent pending proposal for (task_id, entity_id, capability_id).

        UPDATE proficiency_log: set accepted flag and confirmed_value.
        If accepted, UPSERT proficiency_current with the new proficiency.
        """
        # Find the most recent pending row for this triple
        row = self._conn.execute(
            """
            SELECT id FROM proficiency_log
            WHERE task_id = ? AND entity_id = ? AND capability_id = ?
              AND accepted IS NULL
            ORDER BY timestamp DESC
            LIMIT 1
            """,
            (task_id, entity_id, capability_id),
        ).fetchone()

        if row:
            log_id = row[0]
            self._conn.execute(
                """
                UPDATE proficiency_log
                SET accepted = ?, confirmed_value = ?
                WHERE id = ?
                """,
                (1 if accepted else 0, confirmed_value if accepted else None, log_id),
            )

        if accepted:
            self._conn.execute(
                """
                INSERT INTO proficiency_current (entity_id, capability_id, proficiency, updated_at, update_count)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(entity_id, capability_id) DO UPDATE SET
                    proficiency  = excluded.proficiency,
                    updated_at   = excluded.updated_at,
                    update_count = update_count + 1
                """,
                (entity_id, capability_id, confirmed_value, _time.time()),
            )

        self._conn.commit()
        logger.info(
            "ProficiencyStore.confirm_proposal: %s/%s  accepted=%s  value=%.4f",
            entity_id, capability_id, accepted, confirmed_value,
        )

    # ── Query / history ───────────────────────────────────────────────────────

    def get_history(
        self,
        entity_id: str,
        capability_id: str,
        limit: int = 20,
    ) -> list[dict]:
        """Return the last *limit* proficiency_log rows for entity+capability.

        Ordered by timestamp DESC — suitable for trend visualization.
        """
        rows = self._conn.execute(
            """
            SELECT id, timestamp, task_id, entity_id, capability_id,
                   previous_value, proposed_value, confirmed_value,
                   accepted, source, reason, metrics_json
            FROM proficiency_log
            WHERE entity_id = ? AND capability_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (entity_id, capability_id, limit),
        ).fetchall()
        return [self._row_to_log_dict(r) for r in rows]

    def get_bottleneck_history(self, limit: int = 10) -> list[dict]:
        """Return recent bottleneck_history rows for dashboard display."""
        rows = self._conn.execute(
            """
            SELECT id, task_id, timestamp, health_score, total_duration_ms,
                   bottlenecks_json, summary
            FROM bottleneck_history
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            {
                "id": r[0],
                "task_id": r[1],
                "timestamp": r[2],
                "health_score": r[3],
                "total_duration_ms": r[4],
                "bottlenecks": json.loads(r[5]) if r[5] else [],
                "summary": r[6],
            }
            for r in rows
        ]

    # ── Internal helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _row_to_log_dict(row: tuple) -> dict:
        keys = [
            "id", "timestamp", "task_id", "entity_id", "capability_id",
            "previous_value", "proposed_value", "confirmed_value",
            "accepted", "source", "reason", "metrics_json",
        ]
        d = dict(zip(keys, row))
        if d.get("metrics_json"):
            try:
                d["metrics"] = json.loads(d["metrics_json"])
            except Exception:
                d["metrics"] = {}
        del d["metrics_json"]
        return d

    # ── Oracle judgments ────────────────────────────────────────────────────

    def save_oracle_judgment(self, judgment: Any) -> None:
        """Persist an ``OracleJudgment`` from the oracle service."""
        self._conn.execute(
            """
            INSERT INTO oracle_judgments
                (timestamp, task_id, judgment_id, entity_id, capability_id,
                 entity_type, judgment_type, outcome, source, confidence,
                 details_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                getattr(judgment, "timestamp", _time.time()),
                getattr(judgment, "task_id", ""),
                getattr(judgment, "judgment_id", ""),
                judgment.entity_id,
                judgment.capability_id,
                getattr(judgment, "entity_type", ""),
                judgment.judgment_type,
                judgment.outcome,
                getattr(judgment, "source", ""),
                getattr(judgment, "confidence", 1.0),
                json.dumps(getattr(judgment, "details", {})),
            ),
        )
        self._conn.commit()

    def get_oracle_accuracy(
        self,
        entity_id: str,
        capability_id: str,
    ) -> dict:
        """Aggregate oracle TP/FP counts across all missions for this entity+capability."""
        rows = self._conn.execute(
            """
            SELECT outcome, COUNT(*) as cnt
            FROM oracle_judgments
            WHERE entity_id = ? AND capability_id = ?
            GROUP BY outcome
            """,
            (entity_id, capability_id),
        ).fetchall()
        counts: dict[str, int] = {r[0]: r[1] for r in rows}
        tp = counts.get("true_positive", 0)
        fp = counts.get("false_positive", 0)
        total = tp + fp
        return {
            "entity_id": entity_id,
            "capability_id": capability_id,
            "true_positives": tp,
            "false_positives": fp,
            "total_judgments": total,
            "accuracy_rate": tp / total if total > 0 else None,
        }

    def close(self) -> None:
        self._conn.close()
