"""EntityWorker — generic machine-entity task executor for the HMTA generic BT.

Manages a single entity's full task lifecycle from the Blackboard task queue:
  idle → executing → (success → idle | failure → human_fallback → idle)

Never returns FAILURE. If all tasks are done or failed, returns SUCCESS.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import py_trees

from app.schemas.command import AbstractCommand

logger = logging.getLogger(__name__)

_HUMAN_FALLBACK_TIMEOUT_SEC = 180.0
_COMMAND_TIMEOUT_SEC = 120.0
_HUMAN_FALLBACK_PATTERNS = frozenset({"supervised_fallback", "supervised", "human_plan"})

# Statuses where a command is considered terminal
_SUCCESS_STATES = frozenset({"completed", "success"})
_FAILURE_STATES = frozenset({"failed", "failure", "error", "cancelled"})


def _find_task(queue: list[dict], task_id: str) -> dict | None:
    for t in queue:
        if t.get("id") == task_id:
            return t
    return None


class EntityWorker(py_trees.behaviour.Behaviour):
    """
    Generic worker for a single machine entity.  Reads the ``task_queue``
    from the Blackboard, picks the next eligible pending task (respecting
    hypergraph depends_on edges), dispatches commands, handles failures
    via human fallback, and never propagates FAILURE to the parent.

    Phase transitions:
        idle         → pick next task → executing
        executing    → success → idle (pick next)
        executing    → failure + should_escalate → human_fallback
        executing    → failure + no escalation   → mark failed, idle
        human_fallback → response received → executing (retry with params)
        human_fallback → timeout             → mark failed, idle
        idle (no more tasks) → SUCCESS
    """

    def __init__(
        self,
        name: str,
        entity_id: str,
        human_supervisor_id: str = "operator-01",
        human_fallback_timeout_sec: float = _HUMAN_FALLBACK_TIMEOUT_SEC,
    ):
        super().__init__(name=name)
        self.entity_id = entity_id
        self.human_supervisor_id = human_supervisor_id
        self._fallback_timeout = human_fallback_timeout_sec

        self._command_resolver = None
        self._zenoh = None

        self._phase: str = "idle"
        self._current_task: dict | None = None
        self._command_id: str | None = None
        self._fallback_start: float = 0.0
        self._exec_start: float = 0.0

        # Multi-step execution: ordered list of intents to dispatch for the
        # current task, e.g. ["navigation", "scan"]. ``_step_index`` tracks
        # which step is currently in-flight.
        self._exec_steps: list[str] = []
        self._step_index: int = 0

        # Phase transition log for frontend state-machine visualisation
        self._transition_log: list[dict] = []

        # Blackboard client — owns task_queue read+write
        self._bb = py_trees.blackboard.Client(name=f"ew_{entity_id}")
        self._bb.register_key(key="task_queue", access=py_trees.common.Access.WRITE)

    # ── Dependency injection ───────────────────────────────────────────────────

    def set_command_resolver(self, resolver) -> None:
        self._command_resolver = resolver

    def set_zenoh(self, zenoh_bridge) -> None:
        self._zenoh = zenoh_bridge

    # ── State snapshot / transition log ──────────────────────────────────────

    _MAX_TRANSITION_LOG = 50

    def _record_transition(
        self, from_phase: str, to_phase: str, trigger: str, **extra: Any,
    ) -> None:
        entry = {"ts": time.time(), "from": from_phase, "to": to_phase,
                 "trigger": trigger, **extra}
        self._transition_log.append(entry)
        if len(self._transition_log) > self._MAX_TRANSITION_LOG:
            self._transition_log = self._transition_log[-self._MAX_TRANSITION_LOG:]

    def get_state_snapshot(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "phase": self._phase,
            "current_task_id": self._current_task.get("id") if self._current_task else None,
            "exec_steps": list(self._exec_steps),
            "step_index": self._step_index,
            "detection_halted": self._is_detection_active(),
            "fallback_elapsed_sec": (
                round(time.monotonic() - self._fallback_start, 1)
                if self._phase == "human_fallback" else None
            ),
            "exec_elapsed_sec": (
                round(time.monotonic() - self._exec_start, 1)
                if self._phase == "executing" else None
            ),
            "transition_log": list(self._transition_log),
        }

    # ── py_trees lifecycle ─────────────────────────────────────────────────────

    def initialise(self) -> None:
        old = self._phase
        self._phase = "idle"
        self._current_task = None
        self._command_id = None
        self._exec_steps = []
        self._step_index = 0
        if old != "idle":
            self._record_transition(old, "idle", "initialise")

    def update(self) -> py_trees.common.Status:
        if self._phase == "idle":
            return self._handle_idle()
        if self._phase == "executing":
            return self._handle_executing()
        if self._phase == "human_fallback":
            return self._handle_human_fallback()
        return py_trees.common.Status.SUCCESS

    def terminate(self, new_status: py_trees.common.Status) -> None:
        """Cancel any in-flight command when the BT interrupts this behaviour."""
        if self._command_id and self._command_resolver:
            try:
                self._command_resolver.cancel(self._command_id)
            except Exception:
                pass

    # ── Phase handlers ─────────────────────────────────────────────────────────

    def _handle_idle(self) -> py_trees.common.Status:
        if self._is_detection_active():
            return py_trees.common.Status.RUNNING

        task = self._pick_next_task()
        if task is None:
            return py_trees.common.Status.SUCCESS  # no more tasks for this entity

        self._current_task = task
        self._update_task_status(task["id"], "executing")
        self._exec_start = time.monotonic()

        self._exec_steps = self._build_exec_steps(task)
        self._step_index = 0

        cmd_id = self._dispatch_step(task, self._exec_steps[0])
        if not cmd_id:
            self._update_task_status(task["id"], "failed", reason="dispatch_error")
            self._current_task = None
            return py_trees.common.Status.RUNNING

        self._command_id = cmd_id
        self._phase = "executing"
        self._record_transition("idle", "executing", "task_picked",
                                task_id=task.get("id"),
                                step=f"1/{len(self._exec_steps)}")
        return py_trees.common.Status.RUNNING

    def _handle_executing(self) -> py_trees.common.Status:
        if not self._current_task or not self._command_id:
            self._record_transition("executing", "idle", "no_task_or_cmd")
            self._phase = "idle"
            return py_trees.common.Status.RUNNING

        # 检查任务是否已被外部（如 entity_offline 处理器）标记为终止态
        try:
            queue: list[dict] = self._bb.task_queue
            task_in_queue = next(
                (t for t in queue if t.get("id") == self._current_task.get("id")), None
            )
            if task_in_queue and task_in_queue.get("status") in ("failed", "skipped", "completed"):
                logger.info(
                    "[EntityWorker:%s] task %s externally set to '%s', resetting to idle",
                    self.entity_id,
                    self._current_task.get("id"),
                    task_in_queue.get("status"),
                )
                if self._command_id and self._command_resolver:
                    try:
                        self._command_resolver.cancel(self._command_id)
                    except Exception:
                        pass
                self._record_transition("executing", "idle", "external_terminate",
                                        task_id=self._current_task.get("id"))
                self._current_task = None
                self._command_id = None
                self._phase = "idle"
                return py_trees.common.Status.RUNNING
        except Exception:
            pass

        if self._is_detection_active():
            self._halt_for_detection()
            return py_trees.common.Status.RUNNING

        result = self._poll_command_result()
        if result == "success":
            # Check if there are more steps to execute
            self._step_index += 1
            if self._step_index < len(self._exec_steps):
                next_intent = self._exec_steps[self._step_index]
                logger.info(
                    "[EntityWorker:%s] step %d/%d done, advancing to '%s' for task=%s",
                    self.entity_id, self._step_index, len(self._exec_steps),
                    next_intent, self._current_task.get("id"),
                )
                cmd_id = self._dispatch_step(self._current_task, next_intent)
                if not cmd_id:
                    self._update_task_status(
                        self._current_task["id"], "failed",
                        reason=f"dispatch_error_step_{self._step_index}",
                    )
                    self._record_transition("executing", "idle", "dispatch_error_step",
                                            task_id=self._current_task.get("id"),
                                            step=f"{self._step_index + 1}/{len(self._exec_steps)}")
                    self._current_task = None
                    self._command_id = None
                    self._phase = "idle"
                else:
                    self._command_id = cmd_id
                    self._record_transition("executing", "executing", "step_advance",
                                            task_id=self._current_task.get("id"),
                                            step=f"{self._step_index + 1}/{len(self._exec_steps)}")
                return py_trees.common.Status.RUNNING

            # All steps completed
            elapsed = time.monotonic() - self._exec_start
            self._update_task_status(
                self._current_task["id"], "completed",
                elapsed_ms=int(elapsed * 1000),
            )
            self._write_task_effects(self._current_task)
            self._record_transition("executing", "idle", "all_steps_completed",
                                    task_id=self._current_task.get("id"))
            self._current_task = None
            self._command_id = None
            self._phase = "idle"
            return py_trees.common.Status.RUNNING  # pick next task on next tick

        if result == "failure":
            if self._should_escalate_to_human():
                self._phase = "human_fallback"
                self._fallback_start = time.monotonic()
                self._current_task["human_intervention_count"] = \
                    self._current_task.get("human_intervention_count", 0) + 1
                self._write_intervention_reason(self._current_task, "execution_failure")
                self._dispatch_human_fallback_directive()
                self._record_transition("executing", "human_fallback", "escalate_failure",
                                        task_id=self._current_task.get("id"))
            else:
                elapsed = time.monotonic() - self._exec_start
                self._update_task_status(
                    self._current_task["id"], "failed",
                    reason="execution_failure",
                    elapsed_ms=int(elapsed * 1000),
                )
                self._record_transition("executing", "idle", "execution_failure",
                                        task_id=self._current_task.get("id"))
                self._current_task = None
                self._command_id = None
                self._phase = "idle"
            return py_trees.common.Status.RUNNING

        # Timeout: if UE never responds, treat as failure
        elapsed = time.monotonic() - self._exec_start
        if elapsed > _COMMAND_TIMEOUT_SEC:
            logger.warning(
                "[EntityWorker:%s] command timeout after %.0fs for task=%s cmd=%s",
                self.entity_id, elapsed,
                self._current_task.get("id") if self._current_task else "?",
                self._command_id,
            )
            if self._should_escalate_to_human():
                self._phase = "human_fallback"
                self._fallback_start = time.monotonic()
                self._current_task["human_intervention_count"] = \
                    self._current_task.get("human_intervention_count", 0) + 1
                self._write_intervention_reason(self._current_task, "command_timeout")
                self._dispatch_human_fallback_directive()
                self._record_transition("executing", "human_fallback", "escalate_timeout",
                                        task_id=self._current_task.get("id"))
            else:
                self._update_task_status(
                    self._current_task["id"], "failed",
                    reason="command_timeout",
                    elapsed_ms=int(elapsed * 1000),
                )
                self._record_transition("executing", "idle", "command_timeout",
                                        task_id=self._current_task.get("id"))
                self._current_task = None
                self._command_id = None
                self._phase = "idle"
            return py_trees.common.Status.RUNNING

        return py_trees.common.Status.RUNNING  # still executing

    def _handle_human_fallback(self) -> py_trees.common.Status:
        """Check if human has responded (via BB key set by ResponseResolver).

        Three outcomes:
        1. **retry** — human provided new params (e.g. zone_id) → merge + re-dispatch
        2. **skip**  — human chose to skip this task → mark skipped, pick next
        3. **timeout** — no response within deadline → mark failed, pick next
        """
        task = self._current_task
        if not task:
            self._record_transition("human_fallback", "idle", "no_task")
            self._phase = "idle"
            return py_trees.common.Status.RUNNING

        if self._is_detection_active():
            self._halt_for_detection()
            return py_trees.common.Status.RUNNING

        # Re-read the task from the blackboard in case ResponseResolver
        # wrote the human_response while we were waiting.
        human_response = self._read_task_human_response(task["id"])

        if human_response:
            decision = human_response.get("decision", "retry")

            if decision == "skip":
                fb_ms = int((time.monotonic() - self._fallback_start) * 1000)
                task["human_intervention_ms"] = task.get("human_intervention_ms", 0) + fb_ms
                task["human_response_count"] = task.get("human_response_count", 0) + 1
                self._update_task_status(
                    task["id"], "skipped",
                    reason="operator_skipped",
                )
                self._clear_task_human_response(task["id"])
                self._record_transition("human_fallback", "idle", "operator_skipped",
                                        task_id=task.get("id"))
                self._current_task = None
                self._command_id = None
                self._phase = "idle"
                logger.info(
                    "[EntityWorker:%s] task %s skipped by operator",
                    self.entity_id, task.get("id"),
                )
                return py_trees.common.Status.RUNNING

            # decision == "retry" — merge provided params and re-dispatch
            fb_ms = int((time.monotonic() - self._fallback_start) * 1000)
            task["human_intervention_ms"] = task.get("human_intervention_ms", 0) + fb_ms
            task["human_response_count"] = task.get("human_response_count", 0) + 1
            extra_params = human_response.get("params") or {}
            task["params"] = {**task.get("params", {}), **extra_params}
            self._clear_task_human_response(task["id"])
            self._update_task_status(task["id"], "executing")
            self._exec_start = time.monotonic()
            self._exec_steps = self._build_exec_steps(task)
            self._step_index = 0
            cmd_id = self._dispatch_step(task, self._exec_steps[0])
            self._command_id = cmd_id
            new_phase = "executing" if cmd_id else "idle"
            self._phase = new_phase
            self._record_transition("human_fallback", new_phase,
                                    "retry_with_params" if cmd_id else "redispatch_error",
                                    task_id=task.get("id"))
            if not cmd_id:
                self._update_task_status(task["id"], "failed", reason="redispatch_error")
                self._current_task = None
            logger.info(
                "[EntityWorker:%s] task %s retrying with human params: %s",
                self.entity_id, task.get("id"), list(extra_params.keys()),
            )
            return py_trees.common.Status.RUNNING

        # No response yet — check timeout
        elapsed = time.monotonic() - self._fallback_start
        if elapsed >= self._fallback_timeout:
            fb_ms = int(elapsed * 1000)
            task["human_intervention_ms"] = task.get("human_intervention_ms", 0) + fb_ms
            task["human_timeout_count"] = task.get("human_timeout_count", 0) + 1
            self._update_task_status(
                task["id"], "failed",
                reason="human_fallback_timeout",
            )
            self._record_transition("human_fallback", "idle", "fallback_timeout",
                                    task_id=task.get("id"))
            self._current_task = None
            self._command_id = None
            self._phase = "idle"
            return py_trees.common.Status.RUNNING

        return py_trees.common.Status.RUNNING  # waiting for human

    # ── Task queue helpers ─────────────────────────────────────────────────────

    def _pick_next_task(self) -> dict | None:
        """Find the first pending task for this entity whose dependencies are satisfied.

        Before scanning, checks the mission-level success condition on the
        Blackboard.  If met (e.g. ``bomb_detected=True``), all remaining
        pending tasks for this entity are marked *skipped* and ``None`` is
        returned so the worker can exit cleanly.
        """
        if self._is_mission_goal_met():
            self._skip_remaining_pending_tasks()
            return None

        try:
            queue: list[dict] = self._bb.task_queue
        except Exception:
            return None

        _priority_order = {"critical": 0, "urgent": 1, "normal": 2}
        eligible: list[dict] = []

        for task in queue:
            if task.get("entity") != self.entity_id:
                continue
            if task.get("status") != "pending":
                continue
            deps: list[str] = task.get("depends_on") or []
            deps_ok = all(
                (_find_task(queue, dep_id) or {}).get("status") in ("completed", "skipped", "failed")
                for dep_id in deps
            )
            if deps_ok:
                if not self._check_preconditions(task):
                    self._update_task_status(
                        task["id"], "skipped", reason="precondition_not_met",
                    )
                    logger.info(
                        "[EntityWorker:%s] skipping task %s — precondition not met",
                        self.entity_id, task.get("id"),
                    )
                    continue
                eligible.append(task)

        if not eligible:
            return None

        # Return highest-priority task; preserve queue order as tiebreaker
        eligible.sort(key=lambda t: _priority_order.get(t.get("priority", "normal"), 2))
        return eligible[0]

    # ── Generic precondition / effect evaluation ─────────────────────────────

    def _check_preconditions(self, task: dict) -> bool:
        """Evaluate declarative preconditions against Blackboard state.

        Each precondition is ``{"key": "<bb_key>", "expect": <value>}``.
        Returns True only when ALL preconditions are satisfied.
        The method is intentionally business-agnostic: it reads BB keys
        and compares values without understanding their semantics.
        """
        pcs: list[dict] = task.get("preconditions") or []
        if not pcs:
            return True
        storage = py_trees.blackboard.Blackboard.storage
        for pc in pcs:
            key = pc.get("key", "")
            expected = pc.get("expect")
            actual = storage.get(f"/{key}", storage.get(key))
            if actual is None:
                actual = False
            elif isinstance(actual, dict):
                actual = True
            if actual != expected:
                return False
        return True

    def _write_task_effects(self, task: dict) -> None:
        """Write declarative effects to Blackboard after task completion.

        Each effect is ``{"key": "<bb_key>", "value": <any>}``.
        Dict values are enriched with ``entity_id`` and ``timestamp``.
        The method is intentionally business-agnostic.
        """
        effects: list[dict] = task.get("effects") or []
        if not effects:
            return
        storage = py_trees.blackboard.Blackboard.storage
        for eff in effects:
            key = eff.get("key", "")
            value = eff.get("value")
            if isinstance(value, dict):
                value = {**value, "entity_id": self.entity_id,
                         "timestamp": time.time()}
            storage[f"/{key}"] = value
            storage[key] = value
        logger.info(
            "[EntityWorker:%s] wrote %d effects: %s",
            self.entity_id, len(effects),
            [e["key"] for e in effects],
        )

    # ── Mission-goal termination ───────────────────────────────────────────────

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

    def _is_mission_goal_met(self) -> bool:
        """Check if the mission goal has been satisfied.

        Priority order:
        1. ``mission_goal_confirmed`` (set by GoalConfirmationGate)
        2. Structured ``mission_goal`` with ``requires_confirmation=False``
        3. Fallback: no ``mission_goal`` → check well-known detection keys
        """
        try:
            storage = py_trees.blackboard.Blackboard.storage
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
                        "[EntityWorker:%s] Goal met via well-known key: %s=%s",
                        self.entity_id, key, val,
                    )
                    return True
            return False
        except Exception:
            return False

    def _is_detection_active(self) -> bool:
        """True when a critical detection is pending operator confirmation.

        Unlike ``_is_mission_goal_met()`` (which waits for ``mission_goal_confirmed``),
        this fires as soon as the raw detection key is set.  All entity workers
        freeze immediately so the robot does not run away while the operator
        reviews the detection.

        Returns False once the operator has confirmed (mission concluding) or
        rejected the detection (``bomb_detected`` reset to False by
        GoalConfirmationGate).
        """
        try:
            storage = py_trees.blackboard.Blackboard.storage

            if self._bb_get(storage, "mission_goal_confirmed"):
                return False

            goal = self._bb_get(storage, "mission_goal")
            if goal and goal.get("requires_confirmation", False):
                cond = goal.get("success_condition")
                if cond and cond.get("key"):
                    actual = self._bb_get(storage, cond["key"])
                    if actual == cond.get("expected", True):
                        return True

            if not goal:
                for key in self._WELL_KNOWN_DETECTION_KEYS:
                    if self._bb_get(storage, key):
                        return True

            return False
        except Exception:
            return False

    def _halt_for_detection(self) -> None:
        """Cancel in-flight command, send STOP to UE, revert task to pending.

        Called when a critical detection fires while the entity is executing
        or waiting for human fallback.  The entity physically stops in UE,
        and the task is reverted to "pending" so it can be re-dispatched
        after a false-positive dismissal.
        """
        task = self._current_task

        if self._command_id and self._command_resolver:
            try:
                self._command_resolver.cancel(self._command_id)
            except Exception:
                pass

        if self._zenoh:
            try:
                self._zenoh.publish_robot_command(self.entity_id, {
                    "entity_id": self.entity_id,
                    "actionType": "STOP",
                    "nodeId": "detection_halt",
                    "params": {},
                })
            except Exception:
                pass

        if task:
            self._update_task_status(task["id"], "pending")
            try:
                queue: list[dict] = self._bb.task_queue
                for t in queue:
                    if t.get("id") == task["id"]:
                        t.pop("failure_reason", None)
                        t.pop("completed_at", None)
                        break
                self._bb.task_queue = queue
            except Exception:
                pass
            logger.warning(
                "[EntityWorker:%s] HALTED for detection — task %s reverted to pending, STOP sent",
                self.entity_id, task.get("id"),
            )

        old_phase = self._phase
        self._current_task = None
        self._command_id = None
        self._exec_steps = []
        self._step_index = 0
        self._phase = "idle"
        self._record_transition(old_phase, "idle", "detection_halt",
                                task_id=task.get("id") if task else None)

    def _skip_remaining_pending_tasks(self) -> None:
        """Mark all remaining pending tasks for this entity as *skipped*."""
        try:
            queue: list[dict] = self._bb.task_queue
            skipped_ids: list[str] = []
            for task in queue:
                if task.get("entity") != self.entity_id:
                    continue
                if task.get("status") == "pending":
                    task["status"] = "skipped"
                    task["failure_reason"] = "mission_goal_met"
                    skipped_ids.append(task.get("id", "?"))
            if skipped_ids:
                self._bb.task_queue = queue
                logger.info(
                    "[EntityWorker:%s] mission goal met — skipped %d pending tasks: %s",
                    self.entity_id, len(skipped_ids), skipped_ids,
                )
        except Exception as exc:
            logger.warning("[EntityWorker:%s] skip_remaining failed: %s", self.entity_id, exc)

    def _update_task_status(
        self,
        task_id: str,
        status: str,
        reason: str | None = None,
        elapsed_ms: int | None = None,
    ) -> None:
        try:
            queue: list[dict] = self._bb.task_queue
            for task in queue:
                if task.get("id") == task_id:
                    task["status"] = status
                    if status == "executing" and not task.get("started_at"):
                        task["started_at"] = time.time()
                    if status in ("completed", "failed", "skipped"):
                        task["completed_at"] = time.time()
                    if reason:
                        task["failure_reason"] = reason
                    if elapsed_ms is not None:
                        task["elapsed_ms"] = elapsed_ms
                    break
            # Write back (py_trees BB requires explicit set for mutable objects)
            self._bb.task_queue = queue
        except Exception as exc:
            logger.warning("[EntityWorker:%s] failed to update task %s: %s", self.entity_id, task_id, exc)

    def _read_task_human_response(self, task_id: str) -> dict | None:
        """Read human_response from the canonical blackboard task_queue entry."""
        try:
            queue: list[dict] = self._bb.task_queue
            for task in queue:
                if task.get("id") == task_id:
                    return task.get("human_response")
        except Exception:
            pass
        return None

    def _clear_task_human_response(self, task_id: str) -> None:
        """Remove the consumed human_response from the task."""
        try:
            queue: list[dict] = self._bb.task_queue
            for task in queue:
                if task.get("id") == task_id:
                    task.pop("human_response", None)
                    break
            self._bb.task_queue = queue
        except Exception:
            pass

    # ── Multi-step execution plan ─────────────────────────────────────────────

    _NAV_INTENTS = frozenset({"navigation", "navigate", "move", "move_to"})
    _NAV_NOT_NEEDED = frozenset({"navigation", "navigate", "move", "move_to", "patrol",
                                  "follow_path", "follow_by_path", "halt", "wait", "stop"})

    def _build_exec_steps(self, task: dict) -> list[str]:
        """Derive the ordered list of intents to dispatch for a task.

        If the task has ``required_capabilities`` that include a navigation
        prerequisite AND the primary intent is something else (e.g. scan),
        insert a navigation step before the primary intent so the entity
        moves to the target zone first.
        """
        primary = task.get("intent", "")
        req_caps: list[str] = task.get("required_capabilities") or []

        if primary in self._NAV_NOT_NEEDED:
            return [primary]

        has_nav_prereq = any(c in self._NAV_INTENTS for c in req_caps)
        if has_nav_prereq and primary not in self._NAV_INTENTS:
            return ["navigation", primary]

        return [primary]

    # ── Command dispatch / poll ────────────────────────────────────────────────

    def _dispatch_step(self, task: dict, step_intent: str) -> str | None:
        """Dispatch a single execution step for the given task.

        For navigation steps the target is derived from the task's zone_id or
        other location-related parameters.
        """
        if not self._command_resolver:
            logger.error("[EntityWorker:%s] no command_resolver injected", self.entity_id)
            return None
        try:
            params = dict(task.get("params") or {})

            if step_intent in self._NAV_INTENTS:
                waypoints = params.get("waypoints")
                if waypoints and isinstance(waypoints, list) and len(waypoints) > 0:
                    step_intent = "follow_path"
                    params = {"waypoints": waypoints}
                    logger.info(
                        "[EntityWorker:%s] nav step upgraded to follow_path with %d waypoints",
                        self.entity_id, len(waypoints),
                    )
                else:
                    zone_id = (
                        params.get("zone_id")
                        or params.get("target_zone")
                        or params.get("target")
                        or task.get("zone_id")
                    )
                    target = self._resolve_zone_target(zone_id)
                    params = {"target": target}

            cmd = AbstractCommand(
                intent=step_intent,
                entity_id=self.entity_id,
                params=params,
                node_id=task.get("id", ""),
            )
            result = self._command_resolver.resolve(cmd)
            if result.error:
                logger.error(
                    "[EntityWorker:%s] dispatch failed task=%s step=%s error=%s",
                    self.entity_id, task.get("id"), step_intent, result.error,
                )
                return None
            logger.info(
                "[EntityWorker:%s] dispatched task=%s step=%s (%d/%d) cmd=%s",
                self.entity_id, task.get("id"), step_intent,
                self._step_index + 1, len(self._exec_steps), result.command_id,
            )
            return result.command_id
        except Exception as exc:
            logger.error("[EntityWorker:%s] dispatch exception: %s", self.entity_id, exc)
            return None

    def _resolve_zone_target(self, zone_id: str | None) -> Any:
        """Resolve zone_id → ``{x, y, z}`` coordinates for UE NAVIGATE.

        Uses ``ParamResolver.zone_registry`` (populated at generation time from
        ``scene_map.json``) as the primary data source.

        For ring/circle zones whose geometric center sits at the origin (e.g.
        ``inner_courtyard``, ``outer_ring``), navigating to ``(0,0)`` would
        place the robot at the center tower instead of *inside* the ring.  In
        that case we compute an edge point on the circle at 80% radius.
        """
        if not zone_id:
            return zone_id
        try:
            import math
            from app.execution.param_resolver import ParamResolver

            zone_data = ParamResolver.get_zone_data(zone_id)
            if not zone_data:
                storage = py_trees.blackboard.Blackboard.storage
                zone_data = storage.get(f"/zones/{zone_id}/data") or storage.get(f"zones/{zone_id}/data")

            if not zone_data or not isinstance(zone_data, dict):
                logger.warning("[EntityWorker] zone %s: no zone data in ParamResolver or BB", zone_id)
                return zone_id

            center = zone_data.get("center")
            z_range = zone_data.get("z_range", {})
            z_val = (z_range.get("min", 0) + z_range.get("max", 0)) / 2 if z_range else 0
            radius = zone_data.get("radius")
            shape = zone_data.get("shape", "")

            # 1. Non-origin center → use directly
            if center and isinstance(center, dict):
                cx = float(center.get("x", 0))
                cy = float(center.get("y", 0))
                if abs(cx) > 1.0 or abs(cy) > 1.0:
                    result = {"x": cx, "y": cy, "z": float(center.get("z", z_val))}
                    logger.info("[EntityWorker] zone %s → center %s", zone_id, result)
                    return result

            # 2. Circle zone at origin → edge point at 80% radius
            if shape == "circle" and radius and center:
                import hashlib
                seed = int(hashlib.md5(zone_id.encode()).hexdigest()[:8], 16)
                angle = (seed % 360) * math.pi / 180
                r = float(radius) * 0.8
                edge = {
                    "x": float(center.get("x", 0)) + r * math.cos(angle),
                    "y": float(center.get("y", 0)) + r * math.sin(angle),
                    "z": z_val,
                }
                logger.info("[EntityWorker] zone %s → circle edge (r=%.0f, angle=%.0f°) %s",
                            zone_id, r, math.degrees(angle), edge)
                return edge

            # 3. Polygon zone → boundary edge point
            boundary = zone_data.get("boundary_2d", [])
            if boundary:
                bp = boundary[0]
                result = {"x": bp["x"], "y": bp["y"], "z": z_val}
                logger.info("[EntityWorker] zone %s → boundary[0] %s", zone_id, result)
                return result

            # 4. patrolPoints (if any)
            patrol_pts = zone_data.get("patrolPoints", [])
            if patrol_pts:
                wp = patrol_pts[0].get("position", patrol_pts[0]) if isinstance(patrol_pts[0], dict) else patrol_pts[0]
                if isinstance(wp, dict) and "x" in wp:
                    logger.info("[EntityWorker] zone %s → patrolPoint[0] %s", zone_id, wp)
                    return wp

            # 5. Last resort — center even if at origin
            if center and isinstance(center, dict):
                result = {"x": float(center.get("x", 0)), "y": float(center.get("y", 0)), "z": z_val}
                logger.info("[EntityWorker] zone %s → center (last resort) %s", zone_id, result)
                return result

            logger.warning("[EntityWorker] zone %s: no usable geometry (keys=%s)", zone_id, list(zone_data.keys()))
        except Exception as exc:
            logger.warning("[EntityWorker] _resolve_zone_target(%s) error: %s", zone_id, exc)
        return zone_id

    def _poll_command_result(self) -> str | None:
        """Poll command resolver. Returns 'success', 'failure', or None (still running)."""
        if not self._command_resolver or not self._command_id:
            return None
        try:
            status = self._command_resolver.get_status(self._command_id)
            state = (status.state or "").lower()
            if state in _SUCCESS_STATES:
                return "success"
            if state in _FAILURE_STATES:
                return "failure"
            return None
        except Exception as exc:
            logger.warning("[EntityWorker:%s] poll error: %s", self.entity_id, exc)
            return None

    # ── Human fallback ────────────────────────────────────────────────────────

    def _should_escalate_to_human(self) -> bool:
        """Escalate to human whenever a supervisor is available.

        In an HMTA system, the human should always be notified of machine
        failures so they can decide whether to retry, re-parameterise, or
        skip.  ``bt_pattern`` is *not* consulted here — it controls the
        *execution mode* (e.g. whether the human plans upfront), not whether
        failures are reported.
        """
        task = self._current_task
        if not task:
            return False
        supervisor = task.get("human_supervisor") or self.human_supervisor_id
        return bool(supervisor)

    def _write_intervention_reason(self, task: dict, trigger: str) -> None:
        """Write a detailed human-intervention reason to the task dict.

        The profiler reads ``human_intervention_reason`` to display on the
        Gantt chart, so the operator sees *exactly* what failed (e.g.
        "navigation 执行失败 (scan 步骤 1/2)") instead of just the intent.
        """
        failing_step = (
            self._exec_steps[self._step_index]
            if self._exec_steps and self._step_index < len(self._exec_steps)
            else task.get("intent", "unknown")
        )
        primary = task.get("intent", "")
        total = len(self._exec_steps) if self._exec_steps else 1
        step_num = self._step_index + 1

        trigger_label = "超时" if trigger == "command_timeout" else "执行失败"

        if failing_step == primary or total <= 1:
            reason = f"{failing_step} {trigger_label}"
        else:
            reason = f"{failing_step} {trigger_label} ({primary} 步骤 {step_num}/{total})"

        task["human_intervention_reason"] = reason

    def _dispatch_human_fallback_directive(self) -> None:
        """Send a human directive requesting assistance for the current failed task.

        Uses CommandResolver.resolve() with the supervisor entity as target.
        The HumanGate / HumanMonitor will detect this directive and write a
        ``human_response`` key back onto the task dict when the human responds.
        """
        if not self._command_resolver or not self._current_task:
            return
        task = self._current_task
        supervisor = task.get("human_supervisor") or self.human_supervisor_id
        try:
            failing_step = (
                self._exec_steps[self._step_index]
                if self._exec_steps and self._step_index < len(self._exec_steps)
                else task.get("intent", "unknown")
            )
            cmd = AbstractCommand(
                intent="task.assist",
                entity_id=supervisor,
                params={
                    "task_id": task.get("id"),
                    "task_intent": task.get("intent"),
                    "failing_step": failing_step,
                    "step_progress": f"{self._step_index + 1}/{len(self._exec_steps)}",
                    "task_params": task.get("params", {}),
                    "required_capabilities": task.get("required_capabilities", []),
                    "entity": self.entity_id,
                    "reason": f"{failing_step}_failure",
                },
                node_id=f"fallback_{task.get('id', '')}",
            )
            result = self._command_resolver.resolve(cmd)
            if result.error:
                logger.warning(
                    "[EntityWorker:%s] human directive dispatch error: %s",
                    self.entity_id, result.error,
                )
            else:
                # Register the directive→task mapping so ResponseResolver
                # can route the human's answer back to the correct task.
                if hasattr(self._command_resolver, "set_directive_task_id"):
                    self._command_resolver.set_directive_task_id(
                        result.command_id, task.get("id", ""),
                    )
                self._command_id = result.command_id
                logger.info(
                    "[EntityWorker:%s] human fallback dispatched task=%s supervisor=%s cmd=%s",
                    self.entity_id, task.get("id"), supervisor, result.command_id,
                )
        except Exception as exc:
            logger.error("[EntityWorker:%s] human directive dispatch failed: %s", self.entity_id, exc)
