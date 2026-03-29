"""HumanMonitor — py_trees Behaviour that processes pending human directives.

Watches the ``task_queue`` for tasks assigned to the human entity and
routes human-initiated actions (confirmations, parameter provision,
manual overrides) back into the EntityWorker via the CommandResolver's
directive mechanism.

Returns RUNNING while there are active human tasks / open directives.
Returns SUCCESS when all human tasks are resolved (or there are none).
Never returns FAILURE.
"""

from __future__ import annotations

import logging
import time

import py_trees

logger = logging.getLogger(__name__)

_IDLE_POLL_INTERVAL_SEC = 1.0


class HumanMonitor(py_trees.behaviour.Behaviour):
    """
    Operator / human-entity monitor.

    Responsibilities:
    - Watch for task_queue items assigned to this human entity
    - Poll for incoming human directive responses (from IDE / XR device)
    - Acknowledge and forward responses to the CommandResolver
    - Update task_queue items owned by the human (status → executing / completed)

    Returns SUCCESS once all human-assigned tasks reach a terminal status
    (completed / failed / skipped).  Returns RUNNING while any are pending
    or executing.  Never returns FAILURE.
    """

    def __init__(
        self,
        name: str,
        entity_id: str,
        poll_interval_sec: float = _IDLE_POLL_INTERVAL_SEC,
    ):
        super().__init__(name=name)
        self.entity_id = entity_id
        self._poll_interval = poll_interval_sec
        self._last_poll: float = 0.0
        self._command_resolver = None
        self._zenoh = None

        self._bb = py_trees.blackboard.Client(name=f"hm_{entity_id}")
        self._bb.register_key(key="task_queue", access=py_trees.common.Access.WRITE)

    # ── Dependency injection ───────────────────────────────────────────────────

    def set_command_resolver(self, resolver) -> None:
        self._command_resolver = resolver

    def set_zenoh(self, zenoh_bridge) -> None:
        self._zenoh = zenoh_bridge

    # ── py_trees lifecycle ─────────────────────────────────────────────────────

    def initialise(self) -> None:
        self._last_poll = 0.0

    def update(self) -> py_trees.common.Status:
        now = time.monotonic()
        if now - self._last_poll < self._poll_interval:
            # Return immediately without touching the BB; check my own tasks
            return self._compute_status()

        self._last_poll = now
        self._process_pending_directives()
        return self._compute_status()

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _compute_status(self) -> py_trees.common.Status:
        """Return SUCCESS if no human tasks are pending/executing; else RUNNING."""
        try:
            queue: list[dict] = self._bb.task_queue
        except Exception:
            return py_trees.common.Status.SUCCESS  # no queue → nothing to do

        for task in queue:
            if task.get("entity") != self.entity_id:
                continue
            if task.get("status") in ("pending", "executing", "assigned"):
                return py_trees.common.Status.RUNNING

        return py_trees.common.Status.SUCCESS

    def _process_pending_directives(self) -> None:
        """Poll the command resolver for any pending human directive responses."""
        if not self._command_resolver:
            return
        try:
            # Fetch all open directive IDs for this human entity
            open_directives: list[str] = self._command_resolver.get_open_directives(
                entity_id=self.entity_id
            )
        except AttributeError:
            # CommandResolver may not implement this method yet
            return
        except Exception as exc:
            logger.warning("[HumanMonitor:%s] get_open_directives failed: %s", self.entity_id, exc)
            return

        for directive_id in open_directives:
            try:
                response = self._command_resolver.get_directive_response(directive_id)
                if response and response.get("status") in ("approved", "responded", "completed"):
                    self._apply_directive_response(directive_id, response)
            except Exception as exc:
                logger.warning(
                    "[HumanMonitor:%s] directive %s poll error: %s",
                    self.entity_id, directive_id, exc,
                )

    def _apply_directive_response(self, directive_id: str, response: dict) -> None:
        """Apply a completed directive response to the task queue."""
        task_id: str = response.get("task_id") or response.get("params", {}).get("task_id", "")
        if not task_id:
            return
        try:
            queue: list[dict] = self._bb.task_queue
            for task in queue:
                if task.get("id") == task_id:
                    # Merge any extra params provided by the human
                    extra_params = response.get("params") or {}
                    task.setdefault("params", {}).update(extra_params)
                    # Mark as acknowledged (entity worker picks it back up)
                    if task.get("status") == "executing":
                        task["human_response"] = response
                    break
            self._bb.task_queue = queue
            logger.info(
                "[HumanMonitor:%s] applied directive %s for task %s",
                self.entity_id, directive_id, task_id,
            )
        except Exception as exc:
            logger.warning(
                "[HumanMonitor:%s] failed to apply directive %s: %s",
                self.entity_id, directive_id, exc,
            )
