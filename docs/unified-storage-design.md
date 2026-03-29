# Unified Storage Design — Capability Feedback Learning

**Status:** Design Document (Part E)  
**Scope:** Data persistence for the Capability Feedback Learning system  
**Date:** 2026-03

---

## 1. Current State: Storage Fragmentation

The system currently uses **5 distinct storage mechanisms** spread across **2 runtimes** (Python + Node.js), with overlapping concerns and no cross-runtime query capability.

```
Python runtime (hmta-service)
├── ExperimentStore           → SQLite  data/experiments.db
│   └── experiments table     → (f_i, x_i, P_i) records per subtask
├── CapabilityRegistry        → in-memory HyperGraph
│   └── has_capability edges  → entity↔capability with proficiency weight
├── capability-ontology.yaml  → YAML, read-only at startup (rarely mutated)
└── human-profiles.yaml       → YAML, read-only at startup

Node.js runtime (zho-core / Theia)
├── FeedbackStore             → JSON  ~/.zho-ide/feedback-history.json
│   └── TaskFeedbackReport[]  → per-BT-action reliability feedback
├── TaskHistoryStore          → JSON  ~/.zho-ide/task-history.json
│   └── task summaries        → UI history list
├── BayesianCompetence        → in-memory Beta distributions
│   └── per-action α/β params → per-BT-node Bayesian reliability estimate
└── last-behavior-tree.json   → JSON  (ephemeral BT snapshot)
```

### 1.1 Key Problems

| Problem | Impact |
|---|---|
| **No persistence for learned proficiency** | `CapabilityRegistry` edge weights reset on every process restart — all learning is lost |
| **Duplicated capability tracking** | `ExperimentStore.robot_proficiency` + `FeedbackStore.TaskFeedbackReport` track similar per-entity-capability performance in incompatible schemas |
| **Isolated Bayesian models** | `BayesianCompetence` (Node.js) reliability estimates never reach the Python allocator |
| **YAML mutation anti-pattern** | `capability-ontology.yaml` was written by `BoundaryLearner.write_back_to_ontology()` — YAML is not designed for frequent per-entity updates |
| **No unified history API** | Cannot answer "what is dog1's navigation proficiency trend across all missions?" without joining SQLite + JSON + in-memory |
| **Cross-runtime gap** | Python allocation is the source of truth for capability decisions, but Node.js holds richer historical records — they never sync |

---

## 2. Proposed Unified Architecture

### Core Principle

**SQLite (`experiments.db`) is the single source of truth for all learning data.**

Rationale:
- Already used by `ExperimentStore` — no new dependency
- Supports concurrent reads with WAL mode
- Query-able without loading into memory
- Survives process restart and deployment
- Single file, easy to backup/migrate

### 2.1 New Tables

Extend `hmta-service/data/experiments.db` with three new tables:

#### `proficiency_log` — Append-only audit trail

```sql
CREATE TABLE IF NOT EXISTS proficiency_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       REAL    NOT NULL,
    task_id         TEXT,
    entity_id       TEXT    NOT NULL,
    capability_id   TEXT    NOT NULL,
    previous_value  REAL,
    proposed_value  REAL,
    confirmed_value REAL,           -- NULL if rejected or pending
    accepted        INTEGER,        -- 1=accepted, 0=rejected, NULL=pending
    source          TEXT,           -- 'feedback_pipeline' | 'manual' | 'bayesian'
    reason          TEXT,           -- human-readable reason string
    metrics_json    TEXT            -- JSON blob: {success_rate, duration_eff, intervention_rate}
);
```

**Purpose:** Immutable history of all proficiency changes (proposals, confirmations, rejections). Supports trend visualization and audit.

#### `proficiency_current` — Materialized current state

```sql
CREATE TABLE IF NOT EXISTS proficiency_current (
    entity_id       TEXT NOT NULL,
    capability_id   TEXT NOT NULL,
    proficiency     REAL NOT NULL DEFAULT 1.0,
    updated_at      REAL,
    update_count    INTEGER DEFAULT 0,
    PRIMARY KEY (entity_id, capability_id)
);
```

