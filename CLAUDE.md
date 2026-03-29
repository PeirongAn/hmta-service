# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Service Does

**HMTA Service** (Human-Machine Task Allocation) orchestrates collaborative missions between human operators and robots. It takes a high-level mission goal, decomposes it into subtasks, allocates them to humans/robots based on capabilities, generates a Behavior Tree (BT) to execute, and runs that BT at 10 Hz — bridging IDE frontend, UE5 simulation, and physical devices via Zenoh pub/sub.

## Commands

**Setup:**
```bash
uv sync --extra dev    # Install all dependencies (creates .venv)
cp .env.example .env   # Then edit .env with OPENAI_API_KEY, ZENOH_ROUTER, etc.
```

**Run:**
```bash
python -m app.main
# or with auto-reload for development:
uv run uvicorn app.main:app_fastapi --reload --port 8000
```

**Tests:**
```bash
pytest tests/ -v                                    # All tests
pytest tests/test_graph.py -v                       # End-to-end pipeline
pytest tests/test_allocator.py -v                   # Allocation logic
pytest tests/test_hypergraph.py -v                  # Capability reasoning
pytest tests/test_graph.py::ClassName::method -v    # Single test
pytest tests/ --cov=app --cov-report=term-missing   # With coverage
```

**Health check:**
```bash
curl http://localhost:8000/api/v1/health
curl http://localhost:8000/api/v1/generation/pipeline
```

**Docker:**
```bash
docker build -t hmta-service:latest .
docker run -p 8000:8000 -e OPENAI_API_KEY=sk-... hmta-service:latest
```

Requires a running Zenoh router on `tcp/localhost:7447`.

## Architecture

### Core Processing Flow

```
Mission Goal (IDE) → Zenoh: zho/bt/generate/request
    ↓
LangGraph Generation Pipeline (configs/pipeline.yaml)
  ├─ GoalExtractor    → Extract mission objective
  ├─ TaskPlanner      → LLM-based task decomposition
  ├─ CoverageEnsurer  → Spatial/temporal coverage constraints
  ├─ Allocator        → Score & assign tasks to humans/robots
  ├─ BTBuilder        → LLM or template BT generation
  ├─ Validator        → Rule-based validation (retries up to 3x)
  └─ FSM/BB Init      → Initialize state machines & Blackboard
    ↓
ExecutionEngine.load(bt_json) → tick loop at 10 Hz
  ├─ CommandAction → CommandResolver → entity dispatch
  ├─ HumanGate → await operator approval via Zenoh
  ├─ FSM tracks entity lifecycle (idle/executing/completed/failed)
  └─ SnapshotPublisher → Zenoh: zho/bt/execution/tick
```

### Key Subsystems

| Subsystem | Location | Role |
|-----------|----------|------|
| LangGraph Pipeline | `app/generation/graph/` | Multi-stage BT generation with LLM nodes |
| Capability Registry | `app/capability/` | HyperGraph reasoning over entity→device→channel→capability |
| ExecutionEngine | `app/execution/engine.py` | BT lifecycle: load, start, stop, hot_swap |
| Command Layer | `app/execution/command/` | L1 abstract → L2 entity-specific → L3 concrete commands |
| FSM Manager | `app/execution/fsm/` | Entity lifecycle state machines |
| ZenohBridge | `app/zenoh_bridge.py` | Shared pub/sub with IDE, UE5, and other services |
| Experiment Store | `app/experiment/` | A/B testing, capability learning, allocation metrics (SQLite) |

### BT Generation Strategy (Controlled by `configs/pipeline.yaml`)

- **Template-based** (default, enabled): Fast, deterministic — `bt_template_builder`
- **LLM-based** (disabled by default): Flexible, with retry loop — `bt_builder` + `validator`

Toggle by enabling/disabling nodes in `pipeline.yaml`. The graph topology is registry-driven (`node_registry.py`), not hardcoded.

### Capability Reasoning

`app/capability/hypergraph.py` maintains a multi-layer graph: Entity → Device → Channel → Capability. `effective_resolver.py` derives what each entity can do based on current device states and channel availability. This drives allocation scoring in `allocator.py`.

### Zenoh Topics

| Topic | Direction | Purpose |
|-------|-----------|---------|
| `zho/bt/generate/request` | IDE → Service | Trigger generation |
| `zho/bt/generate/{id}/result` | Service → IDE | BT + FSM + Blackboard payload |
| `zho/bt/execution/tick` | Service → IDE | Per-tick node status |
| `zho/command/{entity_id}` | Service → UE5 | Robot command |
| `zho/directive/{entity_id}` | Service → Theia | Human directive |
| `zho/response/{directive_id}` | Theia → Service | Human response |
| `zho/entity/*/state` | UE5 → Service | Entity state updates |

### Key Configuration Files

- `configs/pipeline.yaml` — Enable/disable each LangGraph node; controls the BT generation strategy
- `configs/capability-ontology.yaml` — Skill taxonomy, preconditions/effects, channel definitions
- `configs/human-profiles.yaml` — Human operator device config and cognitive profiles
- `configs/demo-bomb-search-entities*.yaml` — Entity definitions for the bomb-search demo scenario
- `.env` — Runtime settings: LLM keys/endpoints, Zenoh router URL, log level, LangSmith tracing

### State Flow: Generation → Execution

The `LangGraph` state (`app/generation/graph/state.py`) accumulates intermediate results across pipeline nodes. `service.py` assembles the final payload: a `bt_json` (py_trees-compatible), `fsm_defs`, and `bb_init`. `ExecutionEngine` loads these three artifacts; FSMs are instantiated per entity, Blackboard is pre-populated, then the tick loop starts.

### Experiment & Learning Loop

`app/experiment/` implements A/B testing of allocation strategies. After execution, `collector.py` captures outcomes, `learner.py` updates proficiency estimates (Ridge regression / GP), and `proficiency_store.py` persists them for the next mission. `oracle/` provides ground-truth validation.
