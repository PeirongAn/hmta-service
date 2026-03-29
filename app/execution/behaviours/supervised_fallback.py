"""SupervisedFallbackDecorator — wraps a child subtree with autonomous-first,
human-fallback-on-failure semantics.

State machine:
    AUTONOMOUS     → tick child normally
      child SUCCESS → decorator SUCCESS
      child RUNNING → decorator RUNNING
      child FAILURE → transition to HUMAN_GATE

    HUMAN_GATE     → dispatch path_planning directive, wait for operator waypoints
      waypoints received → transition to FOLLOW_PATH

    FOLLOW_PATH    → dispatch follow_by_path command with operator waypoints
      follow_by_path SUCCESS → decorator SUCCESS
      follow_by_path FAILURE → retry (back to HUMAN_GATE, up to max_retries)
      all retries exhausted  → decorator FAILURE
"""

from __future__ import annotations

import logging
import time

import py_trees

logger = logging.getLogger(__name__)

_FALLBACK_TIMEOUT_SEC = 120.0


class SupervisedFallbackDecorator(py_trees.behaviour.Behaviour):

    def __init__(
        self,
        name: str,
        child: py_trees.behaviour.Behaviour,
        human_entity_id: str,
        max_retries: int = 3,
        timeout_sec: float = _FALLBACK_TIMEOUT_SEC,
    ):
        super().__init__(name=name)
        self._child = child
        self._human_entity_id = human_entity_id
        self._max_retries = max_retries
        self._timeout_sec = timeout_sec

        self.children = [self._child]

        self._command_resolver = None
        self._zenoh_bridge = None

        self._phase: str = "autonomous"
        self._retry_count: int = 0
        self._directive_id: str | None = None
        self._follow_cmd_id: str | None = None
        self._gate_start: float = 0.0
        self._follow_start: float = 0.0
        self._failure_reason: str = ""

    # -- Dependency injection (duck-typed, same as CommandAction / HumanGate) ---

    def set_command_resolver(self, resolver) -> None:
        self._command_resolver = resolver

    def set_zenoh(self, bridge) -> None:
        self._zenoh_bridge = bridge

    # -- py_trees lifecycle -----------------------------------------------------

    def initialise(self) -> None:
        self._phase = "autonomous"
        self._retry_count = 0
        self._directive_id = None
        self._follow_cmd_id = None
        self._failure_reason = ""

    def update(self) -> py_trees.common.Status:
        if self._phase == "autonomous":
            return self._tick_autonomous()
        if self._phase == "human_gate":
            return self._tick_human_gate()
        if self._phase == "follow_path":
            return self._tick_follow_path()
        return py_trees.common.Status.FAILURE

    def terminate(self, new_status: py_trees.common.Status) -> None:
        if new_status == py_trees.common.Status.INVALID:
            if self._follow_cmd_id and self._command_resolver:
                self._command_resolver.cancel(self._follow_cmd_id)
            if self._directive_id and self._command_resolver:
                self._command_resolver.cancel(self._directive_id)
            self._child.stop(py_trees.common.Status.INVALID)
        self._directive_id = None
        self._follow_cmd_id = None

    # -- Phase: AUTONOMOUS ------------------------------------------------------

    def _tick_autonomous(self) -> py_trees.common.Status:
        for node in self._child.tick():
            pass  # drive the child's generator

        child_status = self._child.status

        if child_status == py_trees.common.Status.SUCCESS:
            logger.info("[%s] child succeeded autonomously", self.name)
            return py_trees.common.Status.SUCCESS

        if child_status == py_trees.common.Status.RUNNING:
            return py_trees.common.Status.RUNNING

        # FAILURE — transition to human fallback
        self._failure_reason = getattr(self._child, "feedback_message", "") or "child failed"
        logger.warning(
            "[%s] child failed (%s) — entering human fallback (retry %d/%d)",
            self.name, self._failure_reason, self._retry_count, self._max_retries,
        )
        py_trees.blackboard.Blackboard.storage["last_navigation_failure_reason"] = self._failure_reason
        self._enter_human_gate()
        return py_trees.common.Status.RUNNING

    # -- Phase: HUMAN_GATE -----------------------------------------------------

    def _enter_human_gate(self) -> None:
        self._phase = "human_gate"
        self._gate_start = time.monotonic()

        if not self._command_resolver:
            logger.error("[%s] no CommandResolver for human fallback", self.name)
            return

        from app.schemas.command import AbstractCommand

        cmd = AbstractCommand(
            intent="path_planning",
            entity_id=self._human_entity_id,
            params={
                "reason": self._failure_reason,
                "description": f"导航失败: {self._failure_reason}，请在地图上为机器人规划路径",
            },
            timeout_sec=self._timeout_sec,
            node_id=f"py-{self.id.hex[:8]}",
        )
        result = self._command_resolver.resolve(cmd)
        self._directive_id = result.command_id if not result.error else None

        if result.error:
            logger.warning("[%s] directive dispatch failed: %s", self.name, result.error)
        else:
            logger.info("[%s] humanGate directive dispatched id=%s", self.name, self._directive_id)

    def _tick_human_gate(self) -> py_trees.common.Status:
        bb = py_trees.blackboard.Blackboard.storage
        node_suffix = self.id.hex[:8]

        # Check for operator response
        response_key = f"param_response/{node_suffix}"
        response = bb.get(response_key)
        if response and isinstance(response, dict):
            waypoints = (
                response.get("waypoints")
                or response.get("params", {}).get("waypoints")
            )
            if waypoints:
                bb.pop(response_key, None)
                logger.info(
                    "[%s] operator provided %d waypoints — dispatching follow_by_path",
                    self.name, len(waypoints),
                )
                self._enter_follow_path(waypoints)
                return py_trees.common.Status.RUNNING

        # Timeout check
        elapsed = time.monotonic() - self._gate_start
        if elapsed > self._timeout_sec:
            if self._retry_count < self._max_retries:
                self._retry_count += 1
                logger.warning(
                    "[%s] humanGate timeout — re-dispatching (retry %d/%d)",
                    self.name, self._retry_count, self._max_retries,
                )
                self._enter_human_gate()
                return py_trees.common.Status.RUNNING

            self.feedback_message = "human fallback timeout (retries exhausted)"
            logger.warning("[%s] all retries exhausted → FAILURE", self.name)
            return py_trees.common.Status.FAILURE

        remaining = max(0, self._timeout_sec - elapsed)
        self.feedback_message = f"等待操作员规划路径 ({remaining:.0f}s)"
        return py_trees.common.Status.RUNNING

    # -- Phase: FOLLOW_PATH -----------------------------------------------------

    def _enter_follow_path(self, waypoints: list) -> None:
        self._phase = "follow_path"
        self._follow_start = time.time()

        if not self._command_resolver:
            logger.error("[%s] no CommandResolver for follow_by_path", self.name)
            return

        from app.schemas.command import AbstractCommand

        entity_id = self._get_robot_entity_id()
        cmd = AbstractCommand(
            intent="follow_by_path",
            entity_id=entity_id,
            params={"waypoints": waypoints},
            node_id=f"py-{self.id.hex[:8]}",
        )
        result = self._command_resolver.resolve(cmd)
        if result.error:
            logger.warning("[%s] follow_by_path dispatch failed: %s", self.name, result.error)
            self._follow_cmd_id = None
        else:
            self._follow_cmd_id = result.command_id
            logger.info("[%s] follow_by_path dispatched id=%s for %s", self.name, self._follow_cmd_id, entity_id)

    def _tick_follow_path(self) -> py_trees.common.Status:
        if not self._follow_cmd_id:
            return self._handle_follow_failure("dispatch failed")

        status = self._command_resolver.get_status(self._follow_cmd_id)

        if status.state == "completed":
            logger.info("[%s] follow_by_path completed — re-ticking child", self.name)
            self._follow_cmd_id = None
            self._child.stop(py_trees.common.Status.INVALID)
            self._phase = "autonomous"
            return py_trees.common.Status.RUNNING

        if status.state == "failed":
            return self._handle_follow_failure(status.error or "follow_by_path failed")

        elapsed = time.time() - self._follow_start
        if elapsed > 300.0:
            if self._command_resolver:
                self._command_resolver.cancel(self._follow_cmd_id)
            return self._handle_follow_failure("follow_by_path timeout (300s)")

        self.feedback_message = f"机器人正在沿操作员路径移动"
        return py_trees.common.Status.RUNNING

    def _handle_follow_failure(self, reason: str) -> py_trees.common.Status:
        self._follow_cmd_id = None
        self._retry_count += 1
        if self._retry_count <= self._max_retries:
            logger.warning(
                "[%s] follow_by_path failed (%s) — back to humanGate (retry %d/%d)",
                self.name, reason, self._retry_count, self._max_retries,
            )
            self._failure_reason = reason
            self._enter_human_gate()
            return py_trees.common.Status.RUNNING

        self.feedback_message = f"human fallback exhausted: {reason}"
        logger.warning("[%s] all retries exhausted after follow_by_path failure → FAILURE", self.name)
        return py_trees.common.Status.FAILURE

    # -- Helpers ----------------------------------------------------------------

    def _get_robot_entity_id(self) -> str:
        """Extract the robot entity_id from the child subtree."""
        def _find_entity(node):
            eid = getattr(node, "entity_id", None)
            if eid and eid != self._human_entity_id:
                return eid
            for c in getattr(node, "children", []):
                found = _find_entity(c)
                if found:
                    return found
            return None

        return _find_entity(self._child) or "unknown"
