"""GoalConfirmationGate — py_trees Behaviour that confirms mission goal detection.

Runs in parallel with EntityWorkers inside par_workers. Monitors the
Blackboard for mission_goal.success_condition satisfaction, then issues
a HumanDirective for the operator to confirm or reject the detection.

State machine:
    monitoring  → success_condition met → confirming
    confirming  → operator confirms    → write mission_goal_confirmed, SUCCESS
    confirming  → operator rejects     → reset detection key, back to monitoring
    confirming  → timeout              → back to monitoring
    (no mission_goal configured)       → immediate SUCCESS
"""

from __future__ import annotations

import logging
import time

import py_trees

from app.schemas.command import AbstractCommand

logger = logging.getLogger(__name__)

# Blackboard key used by ProfilerPublisher to render human-review bars
_BB_REVIEW_SESSIONS = "human_review_sessions"

_DEFAULT_TIMEOUT_SEC = 120.0


class GoalConfirmationGate(py_trees.behaviour.Behaviour):
    """
    Parallel-safe behaviour that gates mission conclusion behind human
    confirmation of the goal detection result.

    When no ``mission_goal`` is set on the Blackboard, returns SUCCESS
    immediately (transparent pass-through).
    """

    def __init__(
        self,
        name: str,
        human_supervisor_id: str = "operator-01",
        timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
    ):
        super().__init__(name=name)
        self.human_supervisor_id = human_supervisor_id
        self.timeout_sec = timeout_sec

        self._command_resolver = None
        self._zenoh = None
        self._oracle_service = None

        self._phase: str = "monitoring"
        self._directive_id: str | None = None
        self._confirm_start: float = 0.0
        self._rejected_count: int = 0
        self._review_session_id: str | None = None

        self._bb = py_trees.blackboard.Client(name="goal_confirm")
        self._bb.register_key(key="task_queue", access=py_trees.common.Access.READ)

    def set_command_resolver(self, resolver) -> None:
        self._command_resolver = resolver

    def set_zenoh(self, zenoh_bridge) -> None:
        self._zenoh = zenoh_bridge

    def set_oracle_service(self, oracle) -> None:
        """Inject OracleService for ground-truth capability judgment."""
        self._oracle_service = oracle

    def initialise(self) -> None:
        self._phase = "monitoring"
        self._directive_id = None
        self._rejected_count = 0
        self._review_session_id = None

    @staticmethod
    def _bb_get(storage: dict, key: str):
        """Read a Blackboard value, checking both /key and key formats."""
        val = storage.get(f"/{key}")
        if val is not None:
            return val
        return storage.get(key)

    def update(self) -> py_trees.common.Status:
        storage = py_trees.blackboard.Blackboard.storage
        goal = self._bb_get(storage, "mission_goal")
        if not goal or not goal.get("success_condition"):
            if self._phase == "monitoring":
                logger.debug(
                    "[GoalConfirmationGate] No mission_goal or no success_condition — pass-through "
                    "(keys in storage: %s)",
                    [k for k in storage if "mission" in str(k).lower() or "goal" in str(k).lower()],
                )
            return py_trees.common.Status.SUCCESS

        if not goal.get("requires_confirmation", True):
            return py_trees.common.Status.SUCCESS

        if self._bb_get(storage, "mission_goal_confirmed"):
            return py_trees.common.Status.SUCCESS

        if self._phase == "monitoring":
            return self._handle_monitoring(storage, goal)
        if self._phase == "confirming":
            return self._handle_confirming(storage, goal)

        return py_trees.common.Status.RUNNING

    def _handle_monitoring(
        self, storage: dict, goal: dict,
    ) -> py_trees.common.Status:
        cond = goal["success_condition"]
        cond_key = cond.get("key", "")
        expected = cond.get("expected", True)

        actual = self._bb_get(storage, cond_key)
        if actual != expected:
            self.feedback_message = f"monitoring: {cond_key}={actual} (waiting for {expected})"
            return py_trees.common.Status.RUNNING

        logger.info(
            "[GoalConfirmationGate] Goal condition met: %s=%s — requesting confirmation",
            cond_key, actual,
        )
        self._dispatch_confirmation_directive(storage, goal)
        self._open_review_session(storage, f"目标确认: {cond_key}")
        self._phase = "confirming"
        self._confirm_start = time.monotonic()
        return py_trees.common.Status.RUNNING

    def _handle_confirming(
        self, storage: dict, goal: dict,
    ) -> py_trees.common.Status:
        if not self._directive_id or not self._command_resolver:
            self._phase = "monitoring"
            return py_trees.common.Status.RUNNING

        status = self._command_resolver.get_status(self._directive_id)

        if status.state == "completed":
            response = getattr(status, "response", None) or {}
            decision = response.get("decision", "confirm")

            if decision in ("confirm", "approved", "accept"):
                logger.info("[GoalConfirmationGate] Operator CONFIRMED goal detection")
                self._close_review_session(storage, "success")
                storage["/mission_goal_confirmed"] = True
                storage["mission_goal_confirmed"] = True
                self._oracle_confirm(storage, goal)
                return py_trees.common.Status.SUCCESS

            logger.info(
                "[GoalConfirmationGate] Operator REJECTED goal detection (decision=%s)",
                decision,
            )
            self._close_review_session(storage, "failure")
            self._handle_rejection(storage, goal)
            return py_trees.common.Status.RUNNING

        if status.state == "failed":
            logger.info("[GoalConfirmationGate] Directive failed/rejected — treating as rejection")
            self._close_review_session(storage, "failure")
            self._handle_rejection(storage, goal)
            return py_trees.common.Status.RUNNING

        elapsed = time.monotonic() - self._confirm_start
        if elapsed > self.timeout_sec:
            logger.warning(
                "[GoalConfirmationGate] Confirmation timeout after %.0fs — back to monitoring",
                elapsed,
            )
            self._close_review_session(storage, "timeout")
            self._handle_rejection(storage, goal)
            return py_trees.common.Status.RUNNING

        remaining = max(0, self.timeout_sec - elapsed)
        self.feedback_message = f"等待操作员确认检测结果 ({remaining:.0f}s)"
        return py_trees.common.Status.RUNNING

    def _handle_rejection(self, storage: dict, goal: dict) -> None:
        cond = goal.get("success_condition") or {}
        cond_key = cond.get("key", "")
        if cond_key:
            storage[f"/{cond_key}"] = False
            storage[cond_key] = False
            logger.info(
                "[GoalConfirmationGate] Reset %s=False (false positive #%d)",
                cond_key, self._rejected_count + 1,
            )

        self._rejected_count += 1
        storage["/mission_goal_rejected_count"] = self._rejected_count
        storage["mission_goal_rejected_count"] = self._rejected_count
        self._directive_id = None
        self._phase = "monitoring"

        self._oracle_reject(storage, goal, cond_key)

    # ── Oracle integration ────────────────────────────────────────────────────

    def _detecting_entity_id(self, storage: dict) -> str:
        """Resolve the entity that triggered the detection from the blackboard."""
        loc = storage.get("/bomb_location") or storage.get("bomb_location") or {}
        return loc.get("entity_id", "")

    def _oracle_reject(self, storage: dict, goal: dict, cond_key: str) -> None:
        if not self._oracle_service:
            return
        detecting = self._detecting_entity_id(storage)
        if not detecting:
            logger.debug("[GoalConfirmationGate] Cannot issue oracle judgment — no detecting entity_id")
            return
        try:
            self._oracle_service.on_goal_rejected(detecting, cond_key, goal)
        except Exception:
            logger.debug("Oracle on_goal_rejected failed", exc_info=True)

    def _oracle_confirm(self, storage: dict, goal: dict) -> None:
        if not self._oracle_service:
            return
        detecting = self._detecting_entity_id(storage)
        if not detecting:
            return
        cond_key = (goal.get("success_condition") or {}).get("key", "")
        try:
            self._oracle_service.on_goal_confirmed(detecting, cond_key)
        except Exception:
            logger.debug("Oracle on_goal_confirmed failed", exc_info=True)

    # ── BB review-session helpers (read by ProfilerPublisher) ─────────────────

    def _open_review_session(self, storage: dict, label: str) -> None:
        """Add a 'waiting' entry to the human_review_sessions BB list."""
        import time as _time
        self._review_session_id = f"goal_confirm-{self.id.hex[:8]}-{self._rejected_count}"
        sessions: list[dict] = storage.get(_BB_REVIEW_SESSIONS) or []
        sessions.append({
            "id": self._review_session_id,
            "type": "goal_confirmation",
            "entity_id": self.human_supervisor_id,
            "label": label,
            "started_at": _time.time(),
            "ended_at": None,
            "status": "waiting",
        })
        storage[_BB_REVIEW_SESSIONS] = sessions

    def _close_review_session(self, storage: dict, outcome: str) -> None:
        """Update the session status so the profiler can end the bar."""
        if not self._review_session_id:
            return
        import time as _time
        sessions: list[dict] = storage.get(_BB_REVIEW_SESSIONS) or []
        for s in sessions:
            if s.get("id") == self._review_session_id and s.get("ended_at") is None:
                s["ended_at"] = _time.time()
                s["status"] = outcome
                break
        storage[_BB_REVIEW_SESSIONS] = sessions
        self._review_session_id = None

    def _dispatch_confirmation_directive(
        self, storage: dict, goal: dict,
    ) -> None:
        if not self._command_resolver:
            logger.error("[GoalConfirmationGate] No CommandResolver injected")
            return

        cond = goal.get("success_condition") or {}
        cond_key = cond.get("key", "")
        location = storage.get("/bomb_location") or storage.get("bomb_location") or {}

        cmd = AbstractCommand(
            intent="goal_confirmation",
            entity_id=self.human_supervisor_id,
            params={
                "directive_type": "goal_confirmation",
                "condition_key": cond_key,
                "condition_value": cond.get("expected", True),
                "location": location,
                "message": f"检测到任务目标已满足 ({cond_key})，请确认识别结果",
                "decision_options": [
                    {
                        "option_id": "confirm",
                        "label": "确认",
                        "description": "确认检测结果正确，结束任务",
                    },
                    {
                        "option_id": "false_positive",
                        "label": "误报",
                        "description": "标记为误报，继续执行任务",
                    },
                ],
            },
            timeout_sec=self.timeout_sec,
            node_id=f"py-{self.id.hex[:8]}",
        )

        result = self._command_resolver.resolve(cmd)
        self._directive_id = result.command_id if not result.error else None

        if result.error:
            logger.warning(
                "[GoalConfirmationGate] Directive dispatch failed: %s", result.error,
            )
        else:
            logger.info(
                "[GoalConfirmationGate] Confirmation directive dispatched: id=%s",
                self._directive_id,
            )

    def terminate(self, new_status: py_trees.common.Status) -> None:
        storage = py_trees.blackboard.Blackboard.storage
        if new_status == py_trees.common.Status.INVALID:
            if self._directive_id and self._command_resolver:
                self._command_resolver.cancel(self._directive_id)
            self._close_review_session(storage, "cancelled")
        self._directive_id = None
