"""HMTA Service entry point — wires Zenoh, LangGraph generation, and py_trees execution."""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
from concurrent.futures import Future
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import uvicorn
import yaml
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError

from app.api.capability import router as capability_router
from app.api.experiment import experiment_router
from app.api.health import router as health_router
from app.capability.registry import CapabilityRegistry
from app.config import settings
from app.log_setup import setup_logging
from app.capability.allocator import set_experiment_controller, set_capability_registry
from app.experiment.collector import PerformanceCollector
from app.experiment.controller import ExperimentController
from app.experiment.proficiency_store import ProficiencyStore
from app.experiment.store import ExperimentStore
from app.execution.command.command_resolver import CommandResolver
from app.execution.command.device_adapter import DeviceAdapter
from app.execution.command.human_adapter import HumanAdapter
from app.execution.command.response_resolver import ResponseResolver
from app.execution.engine import ExecutionEngine
from app.execution.fsm.fsm_manager import FSMManager
from app.execution.human_interaction.edit_handler import EditHandler
from app.execution.human_interaction.intervention_handler import InterventionHandler
from app.generation.graph.coordinator import get_graph
from app.generation.graph.feedback_pipeline import build_feedback_graph
from app.generation.graph.progress import publish_step, set_context
from app.generation.service import build_initial_state, build_result_payload, build_task_record
from app.schemas.api import GenerationRequest, GenerationTaskRecord
from app.zenoh_bridge import ZenohBridge

# ── Logging setup ──────────────────────────────────────────────────────────────

_log_run_dir = setup_logging(log_level=settings.log_level, logs_root="logs")
logger = logging.getLogger("hmta")

# ── FastAPI app ────────────────────────────────────────────────────────────────


# ── Application state (shared across routes) ──────────────────────────────────

class AppState:
    def __init__(self):
        self.zenoh_bridge: ZenohBridge = ZenohBridge()
        self.fsm_manager: FSMManager = FSMManager()
        self.schema_registry: dict = {}
        # HyperGraph-backed capability registry (replaces plain dict)
        self.capability_registry: CapabilityRegistry = CapabilityRegistry(
            on_capabilities_changed=_on_capabilities_changed,
        )
        self.command_resolver: CommandResolver | None = None
        self.response_resolver: ResponseResolver | None = None
        self.engine: ExecutionEngine | None = None
        self.edit_handler: EditHandler | None = None
        self.intervention_handler: InterventionHandler | None = None
        self.experiment_store: ExperimentStore | None = None
        self.proficiency_store: ProficiencyStore | None = None
        self.experiment_controller: ExperimentController | None = None
        self.performance_collector: PerformanceCollector | None = None
        self.oracle_service = None
        self.task_registry: dict[str, dict] = {}
        self.main_loop: asyncio.AbstractEventLoop | None = None


def _on_capabilities_changed(entity_id: str, added: set[str], removed: set[str]) -> None:
    """Callback from CapabilityRegistry when a human's effective capabilities change."""
    if added:
        logger.info("[CapReg] %s gained capabilities: %s", entity_id, sorted(added))
    if removed:
        logger.warning("[CapReg] %s lost capabilities: %s — consider replan", entity_id, sorted(removed))


_state = AppState()

# ── Human profiles (static device / cognitive config) ─────────────────────────

_HUMAN_PROFILES_YAML = Path(__file__).resolve().parents[1] / "configs" / "human-profiles.yaml"


