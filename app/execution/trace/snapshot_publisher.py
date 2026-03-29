"""Snapshot Publisher — converts py_trees SnapshotVisitor output to Zenoh tick messages."""

from __future__ import annotations

import logging
import time

import py_trees

logger = logging.getLogger(__name__)


class SnapshotPublisher:
    """
    Attached to the BehaviourTree as a post-tick handler.
    Serialises the node status snapshot and publishes it to Zenoh
    so Theia frontend can highlight active nodes in real time.

    Zenoh topic: ``zho/bt/execution/tick``
    """

    def __init__(self, zenoh_bridge):
        self._zenoh = zenoh_bridge
        self._tick_count = 0
        self._visitor = py_trees.visitors.SnapshotVisitor()

    @property
    def visitor(self) -> py_trees.visitors.SnapshotVisitor:
        return self._visitor

    def publish(self, tree: py_trees.trees.BehaviourTree) -> None:
        self._tick_count += 1

        node_statuses: dict[str, dict] = {}
        for node in tree.root.iterate():
            try:
                status_val = node.status.value
            except AttributeError:
                status_val = "INVALID"
            node_statuses[node.name] = {
                "status": status_val,
                "feedback": node.feedback_message or "",
                "is_active": status_val == "RUNNING",
            }

        try:
            root_status = tree.root.status.value
        except AttributeError:
            root_status = "INVALID"

        payload = {
            "tick": self._tick_count,
            "timestamp": time.time(),
            "root_status": root_status,
            "nodes": node_statuses,
        }

        try:
            self._zenoh.publish_tick_snapshot(payload)
            if self._tick_count <= 3 or self._tick_count % 50 == 0:
                logger.info("Tick #%d root=%s nodes=%s", self._tick_count, root_status, list(node_statuses.keys()))
        except Exception:
            logger.exception("Failed to publish tick snapshot (tick=%d)", self._tick_count)