**Purpose:** Fast lookup of current proficiency per `(entity, capability)`. Loaded into `CapabilityRegistry` on startup to restore learning. Updated via UPSERT after human confirmation.

#### `bottleneck_history` — Per-mission bottleneck archive

```sql
CREATE TABLE IF NOT EXISTS bottleneck_history (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id           TEXT    NOT NULL,
    timestamp         REAL    NOT NULL,
    health_score      REAL,
    total_duration_ms REAL,
    bottlenecks_json  TEXT,          -- JSON array of Bottleneck objects
    summary           TEXT
);
```

**Purpose:** Archives `BottleneckData` produced by `bottleneck_analyzer_node` for each mission. Enables trend analysis across missions without re-computing.

---

### 2.2 Data Flow

```
Process Startup
└── ProficiencyStore.load_all_current()
    └── SELECT * FROM proficiency_current
        └── CapabilityRegistry: set has_capability edge weights

Mission End (Feedback Pipeline)
├── metrics_aggregator_node
│   └── ExperimentStore.query_by_task(task_id)    ← historical reference
├── bottleneck_analyzer_node
│   └── ProficiencyStore.save_bottleneck(...)      → INSERT INTO bottleneck_history
└── proficiency_proposer_node
    └── ProficiencyStore.log_proposal(...)          → INSERT INTO proficiency_log (accepted=NULL)

Human Confirmation
└── apply_confirmed_handler(confirmations)
    ├── ProficiencyStore.confirm_proposal(...)      → UPDATE proficiency_log SET accepted, confirmed_value
    │                                               → UPSERT proficiency_current
    └── CapabilityRegistry.update_proficiency(...)  ← in-memory update
        └── compute_robot_utility() uses new weight → next allocation
```

---

## 3. `ProficiencyStore` API

New class in `hmta-service/app/experiment/proficiency_store.py`:

```python
class ProficiencyStore:
    """
    Unified proficiency persistence, extending ExperimentStore's SQLite DB.

    Opens the same experiments.db file and creates the three new tables
    (proficiency_log, proficiency_current, bottleneck_history) if absent.
    Thread-safe with WAL mode.
    """

    def __init__(self, db_path: Path | None = None) -> None:
        """
        Opens (or creates) experiments.db and runs CREATE TABLE IF NOT EXISTS
        for the three new tables. Shares db_path with ExperimentStore.
        """

    # ── Startup loading ──────────────────────────────────────────────────

    def load_all_current(self) -> dict[tuple[str, str], float]:
        """
        Returns {(entity_id, capability_id): proficiency} for all rows in
        proficiency_current. Called once at startup to seed CapabilityRegistry.
        """

    # ── Feedback pipeline writes ─────────────────────────────────────────

    def log_proposal(
        self,
        task_id: str,
        entity_id: str,
        capability_id: str,
        previous_value: float,
        proposed_value: float,
        reason: str,
        metrics: dict,          # {success_rate, duration_eff, intervention_rate}
        source: str = "feedback_pipeline",
    ) -> int:
        """
        INSERT a pending proposal into proficiency_log (accepted=NULL).
        Returns the row id for later confirmation.
        """

    def save_bottleneck(
        self,
        task_id: str,
        health_score: float,
        total_duration_ms: float,
        bottlenecks: list[dict],
        summary: str | None = None,
    ) -> None:
        """INSERT a bottleneck analysis result into bottleneck_history."""

    # ── Human confirmation ───────────────────────────────────────────────

    def confirm_proposal(
        self,
        task_id: str,
        entity_id: str,
        capability_id: str,
        confirmed_value: float,
        accepted: bool,
    ) -> None:
        """
        UPDATE the most recent pending proficiency_log row for this
        (task_id, entity_id, capability_id) to set accepted and confirmed_value.
        Then UPSERT proficiency_current with the new proficiency (if accepted).
        """

    # ── Query / history ──────────────────────────────────────────────────

    def get_history(
        self,
        entity_id: str,
        capability_id: str,
        limit: int = 20,
    ) -> list[dict]:
        """
        Return the last `limit` proficiency_log rows for this entity+capability,
        ordered by timestamp DESC. Suitable for trend visualization.
        """

    def get_bottleneck_history(
        self,
        limit: int = 10,
    ) -> list[dict]:
        """Return recent bottleneck_history rows for dashboard display."""
```

