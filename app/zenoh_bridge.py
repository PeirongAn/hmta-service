"""Zenoh communication layer — subscribe requests, publish results/progress."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

import zenoh

from app.config import settings

logger = logging.getLogger(__name__)


class ZenohBridge:
    """Thread-safe wrapper around a Zenoh session.

    All topic keys follow the project convention: ``zho/{domain}/{id}/{type}``.
    """

    def __init__(self, router_url: str | None = None):
        self._router_url = router_url or settings.zenoh_router
        self.session: zenoh.Session | None = None
        self._subscribers: list[zenoh.Subscriber] = []

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def open(self) -> None:
        config = zenoh.Config()
        config.insert_json5("connect/endpoints", json.dumps([self._router_url]))
        self.session = zenoh.open(config)
        logger.info("Zenoh session opened → %s", self._router_url)

    def close(self) -> None:
        for sub in self._subscribers:
            sub.undeclare()
        if self.session:
            self.session.close()
        logger.info("Zenoh session closed")

    # ── BT Generation topics ─────────────────────────────────────────────────

    def subscribe_generation_requests(self, callback: Callable[[dict], None]) -> None:
        """Listen for BT generation requests from Theia backend."""
        self._subscribe("zho/bt/generate/request", callback)
        logger.info("Subscribed: zho/bt/generate/request")

    def publish_progress(self, task_id: str, step: str, status: str, **kwargs: Any) -> None:
        payload = {"task_id": task_id, "step": step, "status": status, **kwargs}
        self._put(f"zho/bt/generate/{task_id}/progress", payload)

    def publish_generation_result(self, task_id: str, result: dict) -> None:
        payload = {"task_id": task_id, "status": "completed", **result}
        self._put(f"zho/bt/generate/{task_id}/result", payload)

    def publish_generation_error(self, task_id: str, message: str) -> None:
        self._put(f"zho/bt/generate/{task_id}/error", {"task_id": task_id, "message": message})

    # ── BT Execution topics ───────────────────────────────────────────────────

    def publish_tick_snapshot(self, payload: dict) -> None:
        self._put("zho/bt/execution/tick", payload)

    def publish_phase_completed(self, payload: dict) -> None:
        """Notify IDE that a phase's BT has finished execution."""
        self._put("zho/bt/execution/phase_completed", payload)

    def publish_scan_grid(self, payload: dict) -> None:
        """Publish scan grid state for minimap cell-by-cell visualization.

        Payload contains entity_id, zone_id, cells list, progress, and delta flag.
        """
        self._put("zho/bt/execution/scan_grid", payload)

    def publish_task_queue(self, payload: dict) -> None:
        """Publish task queue state snapshot for live UI visualization."""
        self._put("zho/bt/task_queue", payload)

    def publish_zone_coverage(self, payload: dict) -> None:
        """Publish zone coverage status update to IDE.

        Payload schema::

            {
                "zones": {
                    "<zone_id>": {"status": "<status>", "entity_id": "<id>", "timestamp": <float>},
                    ...
                },
                "summary": {"total": N, "checked": K, "unchecked": M}
            }
        """
        self._put("zho/bt/execution/zone_coverage", payload)

    def publish_execution_status(
        self,
        status: str,
        detail: str = "",
        failed_node: str = "",
        failure_type: str = "",
    ) -> None:
        payload: dict = {"status": status, "detail": detail}
        if failed_node:
            payload["failed_node"] = failed_node
        if failure_type:
            payload["failure_type"] = failure_type
        self._put("zho/bt/execution/status", payload)

    def subscribe_execution_commands(self, callback: Callable[[dict], None]) -> None:
        """Listen for BT execution start commands from Theia backend."""
        self._subscribe("zho/bt/execute/start", callback)
        logger.info("Subscribed: zho/bt/execute/start")

    def subscribe_execution_stop(self, callback: Callable[[dict], None]) -> None:
        """Listen for BT execution stop commands from Theia backend."""
        self._subscribe("zho/bt/execute/stop", callback)
        logger.info("Subscribed: zho/bt/execute/stop")

    # ── Command / Directive topics ────────────────────────────────────────────

    def publish_robot_command(self, entity_id: str, command: dict) -> None:
        self._put(f"zho/entity/{entity_id}/control/action", command)

    def publish_detection_config(self, entity_id: str, config: dict) -> None:
        """Push runtime detection configuration to a UE actor.

        Topic: ``actor/{entity_id}/detection/config``

        Payload::

            {
                "alert_classes": ["bomb", "gun"],
                "min_confidence": 0.3,
                "stop_on_alert": true,
                "stop_instead_of_pause": false
            }
        """
        self._put(f"actor/{entity_id}/detection/config", config)
        logger.info("Published detection config for %s: alert_classes=%s",
                     entity_id, config.get("alert_classes"))

    def publish_profiler_gantt(self, payload: dict) -> None:
        self._put("zho/bt/profiler/gantt", payload)

    def publish_profiler_bottlenecks(self, payload: dict) -> None:
        self._put("zho/bt/profiler/bottlenecks", payload)

    def publish_proficiency_proposals(self, payload: dict) -> None:
        self._put("zho/bt/profiler/capability_proposals", payload)

    def publish_proficiency_history(self, payload: list) -> None:
        """Push proficiency_log history rows to the frontend (Zenoh → WS)."""
        self._put("zho/bt/profiler/proficiency_history", payload)

    def publish_bottleneck_history(self, payload: list) -> None:
        """Push bottleneck_history rows to the frontend (Zenoh → WS)."""
        self._put("zho/bt/profiler/bottleneck_history", payload)

    def subscribe_proposal_confirmations(self, callback: Callable[[dict], None]) -> None:
        self._subscribe("zho/bt/profiler/capability_confirm", callback)
        logger.info("Subscribed: zho/bt/profiler/capability_confirm")

    # ── Oracle topics ──────────────────────────────────────────────────────

    def publish_oracle_judgments(self, payload: dict) -> None:
        """Publish oracle capability judgments for downstream consumers."""
        self._put("zho/oracle/capability_judgments", payload)

    def subscribe_ground_truth(self, callback: Callable[[dict], None]) -> None:
        """Subscribe to UE ground truth target positions."""
        self._subscribe("zho/sim/ground_truth", callback)
        logger.info("Subscribed: zho/sim/ground_truth")

    def publish_human_directive(self, entity_id: str, directive: dict) -> None:
        self._put(f"zho/directive/{entity_id}", directive)

    def subscribe_device_callbacks(self, callback: Callable[[dict], None]) -> None:
        self._subscribe("zho/device/*/callback", callback)

    def subscribe_human_responses(self, callback: Callable[[dict], None]) -> None:
        self._subscribe("zho/response/*", callback)

    def subscribe_entity_registry(self, callback: Callable[[str, dict], None]) -> None:
        """Listen for entity registration messages from UE / mock tools.

        The callback receives ``(entity_id, data)`` where *entity_id* is taken
        from ``data["entity_id"] | data["entityId"]`` and *data* is the full
        decoded JSON payload.
        """
        def _wrap(sample: zenoh.Sample) -> None:
            data = json.loads(bytes(sample.payload).decode())
            entity_id = data.get("entity_id") or data.get("entityId", "unknown")
            callback(entity_id, data)

        sub = self.session.declare_subscriber("zho/entity/registry", _wrap)
        self._subscribers.append(sub)
        logger.info("Subscribed: zho/entity/registry")

    def subscribe_entity_states(self, callback: Callable[[str, dict], None]) -> None:
        def _wrap(sample: zenoh.Sample) -> None:
            key = str(sample.key_expr)
            parts = key.split("/")
            entity_id = parts[2] if len(parts) >= 3 else "unknown"
            data = json.loads(bytes(sample.payload).decode())
            callback(entity_id, data)

        sub = self.session.declare_subscriber("zho/entity/*/state", _wrap)
        self._subscribers.append(sub)

    def subscribe_entity_offline(self, callback: Callable[[str, dict], None]) -> None:
        """Listen for entity offline notifications from Node.js NodeRegistry."""
        def _wrap(sample: zenoh.Sample) -> None:
            key = str(sample.key_expr)
            parts = key.split("/")
            entity_id = parts[2] if len(parts) >= 3 else "unknown"
            data = json.loads(bytes(sample.payload).decode())
            callback(entity_id, data)

        sub = self.session.declare_subscriber("zho/entity/*/offline", _wrap)
        self._subscribers.append(sub)

    def subscribe_entity_events(self, callback: Callable[[str, dict], None]) -> None:
        """Listen for entity events (bomb_detected, threat_detected, etc.) from UE.

        Topic: ``zho/entity/{entity_id}/event``
        """
        def _wrap(sample: zenoh.Sample) -> None:
            key = str(sample.key_expr)
            parts = key.split("/")
            entity_id = parts[2] if len(parts) >= 3 else "unknown"
            data = json.loads(bytes(sample.payload).decode())
            if not data.get("entity_id"):
                data["entity_id"] = entity_id
            callback(entity_id, data)

        sub = self.session.declare_subscriber("zho/entity/*/event", _wrap)
        self._subscribers.append(sub)
        logger.info("Subscribed: zho/entity/*/event")

    def subscribe_action_results(self, callback: Callable[[str, dict], None]) -> None:
        """Listen for explicit action completion from UE.

        Topic: ``zho/entity/{entity_id}/control/action_result``

        Expected payload::

            {
              "entity_id": "dog1",
              "nodeId":    "py-a1b2c3d4",
              "actionType": "FOLLOW_PATH",
              "result":    "SUCCESS" | "FAILURE",
              "message":   ""
            }
        """
        def _wrap(sample: zenoh.Sample) -> None:
            key = str(sample.key_expr)
            parts = key.split("/")
            entity_id = parts[2] if len(parts) >= 4 else "unknown"
            data = json.loads(bytes(sample.payload).decode())
            if not data.get("entity_id"):
                data["entity_id"] = entity_id
            callback(entity_id, data)

        sub = self.session.declare_subscriber("zho/entity/*/control/action_result", _wrap)
        self._subscribers.append(sub)
        logger.info("Subscribed: zho/entity/*/control/action_result")

    # ── BT Param request/response topics ─────────────────────────────────────

    def publish_param_request(self, data: dict) -> None:
        """Publish a parameter request to IDE when an action node needs human input."""
        self._put("zho/bt/execution/param_request", data)
        logger.info("Published param_request: node_id=%s, missing=%s",
                     data.get("node_id"), [p["name"] for p in data.get("missing_params", [])])

    def subscribe_param_responses(self, callback: Callable[[dict], None]) -> None:
        """Listen for parameter responses from IDE operators."""
        self._subscribe("zho/bt/execution/param_response", callback)
        logger.info("Subscribed: zho/bt/execution/param_response")

    # ── Capability graph topics ─────────────────────────────────────────────

    def publish_graph_snapshot(self, graph_dict: dict) -> None:
        """Publish a full hypergraph snapshot for frontend initialisation."""
        self._put("zho/capability/graph", graph_dict)

    def publish_graph_delta(self, delta: dict) -> None:
        """Publish an incremental graph change (add/update/remove nodes/edges)."""
        self._put("zho/capability/graph/delta", delta)

    def publish_allocation_feedback(self, task_id: str, feedback: dict) -> None:
        """Publish allocation feedback for experiment tracking."""
        self._put(f"zho/allocation/{task_id}/feedback", feedback)

    # ── Public helpers (used by EditHandler / InterventionHandler) ──────────

    def subscribe(self, key: str, callback: Callable[[Any], None]) -> zenoh.Subscriber:
        """Subscribe with a *raw* Zenoh sample callback (no JSON unwrap).

        Unlike the internal ``_subscribe`` which auto-decodes JSON and
        passes a ``dict``, this hands the raw ``zenoh.Sample`` to the
        caller — matching what ``EditHandler`` / ``InterventionHandler``
        expect.
        """
        if not self.session:
            raise RuntimeError("ZenohBridge not opened")
        sub = self.session.declare_subscriber(key, callback)
        self._subscribers.append(sub)
        return sub

    def publish(self, key: str, payload: dict) -> None:
        """Publish a JSON-serialised dict to *key*."""
        self._put(key, payload)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _put(self, key: str, payload: dict) -> None:
        if not self.session:
            raise RuntimeError("ZenohBridge not opened")
        self.session.put(key, json.dumps(payload))

    def _subscribe(self, key: str, callback: Callable[[dict], None]) -> None:
        if not self.session:
            raise RuntimeError("ZenohBridge not opened")

        def _wrap(sample: zenoh.Sample) -> None:
            try:
                data = json.loads(bytes(sample.payload).decode())
                callback(data)
            except Exception:
                logger.exception("Error in Zenoh callback for key %s", key)

        sub = self.session.declare_subscriber(key, _wrap)
        self._subscribers.append(sub)
