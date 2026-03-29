"""Device Adapter — handles Zenoh device callbacks, drives FSM + Blackboard."""

from __future__ import annotations

import json
import logging

import py_trees

logger = logging.getLogger(__name__)

_COMPLETION_EVENTS = {"waypoint_reached", "scan_complete_clear", "disarm_complete"}
_FAILURE_EVENTS = {"navigation_failed", "comm_lost", "stuck"}


class DeviceAdapter:
    def __init__(self, zenoh_bridge, fsm_manager, command_resolver):
        self._zenoh = zenoh_bridge
        self._fsm = fsm_manager
        self._cr = command_resolver

    def start(self) -> None:
        self._zenoh.subscribe_device_callbacks(self._handle_callback)
        logger.info("DeviceAdapter subscribed to device callbacks")

    def _handle_callback(self, data: dict) -> None:
        entity_id = data.get("entity_id", "")
        event = data.get("event", "")
        command_id = data.get("command_id")

        logger.debug("Device callback: entity=%s event=%s", entity_id, event)

        # Trigger FSM transition
        self._fsm.trigger(entity_id, event)

        # Update Blackboard
        bb = py_trees.blackboard.Client(name="device_adapter")
        self._update_blackboard(bb, entity_id, event, data)

        # Update command status
        if command_id:
            if event in _COMPLETION_EVENTS:
                self._cr.update_status(command_id, "completed")
            elif event in _FAILURE_EVENTS:
                self._cr.update_status(command_id, "failed", error=event)

    @staticmethod
    def _update_blackboard(bb: py_trees.blackboard.Client, entity_id: str, event: str, data: dict) -> None:
        try:
            if event == "waypoint_reached":
                bb.set(f"entities/{entity_id}/current_zone", data.get("zone"))
            elif event == "scan_complete_clear":
                zone = data.get("zone", "")
                bb.set(f"zones/{zone}/explored", True)
                bb.set(f"zones/{zone}/cleared", True)
            elif event == "explosive_detected":
                bb.set(f"entities/{entity_id}/detection", data)
            elif event == "navigation_failed":
                bb.set(f"entities/{entity_id}/alerts", [{"type": "nav_failed", **data}])
            elif event == "comm_lost":
                bb.set(f"entities/{entity_id}/comm_status", "offline")
            elif event == "stuck":
                bb.set(f"entities/{entity_id}/alerts", [{"type": "stuck", **data}])
            elif event == "battery_low":
                bb.set(f"entities/{entity_id}/alerts", [{"type": "battery_low", **data}])
        except Exception:
            logger.exception("Blackboard update error for entity=%s event=%s", entity_id, event)