---

## 4. Migration Strategy

### Phase 1 (immediate, implemented in Part B)

| Action | Detail |
|---|---|
| Create `ProficiencyStore` | New file `app/experiment/proficiency_store.py`, opens same `experiments.db` |
| Startup loading | `main.py` calls `store.load_all_current()` → `registry.load_persisted_proficiency(values)` |
| Feedback pipeline writes | `proficiency_proposer_node` calls `store.log_proposal()` |
| Confirmation handler | `apply_confirmed` calls `store.confirm_proposal()` + `registry.update_proficiency()` |
| Bottleneck archive | `bottleneck_analyzer_node` calls `store.save_bottleneck()` |

### Phase 2 (future, low priority)

| Current Store | Migration | Notes |
|---|---|---|
| `FeedbackStore` (JSON, Node.js) | Expose Python HTTP endpoint; Node writes to it | Cross-runtime sync without Zenoh overhead |
| `BayesianCompetence` (in-memory) | Publish Beta params via Zenoh; Python converts to proficiency signal | Enables Bayesian prior to seed `proficiency_current` |
| `TaskHistoryStore` (JSON) | Keep as-is or read-only UI mirror | Orthogonal to allocation; low value to migrate |

### Phase 3 (future, if scale demands)

- Replace SQLite with PostgreSQL for multi-process or distributed deployment
- No API changes needed — `ProficiencyStore` interface is runtime-agnostic

---

## 5. Impact on Existing Parts

### Part A

- Add `ProficiencyStore` class to `app/experiment/proficiency_store.py`
- Add `CapabilityRegistry.load_persisted_proficiency(values: dict)` method

### Part B

- `feedback_pipeline.py`: `proficiency_proposer_node` calls `store.log_proposal()`
- `feedback_pipeline.py`: `bottleneck_analyzer_node` calls `store.save_bottleneck()`
- `main.py`: Call `store.load_all_current()` at startup + seed registry
- `main.py`: `apply_confirmed` handler calls `store.confirm_proposal()`

### Parts C, D

No storage changes required.

---

## 6. Schema Summary

```
experiments.db
├── experiments           (existing) — per-subtask (f,x,P) records
├── proficiency_log       (new)      — append-only proposal/confirmation audit
├── proficiency_current   (new)      — materialized current proficiency per entity×capability
└── bottleneck_history    (new)      — per-mission bottleneck analysis archive
```

All tables use WAL mode (set once on connection open), enabling concurrent readers with a single writer — suitable for the single-process hmta-service architecture.

---

## 7. Answering Previously Unanswerable Queries

With this design, the following queries become trivial SQL:

```sql
-- dog1's navigation proficiency trend over all missions
SELECT timestamp, previous_value, confirmed_value, reason
FROM proficiency_log
WHERE entity_id = 'dog1' AND capability_id = 'navigation'
  AND accepted = 1
ORDER BY timestamp;

-- Current proficiency of all entities
SELECT entity_id, capability_id, proficiency, update_count
FROM proficiency_current
ORDER BY entity_id, capability_id;

-- Missions where health_score < 0.5 (degraded performance)
SELECT task_id, timestamp, health_score, summary
FROM bottleneck_history
WHERE health_score < 0.5
ORDER BY timestamp DESC;

-- Most rejected capability proposals (entities struggling to improve)
SELECT entity_id, capability_id, COUNT(*) as rejections
FROM proficiency_log
WHERE accepted = 0
GROUP BY entity_id, capability_id
ORDER BY rejections DESC;
```
