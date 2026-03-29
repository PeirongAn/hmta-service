"""Execution Engine — py_trees BehaviourTree wrapper with full lifecycle management."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import py_trees

from app.execution.blackboard_sync import BlackboardSync
from app.execution.trace.profiler_publisher import ProfilerPublisher
from app.execution.trace.snapshot_publisher import SnapshotPublisher
from app.execution.tree_loader import load_tree

if TYPE_CHECKING:
    from app.execution.command.command_resolver import CommandResolver
    from app.execution.fsm.fsm_manager import FSMManager
    from app.zenoh_bridge import ZenohBridge

logger = logging.getLogger(__name__)

# Maps mission_goal.success_condition.key → default UE alert classes.
# Empty list means "alert on any detected class".
_GOAL_KEY_TO_ALERT_CLASSES: dict[str, list[str]] = {
    "bomb_detected":    ["bomb", "explosive", "ied", "suspicious"],
    "target_found":     [],
    "person_found":     ["person", "human", "body"],
    "threat_detected":  ["bomb", "gun", "weapon", "knife", "threat"],
}


class ExecutionEngine:
    """
    Wraps a py_trees BehaviourTree with:
    - load()    : BT JSON + Blackboard init + FSM definitions
    - start()   : begin tick_tock loop (10 Hz default, explicit call required)
    - stop()    : interrupt the loop
    - hot_swap(): replace the running tree without stopping
    """

    def __init__(
        self,
        zenoh_bridge: "ZenohBridge",
        command_resolver: "CommandResolver",
        fsm_manager: "FSMManager",
    ):
        self._zenoh = zenoh_bridge
        self._command_resolver = command_resolver
        self._fsm_manager = fsm_manager

        self.tree: py_trees.trees.BehaviourTree | None = None
        self._bb_sync: BlackboardSync | None = None
        self._snapshot_pub: SnapshotPublisher | None = None
        self._profiler_pub: ProfilerPublisher | None = None
        self._performance_collector = None
        self._reallocation_callback = None
        self._feedback_pipeline = None
        self._oracle_service = None
        self._consecutive_failures: dict[str, int] = {}
        self._task_id: str = ""
        self._current_phase_id: str = ""
        self._total_phases: int = 0
        self._running = False
        # Never-fail: track last published task queue snapshot for delta detection
        self._last_task_queue_snapshot: list[dict] = []
        self._last_ew_phase_snapshot: dict[str, str] = {}
        self._generic_mode: bool = False  # True when using the generic HMTA template
        self._handled_offline: set[str] = set()  # entities already cleaned up
        self._goal_concluded: bool = False
        # Plan-review timing: track when the BT was loaded so we can measure the
        # gap until the operator approves execution.
        self._loaded_at_ms: float = 0.0
        # Retained so main.py can re-push detection config after capability sync
        self._last_bb_init: dict = {}

    # ── Load ──────────────────────────────────────────────────────────────────

    def load(
        self,
        bt_json: dict,
        bb_init: dict,
        fsm_defs: list[dict],
        task_id: str | None = None,
    ) -> None:
        """
        Load a BT JSON and initialise Blackboard + FSM.
        Safe to call while stopped; performs full reload (stop + rebuild)
        if already running.

        *task_id* should be the generation-pipeline task_id so that
        PerformanceCollector.on_execution_complete() can match pending records.
        When omitted, falls back to bt_json["tree_id"] (legacy behaviour, may mismatch).
        """
        if self._running and self.tree:
            logger.info("Engine was running — stopping for full tree reload")
            self.stop()
            # _running is now False; fall through to full load

        # ── Initialise Blackboard BEFORE loading the tree ─────────────────────
        # py_trees enforces per-key write ownership: the client that registers
        # a key first owns it. Initialise before load_tree() so that behaviour
        # nodes (which may register the same keys for READ) don't take ownership.
        #
        # Ownership split:
        #   engine_init  → structural keys: zones/, condition node keys, etc.
        #   zenoh_sync   → entity runtime state: entities/*/position/status/comm_status/battery
        #   fsm_manager  → entity FSM state: entities/*/fsm_state
        py_trees.blackboard.Blackboard.enable_activity_stream(100)
        bb = py_trees.blackboard.Client(name="engine_init")
        for raw_key, value in (bb_init.get("entries") or {}).items():
            key = raw_key.lstrip("/")
            # Skip runtime entity state keys — zenoh_sync / fsm_manager own these
            if key.startswith("entities/"):
                continue
            try:
                bb.register_key(key=key, access=py_trees.common.Access.WRITE)
            except AttributeError:
                pass  # key already registered with WRITE by another client
            try:
                if value is not None:
                    bb.set(key, value)
            except Exception as exc:
                logger.warning("Blackboard init skipped key '%s': %s", key, exc)

        # Initialise mission lifecycle key (used by MissionMonitor + _check_mission_concluded)
        try:
            bb.register_key(key="mission_report_count", access=py_trees.common.Access.WRITE)
            bb.set("mission_report_count", 0)
        except AttributeError:
            pass

        # ── Build py_trees tree AFTER blackboard is initialised ────────────────
        self.tree = load_tree(bt_json)

        # Detect generic HMTA mode (template builder output)
        self._generic_mode = bt_json.get("metadata", {}).get("builder") == "bt_template_builder"
        self._goal_concluded = False
        self._handled_offline.clear()
        if self._generic_mode:
            logger.info("ExecutionEngine: generic HMTA mode (never-fail template BT)")

        # Load FSM instances
        self._fsm_manager.load_definitions(fsm_defs)

        # Blackboard ← Zenoh entity state
        # Pass command_resolver so BlackboardSync can infer command completion
        # from entity status transitions (UE does not send explicit callbacks).
        self._bb_sync = BlackboardSync(self._zenoh, command_resolver=self._command_resolver)
        if self._oracle_service:
            self._bb_sync.set_oracle_service(self._oracle_service)
            self._oracle_service.set_task_id(task_id or bt_json.get("tree_id", ""))
        self._bb_sync.start()

        # Snapshot publisher
        self._snapshot_pub = SnapshotPublisher(self._zenoh)
        self.tree.visitors.append(self._snapshot_pub.visitor)

        # Profiler publisher (Gantt data)
        # Prefer explicit task_id (passed from the generation pipeline) so that
        # PerformanceCollector.on_execution_complete() can reliably match pending
        # records by task_id.  Fall back to bt_json["tree_id"] for compatibility.
        self._task_id = task_id or bt_json.get("tree_id", "")
        self._current_phase_id = bt_json.get("metadata", {}).get("phase_id", "")
        self._total_phases = bt_json.get("metadata", {}).get("total_phases", 0)
        self._profiler_pub = ProfilerPublisher(self._zenoh, task_id=self._task_id)

        # Inject dependencies into custom nodes
        self._inject_dependencies(self.tree.root)

        # Retain bb_init so push_detection_configs_now() can be called again
        # after capability sync (entities may not be in schema yet at this point)
        self._last_bb_init = bb_init
        self._push_detection_configs(bb_init)

        import time as _time
        self._loaded_at_ms = _time.time() * 1000
        self._zenoh.publish_execution_status("loaded")
        logger.info("ExecutionEngine loaded BT with %d root children", len(self.tree.root.children))

    # ── Detection config push ─────────────────────────────────────────────────

    @staticmethod
    def _extract_detection_config(bb_init: dict) -> tuple[list[str], str | None, float]:
        """Derive ``(alert_classes, goal_key, min_confidence)`` from blackboard init.

        Sources (merged):
          1. ``mission_goal.success_condition.key`` → mapped via ``_GOAL_KEY_TO_ALERT_CLASSES``
          2. ``task_queue[*].params.target_classes`` from detect/scan tasks
        """
        entries = bb_init.get("entries") or {}
        classes: set[str] = set()
        goal_key: str | None = None
        min_conf = 0.3  # sensible default for persistent alert monitoring

        mission_goal = entries.get("mission_goal")
        if isinstance(mission_goal, dict):
            cond = mission_goal.get("success_condition")
            if isinstance(cond, dict) and cond.get("key"):
                goal_key = cond["key"]
                base = _GOAL_KEY_TO_ALERT_CLASSES.get(goal_key, [])
                classes.update(base)

        task_queue = entries.get("task_queue") or []
        for task in task_queue:
            intent = (task.get("intent") or "").lower()
            if intent not in ("detect", "scan", "surveil", "observe"):
                continue
            params = task.get("params") or {}
            tc = params.get("target_classes", "")
            if tc:
                classes.update(c.strip().lower() for c in str(tc).split(",") if c.strip())
            mc = params.get("min_confidence")
            if mc is not None:
                try:
                    min_conf = min(min_conf, float(mc))
                except (TypeError, ValueError):
                    pass

        return sorted(classes), goal_key, min_conf

    def _push_detection_configs(self, bb_init: dict) -> None:
        """Publish detection config to all robot entities on mission load.

        Derives ``alert_classes`` from the mission goal and task queue, then
        pushes to ``actor/{id}/detection/config`` for each registered robot.
        Also configures BlackboardSync to use the same alert classes for
        incoming event matching.
        """
        alert_classes, goal_key, min_conf = self._extract_detection_config(bb_init)

        if not alert_classes and not goal_key:
            return

        config = {
            "alert_classes": alert_classes,
            "min_confidence": min_conf,
            "stop_on_alert": True,
            "stop_instead_of_pause": False,
        }

        # Configure BlackboardSync to recognise these classes in incoming events
        if self._bb_sync:
            self._bb_sync.configure_detection(alert_classes, goal_key)

        # Push to every robot entity known to CommandResolver
        pushed = 0
        for entity_id, entity in self._command_resolver._schema.items():
            if entity.get("entity_type") == "robot":
                self._zenoh.publish_detection_config(entity_id, config)
                pushed += 1

        if pushed:
            logger.info(
                "[ExecutionEngine] Pushed detection config to %d robot(s): "
                "alert_classes=%s goal_key=%s min_confidence=%.2f",
                pushed, alert_classes, goal_key, min_conf,
            )

    def push_detection_configs_now(self) -> None:
        """Re-push detection config using the bb_init from the last load().

        Call this after ``CommandResolver.sync_capabilities_from_graph()`` so
        that robots which are only in the capability registry (not yet in the
        schema at load-time) also receive the config.
        """
        if self._last_bb_init:
            self._push_detection_configs(self._last_bb_init)

    # ── Start / Stop / Pause / Resume ─────────────────────────────────────────

    def start(self, tick_period_ms: int = 100) -> None:
        """Start the tick loop (default 10 Hz)."""
        if not self.tree:
            raise RuntimeError("No BT loaded — call load() first")

        # Measure and inject the plan-review (approval wait) time into the Gantt.
        if self._profiler_pub and self._loaded_at_ms > 0:
            import time as _time
            review_ms = _time.time() * 1000 - self._loaded_at_ms
            if review_ms > 500:  # only record if operator actually spent time reviewing
                self._profiler_pub.inject_review_record(review_ms, self._loaded_at_ms)
            self._loaded_at_ms = 0.0

        self._running = True
        logger.info("ExecutionEngine starting @ %dHz", 1000 // tick_period_ms)

        try:
            self.tree.tick_tock(
                period_ms=tick_period_ms,
                pre_tick_handler=self._pre_tick,
                post_tick_handler=self._post_tick,
            )
        finally:
            self._running = False

    def stop(self, _natural_termination: bool = False) -> None:
        self._running = False
        if self.tree:
            self.tree.interrupt()
        if not _natural_termination:
            self._zenoh.publish_execution_status("stopped")
        logger.info("ExecutionEngine stopped%s", " (after natural termination)" if _natural_termination else "")

    def pause(self) -> None:
        self._running = False
        if self.tree:
            self.tree.interrupt()
        self._zenoh.publish_execution_status("paused")
        logger.info("ExecutionEngine paused")

    def resume(self, tick_period_ms: int = 100) -> None:
        if self.tree:
            self.start(tick_period_ms)

    # ── Hot swap ──────────────────────────────────────────────────────────────

    def hot_swap(self, new_bt_json: dict) -> None:
        """Replace the tree root in-place without stopping the tick loop."""
        if not self.tree:
            raise RuntimeError("No tree loaded")
        new_root = load_tree(new_bt_json).root
        self._inject_dependencies(new_root)
        # py_trees forbids replace_subtree on the root node.
        # Directly swap the root attribute — safe between ticks because
        # tick_tock reads self.root on every cycle (no cached local).
        self.tree.root = new_root
        logger.info("Hot-swap completed → new root: '%s'", new_root.name)

    def inject_planning_record(self, planning_ms: float, mission_started_at: float) -> None:
        """Forward planning-phase timing to the profiler so it appears on the Gantt chart."""
        if self._profiler_pub:
            self._profiler_pub.inject_planning_record(planning_ms, mission_started_at)

    def inject_review_record(self, review_ms: float, started_at_ms: float) -> None:
        """Forward plan-review (operator approval wait) timing to the profiler."""
        if self._profiler_pub:
            self._profiler_pub.inject_review_record(review_ms, started_at_ms)

    def set_performance_collector(self, collector) -> None:
        """Inject a PerformanceCollector (Sprint 1)."""
        self._performance_collector = collector

    def set_reallocation_callback(self, callback) -> None:
        """Inject a callback for dynamic reallocation triggers (Sprint 2)."""
        self._reallocation_callback = callback

    def set_feedback_pipeline(self, pipeline) -> None:
        """Inject the compiled FeedbackPipeline LangGraph (Sprint 3)."""
        self._feedback_pipeline = pipeline

    def set_oracle_service(self, oracle) -> None:
        """Inject the OracleService for ground-truth capability judgment."""
        self._oracle_service = oracle

    # ── Tick handlers ──────────────────────────────────────────────────────────

    def _pre_tick(self, tree: py_trees.trees.BehaviourTree) -> None:
        """Sync FSM states to Blackboard before each tick."""
        self._fsm_manager.sync_to_blackboard()

    def _post_tick(self, tree: py_trees.trees.BehaviourTree) -> None:
        """Publish snapshot + profiler data after each tick."""
        if self._snapshot_pub:
            self._snapshot_pub.publish(tree)
        if self._profiler_pub:
            self._profiler_pub.publish(tree)

        self._check_reallocation_triggers(tree)

        # Publish task queue updates (generic HMTA mode + fallback)
        self._publish_task_queue_update()

        # In generic HMTA mode, detect mission goal satisfaction and conclusion.
        if self._generic_mode:
            self._force_conclude_on_goal_met()
            self._check_mission_concluded()

        root_status = tree.root.status

        # Never-fail guarantee: in generic HMTA mode the root should never
        # terminate with FAILURE — that would indicate a programming error.
        # Log a warning and treat it as RUNNING to keep the tree alive.
        if self._generic_mode and root_status == py_trees.common.Status.FAILURE:
            logger.warning(
                "Generic HMTA BT root returned FAILURE (unexpected) — "
                "suppressing to keep tree alive. Check EntityWorker / Selector logic."
            )
            return  # Don't stop; the repeat decorator will restart next tick

        if root_status in (py_trees.common.Status.SUCCESS, py_trees.common.Status.FAILURE):
            status_str = "completed" if root_status == py_trees.common.Status.SUCCESS else "failed"
            detail = ""
            failed_node = ""
            failure_type = ""
            if root_status == py_trees.common.Status.FAILURE:
                detail, failed_node, failure_type = self._extract_failure_info(tree.root)
            self._zenoh.publish_execution_status(
                status_str,
                detail=detail,
                failed_node=failed_node,
                failure_type=failure_type,
            )
            logger.info(
                "BT execution finished: status=%s detail=%s failed_node=%s",
                status_str, detail or "(none)", failed_node or "(none)",
            )

            if self._current_phase_id:
                bb_snapshot = {}
                try:
                    bb_snapshot = dict(py_trees.blackboard.Blackboard.storage)
                except Exception:
                    pass
                self._zenoh.publish_phase_completed({
                    "task_id": self._task_id,
                    "phase_id": self._current_phase_id,
                    "status": status_str,
                    "detail": detail,
                    "total_phases": self._total_phases,
                    "blackboard_snapshot": {
                        k: v for k, v in bb_snapshot.items()
                        if isinstance(k, str) and not k.startswith("entities/")
                    },
                })
                logger.info(
                    "Phase completed: phase_id=%s status=%s",
                    self._current_phase_id, status_str,
                )

            if self._performance_collector and self._profiler_pub:
                try:
                    self._performance_collector.on_execution_complete(
                        task_id=self._task_id,
                        profiler_data=self._profiler_pub.get_summary(),
                        bb_snapshot=dict(py_trees.blackboard.Blackboard.storage),
                    )
                except Exception:
                    logger.exception("PerformanceCollector.on_execution_complete failed")

            self.stop(_natural_termination=True)

    def _collect_entity_worker_states(self) -> dict[str, dict]:
        """Walk the BT to collect state snapshots from all EntityWorker nodes."""
        from app.execution.behaviours.entity_worker import EntityWorker
        result: dict[str, dict] = {}
        def _walk(node: py_trees.behaviour.Behaviour) -> None:
            if isinstance(node, EntityWorker):
                snap = node.get_state_snapshot()
                result[snap["entity_id"]] = snap
            for child in getattr(node, "children", []):
                _walk(child)
        if self.tree:
            _walk(self.tree.root)
        return result

    def _publish_task_queue_update(self) -> None:
        """Publish task_queue state to Zenoh when any status has changed."""
        try:
            storage = py_trees.blackboard.Blackboard.storage
            queue: list[dict] | None = storage.get("/task_queue") or storage.get("task_queue")
            if not queue:
                return

            # Collect entity worker states (cheap — just reads instance attrs)
            ew_states = self._collect_entity_worker_states()

            # Compute lightweight snapshots for delta detection
            task_snapshot = [
                {"id": t.get("id"), "status": t.get("status"), "elapsed_ms": t.get("elapsed_ms")}
                for t in queue
            ]
            ew_phase_snapshot = {eid: s.get("phase", "") for eid, s in ew_states.items()}
            if (task_snapshot == self._last_task_queue_snapshot
                    and ew_phase_snapshot == self._last_ew_phase_snapshot):
                return  # No change — skip publish
            self._last_task_queue_snapshot = task_snapshot
            self._last_ew_phase_snapshot = ew_phase_snapshot

            import time as _time
            now_ms = int(_time.time() * 1000)
            total = len(queue)
            completed = sum(1 for t in queue if t.get("status") == "completed")
            failed = sum(1 for t in queue if t.get("status") == "failed")
            executing = sum(1 for t in queue if t.get("status") == "executing")
            pending = sum(1 for t in queue if t.get("status") == "pending")
            skipped = sum(1 for t in queue if t.get("status") == "skipped")

            # Build serialisable task list (omit large embedded data)
            tasks_out: list[dict] = []
            for t in queue:
                tasks_out.append({
                    "id": t.get("id"),
                    "intent": t.get("intent"),
                    "entity": t.get("entity"),
                    "status": t.get("status"),
                    "depends_on": t.get("depends_on") or [],
                    "bt_pattern": t.get("bt_pattern"),
                    "human_supervisor": t.get("human_supervisor"),
                    "description": t.get("description"),
                    "started_at": t.get("started_at"),
                    "completed_at": t.get("completed_at"),
                    "elapsed_ms": t.get("elapsed_ms"),
                    "failure_reason": t.get("failure_reason"),
                    "human_intervention_count": t.get("human_intervention_count", 0),
                    "human_intervention_ms": t.get("human_intervention_ms", 0),
                })

            payload = {
                "task_id": self._task_id,
                "timestamp_ms": now_ms,
                "tasks": tasks_out,
                "summary": {
                    "total": total,
                    "completed": completed,
                    "failed": failed,
                    "executing": executing,
                    "pending": pending,
                    "skipped": skipped,
                },
                "entity_workers": ew_states,
            }
            self._zenoh.publish_task_queue(payload)
        except Exception:
            logger.debug("_publish_task_queue_update failed silently", exc_info=True)

    _WELL_KNOWN_DETECTION_KEYS = (
        "bomb_detected", "target_found", "person_found", "threat_detected",
    )

    @staticmethod
    def _bb_get(storage: dict, key: str):
        """Read a Blackboard value, checking both /key and key formats."""
        val = storage.get(f"/{key}")
        if val is not None:
            return val
        return storage.get(key)

    def _is_mission_goal_satisfied(self, storage: dict) -> bool:
        """Check if the mission goal has been satisfied.

        Priority order:
        1. ``mission_goal_confirmed`` (set by GoalConfirmationGate after operator approval)
        2. Structured ``mission_goal`` with ``requires_confirmation=False`` → raw condition check
        3. Structured ``mission_goal`` with ``requires_confirmation=True`` → wait for gate
        4. Fallback: no ``mission_goal`` → check well-known detection keys directly
        """
        try:
            if self._bb_get(storage, "mission_goal_confirmed"):
                return True

            goal = self._bb_get(storage, "mission_goal")

            if goal:
                if goal.get("requires_confirmation", False):
                    return False
                cond = goal.get("success_condition")
                if not cond or not cond.get("key"):
                    return False
                cond_key = cond["key"]
                actual = self._bb_get(storage, cond_key)
                return actual == cond.get("expected", True)

            for key in self._WELL_KNOWN_DETECTION_KEYS:
                val = self._bb_get(storage, key)
                if val:
                    logger.info(
                        "[ExecutionEngine] Goal satisfied via well-known key fallback: %s=%s",
                        key, val,
                    )
                    return True
            return False
        except Exception:
            return False

    def _force_conclude_on_goal_met(self) -> None:
        """If the mission goal is confirmed, force all non-terminal tasks to skipped.

        Only fires after GoalConfirmationGate writes mission_goal_confirmed,
        or when requires_confirmation is false and raw condition is met.
        """
        if self._goal_concluded:
            return
        try:
            storage = py_trees.blackboard.Blackboard.storage
            if not self._is_mission_goal_satisfied(storage):
                return

            queue: list[dict] | None = self._bb_get(storage, "task_queue")
            if not queue:
                return

            import time as _time
            terminal = {"completed", "failed", "skipped"}
            forced = 0
            for task in queue:
                if task.get("status") not in terminal:
                    task["status"] = "skipped"
                    task["failure_reason"] = "mission_goal_met"
                    task["completed_at"] = _time.time()
                    forced += 1

            if forced:
                storage["/task_queue"] = queue
                storage["task_queue"] = queue
                logger.info(
                    "[ExecutionEngine] Mission goal met — forced %d tasks to skipped", forced,
                )

            self._goal_concluded = True
        except Exception:
            logger.debug("_force_conclude_on_goal_met failed", exc_info=True)

    def _check_mission_concluded(self) -> None:
        """Stop the engine once all tasks have reached a terminal state.

        Fires when either:
        - ``mission_report_count > 0`` (MissionMonitor detected completion), OR
        - The mission goal is satisfied (e.g. bomb_detected).
        AND all tasks in ``task_queue`` are in a terminal state.
        """
        try:
            storage = py_trees.blackboard.Blackboard.storage

            queue: list[dict] | None = self._bb_get(storage, "task_queue")
            if not queue:
                return

            terminal = {"completed", "failed", "skipped"}
            non_terminal = [
                (t.get("id"), t.get("status"))
                for t in queue
                if t.get("status") not in terminal
            ]
            if non_terminal:
                return

            report_count = self._bb_get(storage, "mission_report_count") or 0
            goal_met = self._is_mission_goal_satisfied(storage)
            if not report_count and not goal_met:
                return

            completed = sum(1 for t in queue if t.get("status") == "completed")
            failed = sum(1 for t in queue if t.get("status") == "failed")
            skipped = sum(1 for t in queue if t.get("status") == "skipped")
            detail = f"Mission concluded: {completed} completed, {failed} failed, {skipped} skipped"
            logger.info("[ExecutionEngine] %s", detail)

            self._publish_mission_summary(storage, queue, completed, failed, skipped)
            self._stop_all_entities(storage)

            self._zenoh.publish_execution_status("completed", detail=detail)
            self.stop(_natural_termination=True)

            # Launch feedback pipeline in a background thread so it does not
            # block the tick loop from exiting cleanly.
            if self._feedback_pipeline is not None:
                import threading as _threading
                from app.generation.graph.feedback_pipeline import run_feedback_pipeline

                profiler_summary = self._profiler_pub.get_summary() if self._profiler_pub else {}
                task_id = self._task_id
                pipeline = self._feedback_pipeline
                queue_snapshot = list(queue)

                _threading.Thread(
                    target=run_feedback_pipeline,
                    args=(pipeline, task_id, queue_snapshot, profiler_summary),
                    name="feedback-pipeline",
                    daemon=True,
                ).start()
                logger.info("[ExecutionEngine] feedback pipeline launched for task_id=%s", task_id)
        except Exception:
            logger.debug("_check_mission_concluded failed silently", exc_info=True)

    def _publish_mission_summary(
        self,
        storage: dict,
        queue: list[dict],
        completed: int,
        failed: int,
        skipped: int,
    ) -> None:
        """Build and publish a comprehensive mission summary to Zenoh."""
        import time as _time
        now_ms = _time.time() * 1000
        mission_started = storage.get("/mission_started_at", storage.get("mission_started_at", now_ms))
        planning_ms = storage.get("/planning_duration_ms", storage.get("planning_duration_ms", 0))
        total_ms = int(now_ms - mission_started)
        execution_ms = max(total_ms - planning_ms, 0)

        end_reason = (
            "mission_goal_met"
            if self._bb_get(storage, "mission_goal_confirmed")
            or self._is_mission_goal_satisfied(storage)
            else "queue_completed"
        )

        total_interventions = sum(t.get("human_intervention_count", 0) for t in queue)
        total_intervention_ms = sum(t.get("human_intervention_ms", 0) for t in queue)

        task_summaries = [
            {
                "id": t.get("id"),
                "intent": t.get("intent"),
                "entity": t.get("entity"),
                "status": t.get("status"),
                "elapsed_ms": t.get("elapsed_ms"),
                "human_intervention_count": t.get("human_intervention_count", 0),
                "human_intervention_ms": t.get("human_intervention_ms", 0),
            }
            for t in queue
        ]

        summary = {
            "mission_started_at": mission_started,
            "planning_duration_ms": planning_ms,
            "execution_duration_ms": execution_ms,
            "total_duration_ms": total_ms,
            "end_reason": end_reason,
            "total_tasks": len(queue),
            "completed": completed,
            "failed": failed,
            "skipped": skipped,
            "human_intervention_count": total_interventions,
            "human_intervention_ms": total_intervention_ms,
            "tasks": task_summaries,
        }

        # Oracle task-outcome judgment — issued once at mission conclusion.
        # Determines success/failure based on task intent and whether the human
        # confirmed the machine's result (e.g., bomb found → human confirms).
        if self._oracle_service:
            try:
                goal_confirmed = bool(self._bb_get(storage, "mission_goal_confirmed"))
                mission_goal = self._bb_get(storage, "mission_goal")
                self._oracle_service.judge_task_outcome(
                    mission_goal=mission_goal,
                    task_queue=queue,
                    end_reason=end_reason,
                    goal_confirmed=goal_confirmed,
                )
            except Exception:
                logger.debug(
                    "[ExecutionEngine] oracle judge_task_outcome failed", exc_info=True
                )

        self._zenoh.publish("zho/bt/mission_summary", summary)
        logger.info(
            "[ExecutionEngine] mission_summary published: total=%dms planning=%dms execution=%dms "
            "end_reason=%s interventions=%d intervention_ms=%d",
            total_ms, planning_ms, execution_ms,
            end_reason, total_interventions, total_intervention_ms,
        )

    def _stop_all_entities(self, storage: dict) -> None:
        """Send STOP commands to all robot entities when mission concludes."""
        try:
            for key in list(storage.keys()):
                if not isinstance(key, str):
                    continue
                # Match keys like /entities/dog1/status or entities/dog1/status
                stripped = key.lstrip("/")
                if stripped.startswith("entities/") and stripped.endswith("/status"):
                    parts = stripped.split("/")
                    if len(parts) >= 3:
                        entity_id = parts[1]
                        stop_cmd = {
                            "entity_id": entity_id,
                            "actionType": "STOP",
                            "nodeId": "mission_end",
                            "params": {},
                        }
                        self._zenoh.publish_robot_command(entity_id, stop_cmd)
                        logger.info("[ExecutionEngine] Sent STOP to entity %s", entity_id)
        except Exception:
            logger.debug("_stop_all_entities failed", exc_info=True)

    @staticmethod
    def _extract_failure_info(node: py_trees.behaviour.Behaviour) -> tuple[str, str, str]:
        """Walk the tree depth-first and find the first FAILURE leaf with a feedback_message."""
        stack = [node]
        while stack:
            current = stack.pop()
            if current.status == py_trees.common.Status.FAILURE:
                msg = getattr(current, "feedback_message", "") or ""
                if msg:
                    ftype = ""
                    for prefix in ("ENTITY_NOT_FOUND", "ENTITY_OFFLINE", "INVALID_TARGET",
                                   "CAPABILITY_MISMATCH", "COMM_LOST", "DISPATCH_ERROR"):
                        if prefix in msg:
                            ftype = prefix
                            break
                    if not ftype and "timeout" in msg.lower():
                        ftype = "TIMEOUT"
                    return msg, current.name, ftype
            for child in reversed(getattr(current, "children", [])):
                stack.append(child)
        return "unknown failure", node.name, ""

    def _check_reallocation_triggers(self, tree: py_trees.trees.BehaviourTree) -> None:
        """Detect conditions that warrant dynamic reallocation."""
        try:
            bb_storage = py_trees.blackboard.Blackboard.storage
        except Exception:
            return

        triggers: list[dict] = []

        for key, val in list(bb_storage.items()):
            if not isinstance(key, str):
                continue
            # Entity offline or battery critically low
            if key.endswith("/status"):
                entity_id = key.rsplit("/status", 1)[0].split("/")[-1]
                if val in ("offline", "dead", "DEAD"):
                    if entity_id not in self._handled_offline:
                        # 只在首次检测到离线时触发一次，防止每 tick 重复回调
                        self._handled_offline.add(entity_id)
                        self._handle_entity_offline(entity_id)
                        triggers.append({"reason": "entity_offline", "entity_id": entity_id})
                else:
                    self._handled_offline.discard(entity_id)

            if key.endswith("/battery"):
                try:
                    battery = float(val)
                except (TypeError, ValueError):
                    continue
                if battery < 0.15:
                    entity_id = key.rsplit("/battery", 1)[0].split("/")[-1]
                    triggers.append({"reason": "low_battery", "entity_id": entity_id, "battery": battery})

            # Operator cognitive overload
            if key.endswith("/cognitive_load"):
                try:
                    load = float(val)
                except (TypeError, ValueError):
                    continue
                if load > 0.9:
                    entity_id = key.rsplit("/cognitive_load", 1)[0].split("/")[-1]
                    triggers.append({"reason": "cognitive_overload", "entity_id": entity_id, "load": load})

        # Consecutive subtask failures
        for child in getattr(tree.root, "children", []):
            name = child.name
            if child.status == py_trees.common.Status.FAILURE:
                self._consecutive_failures[name] = self._consecutive_failures.get(name, 0) + 1
                if self._consecutive_failures[name] >= 2:
                    triggers.append({"reason": "consecutive_failure", "subtask": name, "count": self._consecutive_failures[name]})
            elif child.status == py_trees.common.Status.SUCCESS:
                self._consecutive_failures.pop(name, None)

        if triggers and self._reallocation_callback:
            try:
                self._reallocation_callback(triggers)
            except Exception:
                logger.exception("Reallocation callback failed")

    # ── Entity offline handling ─────────────────────────────────────────────────

    def _handle_entity_offline(self, entity_id: str) -> None:
        """Fail executing tasks and reassign pending tasks for an offline entity."""
        logger.info("[ExecutionEngine] Handling entity offline: %s", entity_id)
        storage = py_trees.blackboard.Blackboard.storage
        queue: list[dict] | None = storage.get("/task_queue") or storage.get("task_queue")
        if not queue:
            return

        import time as _time
        changed = False
        for task in queue:
            if task.get("entity") != entity_id:
                continue
            status = task.get("status", "")
            if status == "executing":
                task["status"] = "failed"
                task["failure_reason"] = "entity_offline"
                task["completed_at"] = _time.time()
                changed = True
                logger.warning(
                    "Task %s failed: entity %s went offline during execution",
                    task.get("id"), entity_id,
                )
            elif status == "pending":
                replacement = self._find_replacement_entity(entity_id, task)
                if replacement:
                    task["entity"] = replacement
                    logger.info(
                        "Reassigned task %s: %s -> %s",
                        task.get("id"), entity_id, replacement,
                    )
                else:
                    task["status"] = "failed"
                    task["failure_reason"] = "entity_offline_no_replacement"
                    task["completed_at"] = _time.time()
                    logger.warning(
                        "Task %s failed: entity %s offline, no replacement available",
                        task.get("id"), entity_id,
                    )
                changed = True

        if changed:
            storage["/task_queue"] = queue
            storage["task_queue"] = queue

    def _find_replacement_entity(self, offline_id: str, task: dict) -> str | None:
        """Find another online entity with the required capability for *task*."""
        intent = task.get("intent", "")
        if not self._command_resolver:
            return None
        caps_map = getattr(self._command_resolver, "_capabilities", {})
        schema_map = getattr(self._command_resolver, "_schema", {})
        for eid, caps in caps_map.items():
            if eid == offline_id:
                continue
            schema = schema_map.get(eid, {})
            if schema.get("status") in ("offline", "dead"):
                continue
            if schema.get("comm_status") in ("offline", "comm_lost"):
                continue
            if intent and intent in caps:
                return eid
        return None

    # ── Dependency injection ───────────────────────────────────────────────────

    def _inject_dependencies(self, node: py_trees.behaviour.Behaviour) -> None:
        """Recursively inject command_resolver and zenoh into custom behaviour nodes."""
        if hasattr(node, "set_command_resolver"):
            node.set_command_resolver(self._command_resolver)
        if hasattr(node, "set_zenoh"):
            node.set_zenoh(self._zenoh)
        if hasattr(node, "set_fsm_manager"):
            node.set_fsm_manager(self._fsm_manager)
        if hasattr(node, "set_oracle_service") and self._oracle_service:
            node.set_oracle_service(self._oracle_service)
        children = getattr(node, "children", [])
        for child in children:
            self._inject_dependencies(child)
