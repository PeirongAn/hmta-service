"""HumanGate — py_trees Behaviour that sends a Human Directive and awaits a response."""

from __future__ import annotations

import logging
import time

import py_trees

logger = logging.getLogger(__name__)

_REMINDER_INTERVALS = [0.5, 0.75, 0.9]  # send reminders at 50%, 75%, 90% of timeout


class HumanGate(py_trees.behaviour.Behaviour):
    """
    HumanGateNode: issues a Human Directive via CommandResolver
    and polls for the human's response each tick.

    Timeout resilience:
    - Sends reminder notifications at 50%, 75%, 90% of the timeout window.
    - On first timeout, re-dispatches the directive and resets the clock
      (up to ``max_retries`` times).
    - Only returns FAILURE after all retries are exhausted.

    Navigation fallback retry:
    - For path_planning intents, if follow_by_path failed (signalled via
      Blackboard ``follow_by_path_failed/{entity_id}``), the directive is
      re-dispatched automatically so the operator can try a different route.
    """

    def __init__(
        self,
        name: str,
        entity_id: str,
        intent: str = "command.approve",
        params: dict | None = None,
        timeout_sec: float = 120.0,
        max_retries: int = 2,
    ):
        super().__init__(name=name)
        self.entity_id = entity_id
        self.intent = intent
        self.params = params or {}
        self.timeout_sec = timeout_sec
        self.max_retries = max_retries
        self._command_resolver = None
        self._zenoh = None
        self._directive_id: str | None = None
        self._start_time: float = 0.0
        self._retry_count: int = 0
        self._reminders_sent: set[int] = set()
        self._bb = py_trees.blackboard.Client(name=f"human_gate_{name}")

    def set_command_resolver(self, resolver) -> None:
        self._command_resolver = resolver

    def set_zenoh(self, zenoh_bridge) -> None:
        self._zenoh = zenoh_bridge

    def initialise(self) -> None:
        self._retry_count = 0
        self._reminders_sent = set()
        # If this humanGate is a navigation fallback, enrich params with the
        # failure reason from the Blackboard (written by the sibling action node)
        if self.intent == "path_planning" and "reason" not in self.params:
            bb = py_trees.blackboard.Blackboard.storage
            last_fail = bb.get("last_navigation_failure_reason")
            if last_fail:
                self.params = {**self.params, "reason": last_fail}
        self._dispatch_directive()

    def _dispatch_directive(self) -> None:
        if not self._command_resolver:
            logger.error("[%s] CommandResolver not injected", self.name)
            self._directive_id = None
            return

        from app.schemas.command import AbstractCommand

        cmd = AbstractCommand(
            intent=self.intent,
            entity_id=self.entity_id,
            params=self.params,
            timeout_sec=self.timeout_sec,
            node_id=f"py-{self.id.hex[:8]}",
        )
        result = self._command_resolver.resolve(cmd)
        self._directive_id = result.command_id if not result.error else None
        self._start_time = time.monotonic()
        self._reminders_sent = set()

        if result.error:
            self.feedback_message = result.error
            logger.warning("[%s] directive dispatch failed: %s", self.name, result.error)
        else:
            attempt = f" (retry {self._retry_count})" if self._retry_count else ""
            logger.info("[%s] directive dispatched id=%s%s", self.name, self._directive_id, attempt)

    def update(self) -> py_trees.common.Status:
        if not self._directive_id:
            return py_trees.common.Status.FAILURE

        elapsed = time.monotonic() - self._start_time

        # Send reminder notifications before timeout
        for idx, ratio in enumerate(_REMINDER_INTERVALS):
            if idx not in self._reminders_sent and elapsed >= self.timeout_sec * ratio:
                self._reminders_sent.add(idx)
                remaining = max(0, self.timeout_sec - elapsed)
                self._send_reminder(remaining)

        # Timeout handling with retries
        if elapsed > self.timeout_sec:
            if self._retry_count < self.max_retries:
                self._retry_count += 1
                logger.warning(
                    "[%s] timeout after %.0fs — retrying (%d/%d)",
                    self.name, self.timeout_sec, self._retry_count, self.max_retries,
                )
                self._dispatch_directive()
                return py_trees.common.Status.RUNNING

            self.feedback_message = "human gate timeout (retries exhausted)"
            py_trees.blackboard.Blackboard.storage["approval_result"] = "timeout"
            logger.warning(
                "[%s] timeout after %.0fs — all %d retries exhausted, FAILURE",
                self.name, self.timeout_sec, self.max_retries,
            )
            return py_trees.common.Status.FAILURE

        # For path_planning gates (navigation failure fallback), the operator
        # must actually submit waypoints via the minimap — DO NOT trust
        # CommandResolver status, which can be auto-completed by UE ack messages.
        node_suffix = self.id.hex[:8]
        if self.intent in ("path_planning", "plan_path", "plan_route",
                           "remote_control", "remote_operate"):
            return self._poll_waypoint_response(node_suffix)

        # For other directive types (approve/reject), CommandResolver status is fine.
        status = self._command_resolver.get_status(self._directive_id)
        if status.state == "completed":
            logger.info("[%s] approved", self.name)
            return py_trees.common.Status.SUCCESS
        elif status.state == "failed":
            self.feedback_message = status.error or "rejected"
            logger.info("[%s] rejected/failed", self.name)
            return py_trees.common.Status.FAILURE

        remaining = max(0, self.timeout_sec - elapsed)
        self.feedback_message = f"waiting ({remaining:.0f}s left)"
        return py_trees.common.Status.RUNNING

    def _poll_waypoint_response(self, node_suffix: str) -> py_trees.common.Status:
        """Poll Blackboard param_response for operator-provided waypoints.

        For path_planning humanGates, we ONLY complete when the operator
        explicitly submits waypoints via the minimap directive card — UE
        auto-acks must not trigger completion.

        On success, writes the waypoints to ``pending_waypoints/{entity_id}``
        so the follow_by_path action node can pick them up.

        If follow_by_path subsequently failed (signalled via
        ``follow_by_path_failed/{entity_id}``), the directive is re-dispatched
        so the operator can try a different route.
        """
        bb = py_trees.blackboard.Blackboard.storage

        # Check if follow_by_path already tried and failed with our waypoints.
        # If so, clear the failure flag and re-dispatch to ask operator for new route.
        fail_key = f"follow_by_path_failed/{self.entity_id}"
        if bb.get(fail_key):
            bb.pop(fail_key, None)
            self._retry_count += 1
            if self._retry_count <= self.max_retries:
                logger.warning(
                    "[%s] follow_by_path failed — re-dispatching directive (retry %d/%d)",
                    self.name, self._retry_count, self.max_retries,
                )
                self._dispatch_directive()
                return py_trees.common.Status.RUNNING
            else:
                self.feedback_message = "reroute failed after maximum retries"
                logger.warning("[%s] all reroute retries exhausted", self.name)
                return py_trees.common.Status.FAILURE

        response_key = f"param_response/{node_suffix}"
        response = bb.get(response_key)

        if response and isinstance(response, dict):
            waypoints = (
                response.get("waypoints")
                or response.get("params", {}).get("waypoints")
            )
            if waypoints:
                # Write to a stable key so follow_by_path can find them
                bb[f"pending_waypoints/{self.entity_id}"] = waypoints
                bb.pop(response_key, None)
                logger.info(
                    "[%s] operator provided %d waypoints → pending_waypoints/%s",
                    self.name, len(waypoints), self.entity_id,
                )
                return py_trees.common.Status.SUCCESS
            # Response exists but no waypoints — treat as reject
            decision = response.get("decision", "")
            if decision in ("reject", "rejected"):
                self.feedback_message = "operator rejected the reroute request"
                logger.info("[%s] operator rejected", self.name)
                return py_trees.common.Status.FAILURE

        remaining = max(0, self.timeout_sec - (time.monotonic() - self._start_time))
        self.feedback_message = f"等待操作员在地图上规划绕行路线 ({remaining:.0f}s)"
        return py_trees.common.Status.RUNNING

    def _send_reminder(self, remaining_sec: float) -> None:
        """Publish a reminder notification via Zenoh so operators see the urgency."""
        logger.info(
            "[%s] reminder: %.0fs remaining for %s",
            self.name, remaining_sec, self.entity_id,
        )
        if self._zenoh:
            try:
                self._zenoh.publish_human_directive(self.entity_id, {
                    "directive_type": "reminder",
                    "directive_id": self._directive_id,
                    "entity_id": self.entity_id,
                    "message": f"操作超时提醒：{self.name} 还剩 {remaining_sec:.0f} 秒",
                    "remaining_sec": remaining_sec,
                    "urgency": "high" if remaining_sec < 30 else "medium",
                })
            except Exception:
                logger.exception("[%s] failed to send reminder", self.name)

    def terminate(self, new_status: py_trees.common.Status) -> None:
        if new_status == py_trees.common.Status.INVALID and self._directive_id and self._command_resolver:
            self._command_resolver.cancel(self._directive_id)
        self._directive_id = None
