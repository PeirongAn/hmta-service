"""MissionMonitor — py_trees Behaviour that checks task queue completion.

Acts as a condition node in the generic HMTA BT:
  Returns SUCCESS  when all tasks have reached a terminal state
                   (completed / failed / skipped) — mission done.
  Returns FAILURE  while any task is still pending / executing
                   — mission not yet complete.

Uses a blackboard counter ``mission_report_count`` so the mission-done
sequence only fires once.  After the first report cycle the engine's
``_post_tick`` detects the counter and stops the tree cleanly.
"""

from __future__ import annotations

import logging

import py_trees

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({"completed", "failed", "skipped"})


class MissionMonitor(py_trees.behaviour.Behaviour):
    """
    Condition: all tasks in task_queue are terminal?

    Returns SUCCESS  → mission complete (all tasks resolved), first time only
    Returns FAILURE  → mission ongoing, or already reported
    """

    def __init__(self, name: str = "MissionMonitor"):
        super().__init__(name=name)
        self._bb = py_trees.blackboard.Client(name="mission_monitor")
        self._bb.register_key(key="task_queue", access=py_trees.common.Access.WRITE)
        self._bb.register_key(key="mission_report_count", access=py_trees.common.Access.WRITE)

    # ── Dependency injection stubs (for uniform injection in engine) ───────────

    def set_command_resolver(self, resolver) -> None:
        pass

    def set_zenoh(self, zenoh_bridge) -> None:
        pass

    # ── py_trees lifecycle ─────────────────────────────────────────────────────

    def update(self) -> py_trees.common.Status:
        try:
            queue: list[dict] = self._bb.task_queue
        except Exception:
            return py_trees.common.Status.FAILURE

        if not queue:
            return py_trees.common.Status.SUCCESS

        all_done = all(t.get("status") in _TERMINAL_STATUSES for t in queue)
        if not all_done:
            executing = sum(1 for t in queue if t.get("status") == "executing")
            pending = sum(1 for t in queue if t.get("status") == "pending")
            self.feedback_message = f"{executing} executing, {pending} pending"
            return py_trees.common.Status.FAILURE

        # All tasks terminal — check if we already reported
        try:
            report_count = self._bb.mission_report_count
        except Exception:
            report_count = 0

        if report_count and report_count > 0:
            self.feedback_message = "mission already reported — awaiting engine shutdown"
            return py_trees.common.Status.FAILURE

        completed = sum(1 for t in queue if t.get("status") == "completed")
        failed = sum(1 for t in queue if t.get("status") == "failed")
        skipped = sum(1 for t in queue if t.get("status") == "skipped")
        logger.info(
            "[MissionMonitor] all tasks terminal — completed=%d failed=%d skipped=%d",
            completed, failed, skipped,
        )

        # Mark as reported so we don't fire again after the repeat loop
        self._bb.mission_report_count = (report_count or 0) + 1

        self.feedback_message = (
            f"Mission complete: {completed} completed, {failed} failed, {skipped} skipped"
        )
        return py_trees.common.Status.SUCCESS