def _load_human_profiles() -> dict[str, Any]:
    """Load human-profiles.yaml and return the `profiles` mapping."""
    if not _HUMAN_PROFILES_YAML.exists():
        logger.warning("human-profiles.yaml not found at %s", _HUMAN_PROFILES_YAML)
        return {}
    with open(_HUMAN_PROFILES_YAML, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    profiles = data.get("profiles", {})
    logger.info("Loaded %d human profile(s) from %s", len(profiles), _HUMAN_PROFILES_YAML.name)
    return profiles


_human_profiles: dict[str, Any] = {}

_HUMAN_DIRECTIVE_CAPABILITIES = {
    "path_planning", "plan_path", "plan_patrol", "plan_route",
    "command.approve", "approve", "approval",
    "observe", "observation", "reconnaissance", "survey",
    "command.override", "override", "manual_control",
    "system.report", "report",
    "goal_confirmation",
}

# ── Startup / Shutdown ─────────────────────────────────────────────────────────

async def _startup() -> None:
    logger.info("HMTA Service starting…")
    logger.info("📁 Log directory: %s", _log_run_dir.resolve())
    _state.main_loop = asyncio.get_running_loop()
    app_fastapi.state.main_loop = _state.main_loop

    # 1. Open Zenoh session
    _state.zenoh_bridge.open()

    # 2. Wire command layer
    # CommandResolver uses a plain dict for legacy capability lookup;
    # the HyperGraph-backed CapabilityRegistry lives in _state.capability_registry.
    _state.command_resolver = CommandResolver(
        schema_registry=_state.schema_registry,
        capability_registry={},
        zenoh_bridge=_state.zenoh_bridge,
    )
    _state.response_resolver = ResponseResolver(_state.command_resolver)

    # 3. Wire execution engine
    _state.engine = ExecutionEngine(
        zenoh_bridge=_state.zenoh_bridge,
        command_resolver=_state.command_resolver,
        fsm_manager=_state.fsm_manager,
    )
    app_fastapi.state.engine = _state.engine

    # 4. Start device + human adapters
    DeviceAdapter(
        zenoh_bridge=_state.zenoh_bridge,
        fsm_manager=_state.fsm_manager,
        command_resolver=_state.command_resolver,
    ).start()
    HumanAdapter(
        zenoh_bridge=_state.zenoh_bridge,
        response_resolver=_state.response_resolver,
    ).start()

    # 4b. Start human interaction handlers (edit + intervention)
    _state.edit_handler = EditHandler(
        zenoh=_state.zenoh_bridge,
        engine=_state.engine,
    )
    _state.edit_handler.start()

    _state.intervention_handler = InterventionHandler(
        zenoh=_state.zenoh_bridge,
        engine=_state.engine,
    )
    _state.intervention_handler.start()

    # 4c. Experiment data collection
    _state.experiment_store = ExperimentStore()
    _state.performance_collector = PerformanceCollector(
        store=_state.experiment_store,
        zenoh=_state.zenoh_bridge,
    )
    _state.engine.set_performance_collector(_state.performance_collector)

    # 4d. Experiment controller (plan management + allocation overrides)
    _state.experiment_controller = ExperimentController()
    set_experiment_controller(_state.experiment_controller)
    set_capability_registry(_state.capability_registry)

    # 4e. Dynamic reallocation callback
    _state.engine.set_reallocation_callback(_on_reallocation_trigger)
    logger.info("Experiment store + controller + performance collector initialised")

    # 4e-2. Unified proficiency store (same experiments.db, three new tables)
    _state.proficiency_store = ProficiencyStore()
    persisted_proficiency = _state.proficiency_store.load_all_current()
    if persisted_proficiency:
        _state.capability_registry.load_persisted_proficiency(persisted_proficiency)
        logger.info(
            "Restored %d persisted proficiency values into CapabilityRegistry",
            len(persisted_proficiency),
        )

    # 4e-3. Oracle service (ground-truth capability judgment)
    try:
        _oracle_cfg = _load_capability_ontology_config()
        from app.oracle.ground_truth_store import GroundTruthStore
        from app.oracle.oracle_service import OracleService

        _ground_truth_store = GroundTruthStore()
        _state.oracle_service = OracleService(
            zenoh=_state.zenoh_bridge,
            ground_truth=_ground_truth_store,
            config=_oracle_cfg,
            store=_state.proficiency_store,
        )
        _state.engine.set_oracle_service(_state.oracle_service)
        _state.zenoh_bridge.subscribe_ground_truth(
            _state.oracle_service.on_ground_truth_received,
        )
        logger.info("OracleService initialised and wired")
    except Exception:
        logger.exception("OracleService setup failed — oracle judgments disabled")

    # 4f. Feedback pipeline (capability learning after mission conclusion)
    try:
        _capability_ontology_cfg = _load_capability_ontology_config()
        feedback_graph = build_feedback_graph(
            _state.zenoh_bridge,
            _state.capability_registry,
            _capability_ontology_cfg,
            store=_state.proficiency_store,
        )
        _state.engine.set_feedback_pipeline(feedback_graph)
        _state.zenoh_bridge.subscribe_proposal_confirmations(_apply_confirmed_handler)
        logger.info("Feedback pipeline compiled and wired")
    except Exception:
        logger.exception("Feedback pipeline setup failed — capability learning disabled")

    # 5. Warm up the LangGraph compilation (avoids "unhashable type: dict" on first call)
    try:
        get_graph()
        logger.info("LangGraph pipeline compiled and ready")
    except Exception:
        logger.exception("LangGraph warm-up failed — generation will compile on first request")

    # 6. Load human profiles (static device/cognitive config)
    global _human_profiles
    _human_profiles = _load_human_profiles()

    # 7. Subscribe to generation requests (driven by Theia backend)
    _state.zenoh_bridge.subscribe_generation_requests(_handle_generation_request)

    # 8. Subscribe to execution start/stop commands
    _state.zenoh_bridge.subscribe_execution_commands(_handle_execution_start)
    _state.zenoh_bridge.subscribe_execution_stop(_handle_execution_stop)

    # 9. Subscribe to entity registry (human + robot registration from UE / mock)
    _state.zenoh_bridge.subscribe_entity_registry(_on_entity_registry)

    # 10. Subscribe to entity state (runtime telemetry — pose, cognitive, device status)
    _state.zenoh_bridge.subscribe_entity_states(_on_entity_state)

    logger.info("HMTA Service ready — Zenoh router: %s", settings.zenoh_router)


async def _shutdown() -> None:
    if _state.engine:
        _state.engine.stop()
    _state.main_loop = None
    _state.zenoh_bridge.close()
    logger.info("HMTA Service stopped")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.zenoh_bridge = _state.zenoh_bridge
    app.state.fsm_manager = _state.fsm_manager
    app.state.task_registry = _state.task_registry
    app.state.capability_registry = _state.capability_registry
    await _startup()
    app.state.experiment_store = _state.experiment_store
    app.state.proficiency_store = _state.proficiency_store
    app.state.experiment_controller = _state.experiment_controller
    try:
        yield
    finally:
        await _shutdown()


app_fastapi = FastAPI(
    title="HMTA Service",
    description="Human-Machine Task Allocation — LangGraph generation + py_trees BT execution",
    version="0.1.0",
    lifespan=lifespan,
)

app_fastapi.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app_fastapi.include_router(health_router)
app_fastapi.include_router(capability_router)
app_fastapi.include_router(experiment_router)


# ── Generation request handler ────────────────────────────────────────────────

_execution_thread: threading.Thread | None = None


def _handle_execution_start(data: dict) -> None:
    """Called by Zenoh subscriber — start the py_trees tick loop in a background thread."""
    global _execution_thread

    engine = _state.engine
    if engine is None:
        logger.error("Execution start received but engine is not initialized")
        return

    if not engine.tree:
        logger.warning("Execution start received but no BT loaded")
        _state.zenoh_bridge.publish_execution_status("error", "No BT loaded — generate a BT first")
        return

    if engine._running:
        logger.info("Execution start received but engine is already running")
        return

    tick_period = data.get("tick_period_ms", 200)
    logger.info("Execution start command received — launching tick loop (period=%dms)", tick_period)
    _state.zenoh_bridge.publish_execution_status("starting")

    def _run() -> None:
        try:
            engine.start(tick_period_ms=tick_period)
        except Exception:
            logger.exception("ExecutionEngine tick loop crashed")
            _state.zenoh_bridge.publish_execution_status("error", "Tick loop crashed")

    _execution_thread = threading.Thread(target=_run, name="bt-ticker", daemon=True)
    _execution_thread.start()


def _handle_execution_stop(data: dict) -> None:
    """Called by Zenoh subscriber — stop the py_trees tick loop."""
    engine = _state.engine
    if engine is None:
        logger.error("Execution stop received but engine is not initialized")
        return

    if not engine._running:
        logger.info("Execution stop received but engine is not running — ignoring")
        return

    tree_id = data.get("tree_id", "unknown")
    logger.info("Execution stop command received — halting tick loop for tree: %s", tree_id)
    engine.stop()
    _state.zenoh_bridge.publish_execution_status("stopped", f"Manually stopped by operator (tree={tree_id})")
    logger.info("ExecutionEngine stopped successfully")


def _handle_generation_request(data: dict) -> None:
    """Called by Zenoh subscriber thread — forwards work to the FastAPI loop safely."""
    loop = _state.main_loop
    task_id = data.get("task_id", "unknown")

    if loop is None:
        logger.error("Generation request received before startup completed: task_id=%s", task_id)
        _state.zenoh_bridge.publish_generation_error(task_id, "HMTA Service is not ready yet")
        return

    future = asyncio.run_coroutine_threadsafe(_run_generation(data), loop)
    future.add_done_callback(lambda done: _log_generation_future(task_id, done))


def _store_task_record(record: GenerationTaskRecord) -> None:
    _state.task_registry[record.task_id] = record.model_dump()


async def _run_generation(request: dict) -> GenerationTaskRecord:
    import time as _time
    mission_started_at = _time.time() * 1000  # epoch ms

    bridge = _state.zenoh_bridge
    task_id = request.get("task_id", "unknown")
    logger.info("Generation request received: task_id=%s (mission_started_at=%.0f)", task_id, mission_started_at)

    bridge.publish("zho/bt/mission_started", {
        "mission_started_at": mission_started_at,
        "task_id": task_id,
    })

    try:
        payload = GenerationRequest.model_validate(request)
    except ValidationError as exc:
        message = f"Invalid generation request: {exc}"
        logger.warning("Rejected invalid generation request: task_id=%s", task_id)
        failure = GenerationTaskRecord(task_id=task_id, status="failed", error=message)
        _store_task_record(failure)
        bridge.publish_generation_error(task_id, message)
        return failure

    task_id = payload.task_id or task_id
    graph = get_graph()
    initial_state = build_initial_state(payload, task_id)

    try:
        # Set up progress context so pipeline nodes can publish via Zenoh
        set_context(bridge, task_id)
        bridge.publish_progress(task_id, "pipeline", "started",
                                message="已发送请求到 HMTA Service，开始生成管线…")

        planning_start = _time.monotonic()
        final_state = await graph.ainvoke(
            initial_state,
            config={"callbacks": [_make_progress_callback(task_id, bridge)]},
        )
        planning_duration_ms = int((_time.monotonic() - planning_start) * 1000)
        logger.info("Planning completed in %dms for task_id=%s", planning_duration_ms, task_id)

        result = build_task_record(task_id, final_state)
        _store_task_record(result)

        n_alloc = len(result.allocation_trace or [])
        if n_alloc:
            logger.info(
                "Generation allocation_trace: task_id=%s subtasks_traced=%d (see result payload / Zenoh)",
                task_id,
                n_alloc,
            )

        # Notify collector of generation results (extract f_i, x_i)
        if _state.performance_collector:
            try:
                _state.performance_collector.on_generation_complete(task_id, final_state)
            except Exception:
                logger.exception("PerformanceCollector.on_generation_complete failed")

        if result.status != "loaded":
            logger.warning("Generation produced invalid BT: task_id=%s", task_id)
            bridge.publish_generation_error(task_id, result.error or "Behavior tree validation failed")
            return result

        if _state.engine is None:
            raise RuntimeError("Execution engine is not available")

        _state.engine.load(
            bt_json=result.behavior_tree or {},
            bb_init=result.blackboard_init,
            fsm_defs=result.fsm_definitions,
            task_id=task_id,          # align engine._task_id with PerformanceCollector
        )

        # Write mission timing to Blackboard for engine to read at conclusion
        import py_trees
        bb_storage = py_trees.blackboard.Blackboard.storage
        bb_storage["/mission_started_at"] = mission_started_at
        bb_storage["mission_started_at"] = mission_started_at
        bb_storage["/planning_duration_ms"] = planning_duration_ms
        bb_storage["planning_duration_ms"] = planning_duration_ms

        # Inject planning phase into the Gantt profiler (system lane)
        _state.engine.inject_planning_record(planning_duration_ms, mission_started_at)

        # Sync capabilities from the HyperGraph-backed registry into
        # CommandResolver so dispatch works even if UE registration
        # events were published before this service subscribed.
        if _state.command_resolver and _state.capability_registry:
            _state.command_resolver.sync_capabilities_from_graph(
                _state.capability_registry,
            )

        # Re-push detection config now that the schema is fully populated.
        # (The first push inside engine.load() may have found an empty schema
        # if entity registry events arrived after this service started.)
        if _state.engine:
            _state.engine.push_detection_configs_now()

        n_nodes = len((result.behavior_tree or {}).get("nodes", {}))
        publish_step("bt_loaded", "completed",
                     message=f"行为树已加载, {n_nodes} 个节点, 等待审批执行")

        if _state.oracle_service:
            _objective = (payload.task_context or {}).get("objective", "")
            _mission_goal = final_state.get("mission_goal")
            _entity_ids = [e.get("entity_id", e.get("entityId", "")) for e in (payload.entities or [])]
            _task_queue = (result.blackboard_init or {}).get("entries", {}).get("task_queue", [])
            _state.oracle_service.publish_mission_goal(
                objective=_objective,
                mission_goal=_mission_goal,
                task_count=len(_task_queue) if isinstance(_task_queue, list) else 0,
                entities=[e for e in _entity_ids if e],
            )

        bridge.publish_generation_result(task_id, build_result_payload(result))
        logger.info(
            "Generation completed and BT loaded (execution not auto-started): task_id=%s",
            task_id,
        )
        return result

    except Exception as exc:
        logger.exception("Generation failed: task_id=%s", task_id)
        failure = build_task_record(task_id, locals().get("final_state", {}), error=str(exc))
        _store_task_record(failure)
        bridge.publish_generation_error(task_id, str(exc))
        return failure


def _log_generation_future(task_id: str, future: Future) -> None:
    """Log any unexpected scheduling failure after handing off to the main loop."""
    try:
        future.result()
    except Exception:
        logger.exception("Background generation task crashed: task_id=%s", task_id)


def _make_progress_callback(task_id: str, bridge: ZenohBridge):
    """Create a LangChain callback that publishes step progress to Zenoh."""
    from langchain_core.callbacks import BaseCallbackHandler

    class ProgressCallback(BaseCallbackHandler):
        def on_chain_start(self, serialized, inputs, **kwargs):
            name = (serialized or {}).get("name", "unknown")
            bridge.publish_progress(task_id, name, "started")

        def on_chain_end(self, outputs, **kwargs):
            pass   # step-level "completed" is published inside each node

    return ProgressCallback()


# ── Entity registry handlers ───────────────────────────────────────────────────

def _resolve_human_profile(entity_id: str, data: dict) -> dict:
    """Look up the human profile in human-profiles.yaml using a three-step strategy.

    1. ``metadata.profile`` — explicit hint UE can set without changing its schema
       e.g.  metadata: { "profile": "operator-01" }
    2. ``entity_id`` — direct key match (works when IDs are clean like "operator-01")
    3. ``display_name`` — fallback for UE class-name entity IDs
    """
    metadata = data.get("metadata") or {}

    # 1. Explicit profile hint in metadata
    hint = metadata.get("profile") or metadata.get("profile_id")
    if hint and hint in _human_profiles:
        logger.info("[Registry] %s: profile resolved via metadata.profile = '%s'", entity_id, hint)
        return _human_profiles[hint]

    # 2. Direct entity_id match
    if entity_id in _human_profiles:
        return _human_profiles[entity_id]

    # 3. display_name match (UE class names often embed a meaningful name)
    display_name = data.get("display_name", "")
    for key, prof in _human_profiles.items():
        if prof.get("display_name") == display_name or key == display_name:
            logger.info("[Registry] %s: profile resolved via display_name = '%s'", entity_id, display_name)
            return prof

    return {}


def _on_entity_registry(entity_id: str, data: dict) -> None:
    """Handle entity registration messages from UE5 / mock tools.

    - Human: merges static device config from human-profiles.yaml when
      the registration lacks ``devices``, then calls
      ``register_human_with_devices()`` to build hypergraph nodes.
    - Robot: delegates to ``register_entity()`` for capability indexing.
    - Both paths also sync the legacy CommandResolver.

    UE sends human entities using a machine-registration template, so
    ``machine_type`` may be ``MT_ROBOT`` even for humans — ``category``
    (``ENTITY_HUMAN``) takes precedence for human detection.
    """
    category = data.get("category", "")
    entity_type = data.get("entity_type", "")
    action = data.get("action", "REG_REGISTER")

    if action not in ("REG_REGISTER", "register"):
        return  # ignore unregister / unknown actions here

    # category is the authoritative discriminator; machine_type may be wrong for humans
    is_human = category in ("ENTITY_HUMAN", "human") or entity_type == "human"
    is_object = category in ("ENTITY_OBJECT", "object") and not is_human

    # ── Device registration (ENTITY_OBJECT + metadata.owner + metadata.device_type) ──
    # UE registers wearable devices as objects with an owner link.
    # Route them into the capability registry as device nodes under the owner human.
    if is_object:
        metadata = data.get("metadata") or {}
        owner_id: str = metadata.get("owner", "")
        device_type: str = metadata.get("device_type") or metadata.get("deviceType", "")
        if owner_id and device_type:
            # Look up channel config for this device from human-profiles.yaml
            profile = _resolve_human_profile(owner_id, {"entity_id": owner_id})
            profile_devices: list[dict] = profile.get("devices", [])
            channels: list[str] = []
            for pd in profile_devices:
                if pd.get("type") == device_type or pd.get("device_id") == entity_id:
                    channels = pd.get("channels", [])
                    break

            _state.capability_registry.update_device_status(
                owner_id,
                [{"device_id": entity_id, "status": "online", "battery": 1.0}],
            )
            logger.info(
                "[Registry] %s: wearable device (%s) of %s — channels: %s",
                entity_id, device_type, owner_id, channels or "unknown",
            )
            return  # Do not register as a standalone object entity

    if is_human:
        payload = {"entity_id": entity_id, **data}

        # Supplement missing device/cognitive data from human-profiles.yaml
        if not data.get("devices"):
            profile = _resolve_human_profile(entity_id, data)
            if profile:
                payload.setdefault("authority_level", profile.get("authority_level", "operator"))
                payload.setdefault("role", profile.get("role", data.get("role", "")))
                payload["devices"] = profile.get("devices", [])
                if not payload.get("cognitive_profile"):
                    payload["cognitive_profile"] = profile.get("cognitive_profile", {})
                if profile.get("proficiency_overrides"):
                    payload["proficiency_overrides"] = profile["proficiency_overrides"]
                # Preserve UE metadata but merge profile metadata for missing keys
                merged_meta = {**profile.get("metadata", {}), **(payload.get("metadata") or {})}
                payload["metadata"] = merged_meta
                logger.info(
                    "[Registry] %s → profile '%s': supplemented %d device(s)",
                    entity_id, profile.get("display_name", "?"), len(payload["devices"]),
                )
            else:
                logger.warning(
                    "[Registry] %s: no profile match (tried metadata.profile / entity_id / display_name) — "
                    "add metadata.profile='<profile_key>' in UE to link a profile",
                    entity_id,
                )

        if payload.get("devices"):
            _state.capability_registry.register_human_with_devices(payload)
        else:
            # Register entity node only (no capability edges yet)
            _state.capability_registry.register_entity(payload)

    else:
        # Robot registration
        payload = {"entity_id": entity_id, **data}
        # Log the full raw payload to diagnose capability registration issues
        sc = data.get("structured_capabilities") or data.get("structuredCapabilities", [])
        flat = data.get("capabilities", [])
        logger.info(
            "[Registry] robot %s raw payload keys=%s  structured_caps=%s  flat_caps=%s",
            entity_id,
            sorted(data.keys()),
            [c.get("name") or c.get("id") if isinstance(c, dict) else c for c in sc],
            flat,
        )
        if sc or flat:
            # Normalise: HMTA registry.register_entity only reads snake_case fields
            if not data.get("structured_capabilities") and sc:
                payload["structured_capabilities"] = list(sc)
            _state.capability_registry.register_entity(payload)
        else:
            logger.warning(
                "[Registry] robot %s has NO capabilities in payload — check UE ZenohCap component field names",
                entity_id,
            )

    # Legacy sync for command_resolver
    if _state.command_resolver:
        cr_payload = {"entity_id": entity_id, **data}
        if is_human:
            cr_payload["capabilities"] = list(_HUMAN_DIRECTIVE_CAPABILITIES)
            cr_payload["entity_type"] = "human"
        elif not is_object:
            cr_payload["entity_type"] = "robot"
            # Normalise camelCase → snake_case so CommandResolver can find caps
            if not cr_payload.get("structured_capabilities") and cr_payload.get("structuredCapabilities"):
                cr_payload["structured_capabilities"] = cr_payload["structuredCapabilities"]
        _state.command_resolver.register_entity(cr_payload)

    logger.debug("[Registry] processed: %s (human=%s)", entity_id, is_human)


def _on_entity_state(entity_id: str, data: dict) -> None:
    """Keep schema/capability registries current from Zenoh entity state."""
    payload = {"entity_id": entity_id, **data}

    # --- CapabilityRegistry (hypergraph) sync ---
    category = data.get("category", "")
    entity_type = data.get("entity_type", "")
    is_human = category == "ENTITY_HUMAN" or entity_type == "human"

    if is_human:
        # Update device status if included in state message
        device_status_list = data.get("device_status", [])
        if device_status_list:
            _state.capability_registry.update_device_status(entity_id, device_status_list)
        # Register/refresh human if full devices info present
        if data.get("devices"):
            _state.capability_registry.register_human_with_devices(payload)
    else:
        # Robot: update capabilities from structured_capabilities / capabilities fields
        if data.get("structured_capabilities") or data.get("capabilities"):
            _state.capability_registry.register_entity(payload)

    # --- Legacy CommandResolver sync (keeps command dispatch working) ---
    if _state.command_resolver:
        if is_human:
            payload["capabilities"] = list(_HUMAN_DIRECTIVE_CAPABILITIES)
            payload["entity_type"] = "human"
        else:
            payload["entity_type"] = "robot"
        _state.command_resolver.register_entity(payload)


# ── Capability ontology config loader ─────────────────────────────────────────

_CAPABILITY_ONTOLOGY_YAML = Path(__file__).resolve().parents[1] / "configs" / "capability-ontology.yaml"


def _load_capability_ontology_config() -> dict:
    """Load capability-ontology.yaml and return the full config dict."""
    if not _CAPABILITY_ONTOLOGY_YAML.exists():
        return {}
    with open(_CAPABILITY_ONTOLOGY_YAML, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


# ── Feedback pipeline confirmation handler ─────────────────────────────────────

def _apply_confirmed_handler(data: dict) -> None:
    """Apply human-confirmed proficiency proposals to the CapabilityRegistry.

    Expected payload::

        {
            "task_id": "...",
            "confirmations": [
                {"entity_id": "...", "capability_id": "...", "approved_proficiency": 0.72},
                ...
            ]
        }
    """
    task_id = data.get("task_id", "unknown")
    confirmations: list[dict] = data.get("confirmations") or []
    registry = _state.capability_registry
    store = _state.proficiency_store

    applied = 0
    for conf in confirmations:
        entity_id = conf.get("entity_id", "")
        cap_id = conf.get("capability_id", "")
        new_prof = conf.get("approved_proficiency")
        accepted = conf.get("accepted", True)  # default True when approved_proficiency is present
        if not entity_id or not cap_id or new_prof is None:
            continue
        try:
            registry.update_proficiency(entity_id, cap_id, float(new_prof))
            applied += 1
        except Exception:
            logger.exception(
                "[Feedback] update_proficiency failed: %s/%s", entity_id, cap_id
            )
        if store is not None:
            try:
                store.confirm_proposal(
                    task_id=task_id,
                    entity_id=entity_id,
                    capability_id=cap_id,
                    confirmed_value=float(new_prof),
                    accepted=bool(accepted),
                )
            except Exception:
                logger.exception(
                    "[Feedback] confirm_proposal failed: %s/%s", entity_id, cap_id
                )

    logger.info(
        "[Feedback] applied %d/%d confirmed proficiency updates for task_id=%s",
        applied, len(confirmations), task_id,
    )

    # Publish the human_confirm step as done so the frontend step-strip updates
    if _state.zenoh_bridge and task_id != "unknown":
        _state.zenoh_bridge.publish_progress(task_id, "human_confirm", "completed",
                                              message=f"已确认 {applied} 项能力更新")


# ── Dynamic reallocation callback ──────────────────────────────────────────────

def _on_reallocation_trigger(triggers: list[dict]) -> None:
    """Called by ExecutionEngine when reallocation conditions are detected."""
    for trigger in triggers:
        reason = trigger.get("reason", "unknown")
        entity_id = trigger.get("entity_id", "")
        logger.warning(
            "[Realloc] trigger: reason=%s entity=%s details=%s",
            reason, entity_id, trigger,
        )
        _state.zenoh_bridge.publish(
            "zho/allocation/reallocation_trigger",
            {"reason": reason, "entity_id": entity_id, **trigger},
        )


# ── CLI entrypoint ─────────────────────────────────────────────────────────────

def main() -> None:
    uvicorn.run(
        "app.main:app_fastapi",
        host=settings.service_host,
        port=settings.service_port,
        reload=False,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
