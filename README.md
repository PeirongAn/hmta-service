
# HMTA Service

**Human-Machine Task Allocation Service** — Python microservice combining:

- **LangGraph** multi-agent BT generation pipeline (Plan 03)
- **py_trees** behavior tree execution engine (Plan 04)
- Three-layer command system L1→L2→L3 (Plan 05)
- **python-statemachine** FSM manager (Plan 06)
- **Zenoh** as the unified communication bus
- **FastAPI** for health checks and HTTP fallback

---

## Architecture

```
Theia Backend (TypeScript)
    │  zho/bt/generate/request
    ▼
ZenohBridge.subscribe_generation_requests()
    │
    ▼
LangGraph Pipeline
    TaskPlanner → BTBuilder → ConstraintValidator → FSM_BB_Init
         ↑               ↓ (repair loop, max 3 iterations)
         └─── violations ─┘
    │
    ▼  zho/bt/generate/{task_id}/result
ExecutionEngine.load(bt_json, bb_init, fsm_defs)
    │
    └─ BT is validated and loaded only
       (execution start is an explicit later step)
```

---

## 找炸弹演示（第一阶段）

参见 [configs/DEMO-BOMB-SEARCH.md](configs/DEMO-BOMB-SEARCH.md) 与 `configs/demo-bomb-search-entities*.yaml`：2 狗 + 1 操作员、allocator 对照画像、Zenoh/HTTP 结果中的 `task_plan` / `allocation_trace`。

## Project Structure

```
hmta-service/
├── pyproject.toml
├── Dockerfile
├── .env.example
├── app/
│   ├── main.py                    ← FastAPI + startup wiring
│   ├── config.py                  ← pydantic-settings
│   ├── zenoh_bridge.py            ← Zenoh session wrapper
│   ├── schemas/                   ← Pydantic models (entity, command, bt, fsm)
│   ├── api/
│   │   └── health.py              ← GET /api/v1/health + POST /api/v1/generate
│   ├── generation/
│   │   ├── graph/
│   │   │   ├── state.py           ← LangGraph TypedDict State
│   │   │   ├── coordinator.py     ← StateGraph + conditional edges
│   │   │   ├── task_planner.py    ← LCEL chain (ChatOpenAI | prompt | JsonOutputParser)
│   │   │   ├── bt_builder.py      ← Build + repair chain
│   │   │   ├── constraint_validator.py  ← Pure rule engine
│   │   │   └── fsm_bb_init.py     ← Template-based init
│   │   ├── prompts/               ← task_planner.md / bt_builder.md / bt_fix.md
│   │   └── validators/            ← capability / safety / structure checks
│   └── execution/
│       ├── engine.py              ← BehaviourTree lifecycle wrapper
│       ├── tree_loader.py         ← JSON → py_trees nodes
│       ├── blackboard_sync.py     ← Zenoh entity state → Blackboard
│       ├── behaviours/            ← CommandAction / HumanGate / ZenohCondition
│       ├── command/               ← CommandResolver / Translators / Adapters
│       ├── fsm/                   ← RobotFSM / HumanFSM / FSMManager
│       └── trace/                 ← SnapshotPublisher
└── tests/
    ├── test_validators.py
    ├── test_tree_loader.py
    ├── test_fsm_manager.py
    ├── test_command_resolver.py
    └── test_graph.py              ← End-to-end pipeline with mocked LLM
```

---

## Quick Start

### Prerequisites

```bash
# Start Zenoh router (shared with Theia IDE)
zenohd --cfg zenoh-config.json5
```

### Install & Run

#### 安装 uv（如未安装）

```powershell
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

#### 安装项目依赖

```bash
cd hmta-service
cp .env.example .env
# 编辑 .env，填入 OPENAI_API_KEY 等配置
```

```bash
# 使用 uv sync 一步创建虚拟环境并安装所有依赖（含 dev 依赖）
uv sync --extra dev
```

> **说明：** `uv sync` 会自动在项目目录下创建 `.venv` 虚拟环境，无需手动 `uv venv`。
> 支持 Python ≥ 3.11，推荐使用 3.12+。

#### 激活环境并运行

```powershell
# Windows (PowerShell)
.venv\Scripts\Activate.ps1
python -m app.main
```

```bash
# macOS / Linux
source .venv/bin/activate
python -m app.main
```

或直接通过 `uv run` 运行，无需手动激活虚拟环境：

```bash
uv run python -m app.main
# or:
uv run uvicorn app.main:app_fastapi --reload
```

Service starts on `http://localhost:8000`.

### Health check

```bash
curl http://localhost:8000/api/v1/health
```

### HTTP fallback generation

```bash
curl -X POST http://localhost:8000/api/v1/generate \
  -H "Content-Type: application/json" \
  -d '{
    "task_id": "demo_task",
    "entities": [],
    "environment": {},
    "task_context": {},
    "options": {
      "llm_model": "gpt-4o"
    }
  }'
```

On success the service validates the BT and loads it into `ExecutionEngine`, but does not auto-start ticking.

### Run tests

```bash
pytest tests/ -v
```

---

## Key Design Choices

| Decision | Reason |
|----------|--------|
| **LangGraph StateGraph** | Natively supports directed graph with conditional edges (retry loop), typed State, and LangSmith tracing |
| **LCEL chains** (`prompt \| llm \| parser`) | Composable, async-first, easy to swap models or add fallbacks |
| **JsonOutputParser + Pydantic** | Type-safe structured output from LLM — validator catches schema violations immediately |
| **py_trees** | Mature ROS-validated BT framework; replaces ~650 lines of hand-written TypeScript |
| **python-statemachine** | Declarative FSM definition (code = documentation), built-in guards and listeners |
| **Zenoh** | Unified pub/sub bus — decouples generation service from IDE, supports multiple consumers for the same result |

---

## Zenoh Topics

| Topic | Direction | Purpose |
|-------|-----------|---------|
| `zho/bt/generate/request` | Theia → Service | Generation request |
| `zho/bt/generate/{id}/progress` | Service → Theia | Per-step progress |
| `zho/bt/generate/{id}/result` | Service → Theia | Final BT + FSM + BB |
| `zho/bt/generate/{id}/error` | Service → Theia | Error notification |
| `zho/bt/execution/tick` | Service → Theia | Per-tick node status snapshot |
| `zho/bt/execution/status` | Service → Theia | Overall tree status |
| `zho/command/{entity_id}` | Service → UE5 | Robot command (L3) |
| `zho/directive/{entity_id}` | Service → Theia | Human directive (L3) |
| `zho/response/{directive_id}` | Theia → Service | Human response |
| `zho/device/*/callback` | UE5 → Service | Device events |
| `zho/entity/*/state` | UE5 → Service | Entity state updates |

---

## LLM Configuration

The service currently supports official OpenAI and OpenAI-compatible endpoints via:

```bash
OPENAI_API_KEY=...
OPENAI_BASE_URL=
```

`ANTHROPIC_API_KEY` and `COZE_API_KEY` are reserved for future integrations and are not wired yet.

Default models from `.env`:

```
PLANNER_MODEL=gpt-4o        # Task Planner
BUILDER_MODEL=gpt-4o        # BT Builder
FIXER_MODEL=gpt-4o-mini     # BT repair (cheaper, faster)
```

Per-request overrides:

- `options.llm_model`: override all planner/builder/fixer models with one value
- `options.models.planner`: override only Task Planner
- `options.models.builder`: override only BT Builder
- `options.models.fixer`: override only BT repair

LangSmith tracing (optional):
```
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=ls__...
LANGCHAIN_PROJECT=hmta-service
```
