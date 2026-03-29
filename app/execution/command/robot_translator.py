"""L1 → L2: Robot Command Translator — data-driven.

Intent→command_type mappings are loaded from the capability registry
(hypergraph) at startup, so new capabilities added to the ontology or
structured_capabilities automatically become translatable.  Fallback
hardcoded handlers remain for backward compatibility.
"""

from __future__ import annotations

import logging
from typing import Any

from app.schemas.command import (
    AbortCondition,
    AbstractCommand,
    RobotCommand,
    SuccessCriteria,
)

logger = logging.getLogger(__name__)

# Default mapping (backward compat + bootstrap)
_DEFAULT_MAPPINGS: dict[str, dict[str, Any]] = {
    "navigation":         {"command_type": "NAVIGATE", "handler": "_translate_navigate"},
    "navigation.move_to": {"command_type": "NAVIGATE", "handler": "_translate_navigate"},
    "navigate":           {"command_type": "NAVIGATE", "handler": "_translate_navigate"},
    "move":               {"command_type": "NAVIGATE", "handler": "_translate_navigate"},
    "move_to":            {"command_type": "NAVIGATE", "handler": "_translate_navigate"},
    "patrol":             {"command_type": "PATROL", "handler": "_translate_patrol"},
    "follow_by_path":     {"command_type": "FOLLOW_PATH", "handler": "_translate_follow_by_path"},
    "follow_path":        {"command_type": "FOLLOW_PATH", "handler": "_translate_follow_by_path"},
    "transport":          {"command_type": "NAVIGATE", "handler": "_translate_navigate"},
    "perception.scan_zone": {"command_type": "SCAN", "handler": "_translate_scan"},
    "scan":               {"command_type": "SCAN", "handler": "_translate_scan"},
    "detect":             {"command_type": "DETECT", "handler": "_translate_detect"},
    "manipulation.disarm": {"command_type": "DISARM", "handler": "_translate_disarm"},
    "disarm":             {"command_type": "DISARM", "handler": "_translate_disarm"},
    "manipulation.mark_target": {"command_type": "MARK_TARGET", "handler": "_translate_mark"},
    "mark":               {"command_type": "MARK_TARGET", "handler": "_translate_mark"},
    "system.halt":        {"command_type": "WAIT", "handler": "_translate_halt"},
    "halt":               {"command_type": "WAIT", "handler": "_translate_halt"},
    "stop":               {"command_type": "WAIT", "handler": "_translate_halt"},
    "wait":               {"command_type": "WAIT", "handler": "_translate_halt"},
}


class RobotTranslator:
    """Translates AbstractCommand → RobotCommand (L2).

    Mapping sources (in priority order):
    1. Entity-level ``structured_capabilities`` (command_type field)
    2. Registry-supplied dynamic mappings
    3. Built-in defaults
    """

    def __init__(self) -> None:
        self._dynamic_mappings: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # Data-driven mapping management
    # ------------------------------------------------------------------

    def load_mappings_from_graph(self, graph_dict: dict) -> None:
        """Extract intent→command_type from a serialised hypergraph."""
        for eid, edata in graph_dict.get("edges", {}).items():
            if edata.get("kind") != "has_capability":
                continue
            node_ids = edata.get("nodes", [])
            cap_ids = [
                nid for nid in node_ids
                if graph_dict.get("nodes", {}).get(nid, {}).get("kind") == "capability"
            ]
            for cid in cap_ids:
                cap_attrs = graph_dict.get("nodes", {}).get(cid, {}).get("attrs", {})
                cmd_type = cap_attrs.get("command_type")
                if cmd_type:
                    self._dynamic_mappings[cid] = {"command_type": cmd_type}

    def load_mappings_from_command_mappings(self, command_mappings: dict) -> None:
        """Load from a pre-compiled command_mappings.json (Build artifact)."""
        for entity_id, caps in command_mappings.items():
            for cap_name, cmd_type in caps.items():
                self._dynamic_mappings[cap_name] = {"command_type": cmd_type}

    def register_mapping(self, intent: str, command_type: str, **extra: Any) -> None:
        self._dynamic_mappings[intent] = {"command_type": command_type, **extra}

    # ------------------------------------------------------------------
    # Translation
    # ------------------------------------------------------------------

    def translate(self, command: AbstractCommand, entity: dict) -> RobotCommand:
        extensions = entity.get("extensions", {})
        intent = command.intent

        mapping = (
            self._dynamic_mappings.get(intent)
            or _DEFAULT_MAPPINGS.get(intent)
        )

        if mapping and "handler" in mapping:
            handler_name = mapping["handler"]
            handler = getattr(self, handler_name, None)
            if handler:
                result = handler(command, extensions)
                result.node_id = command.node_id
                return result

        if mapping:
            result = self._translate_from_mapping(command, extensions, mapping)
            result.node_id = command.node_id
            return result

        result = self._translate_generic(command, extensions)
        result.node_id = command.node_id
        return result

    # ------------------------------------------------------------------
    # Data-driven generic translation from mapping
    # ------------------------------------------------------------------

    def _translate_from_mapping(
        self,
        cmd: AbstractCommand,
        ext: dict,
        mapping: dict[str, Any],
    ) -> RobotCommand:
        cmd_type = mapping.get("command_type", cmd.intent.upper().replace(".", "_"))
        return RobotCommand(
            command_id=cmd.command_id,
            entity_id=cmd.entity_id,
            command_type=cmd_type,
            execution_params=cmd.params,
            success_criteria=SuccessCriteria(timeout_sec=cmd.timeout_sec or 60.0),
            abort_conditions=self._build_abort_conditions(ext),
        )

    # ------------------------------------------------------------------
    # Built-in intent handlers (backward compat)
    # ------------------------------------------------------------------

    def _translate_navigate(self, cmd: AbstractCommand, ext: dict) -> RobotCommand:
        locomotion = ext.get("locomotion", {})
        p = cmd.params

        target = (
            p.get("target_position")
            or p.get("target_zone")
            or p.get("target")
            or p.get("destination")
            or p.get("end")
            or p.get("goal")
            or p.get("zone")
        )
        start = p.get("start") or p.get("start_position") or p.get("current_position")

        return RobotCommand(
            command_id=cmd.command_id,
            entity_id=cmd.entity_id,
            command_type="NAVIGATE",
            execution_params={
                "target": target,
                "start": start,
                "speed": p.get("speed") or locomotion.get("max_speed", 200.0),
                "formation": p.get("formation"),
                "obstacle_avoidance": True,
            },
            success_criteria=SuccessCriteria(
                position_tolerance=50.0,
                timeout_sec=cmd.timeout_sec or 120.0,
            ),
            abort_conditions=self._build_abort_conditions(ext),
            fallback_behavior="return_to_last_waypoint",
        )

    def _translate_scan(self, cmd: AbstractCommand, ext: dict) -> RobotCommand:
        sensors = ext.get("sensors", {})
        return RobotCommand(
            command_id=cmd.command_id,
            entity_id=cmd.entity_id,
            command_type="SCAN",
            execution_params={
                "zone_id": cmd.params.get("zone_id"),
                "mode": cmd.params.get("mode", "full"),
                "coverage_pattern": cmd.params.get("coverage_pattern", "grid"),
                "sensor_type": sensors.get("primary", "lidar"),
            },
            success_criteria=SuccessCriteria(timeout_sec=cmd.timeout_sec or 120.0),
            abort_conditions=self._build_abort_conditions(ext),
        )

    def _translate_disarm(self, cmd: AbstractCommand, ext: dict) -> RobotCommand:
        return RobotCommand(
            command_id=cmd.command_id,
            entity_id=cmd.entity_id,
            command_type="DISARM",
            execution_params={
                "target_id": cmd.params.get("target_id"),
                "approach_direction": cmd.params.get("approach_direction", "front"),
            },
            success_criteria=SuccessCriteria(timeout_sec=cmd.timeout_sec or 300.0),
            abort_conditions=self._build_abort_conditions(ext),
            fallback_behavior="retreat_and_report",
        )

    def _translate_mark(self, cmd: AbstractCommand, ext: dict) -> RobotCommand:
        return RobotCommand(
            command_id=cmd.command_id,
            entity_id=cmd.entity_id,
            command_type="MARK_TARGET",
            execution_params={"target_id": cmd.params.get("target_id")},
            success_criteria=SuccessCriteria(timeout_sec=30.0),
            abort_conditions=[],
        )

    def _translate_patrol(self, cmd: AbstractCommand, ext: dict) -> RobotCommand:
        return RobotCommand(
            command_id=cmd.command_id,
            entity_id=cmd.entity_id,
            command_type="PATROL",
            execution_params={
                "waypoints": cmd.params.get("waypoints", []),
                "zone_id": cmd.params.get("zone_id", "default"),
                "pattern": cmd.params.get("pattern", "perimeter"),
                "speed": cmd.params.get("speed") or ext.get("locomotion", {}).get("max_speed", 200.0),
            },
            success_criteria=SuccessCriteria(timeout_sec=cmd.timeout_sec or 300.0),
            abort_conditions=self._build_abort_conditions(ext),
            fallback_behavior="standby",
        )

    def _translate_detect(self, cmd: AbstractCommand, ext: dict) -> RobotCommand:
        return RobotCommand(
            command_id=cmd.command_id,
            entity_id=cmd.entity_id,
            command_type="DETECT",
            execution_params={
                "duration_sec": cmd.params.get("duration_sec", 5),
                "target_classes": cmd.params.get("target_classes", ""),
                "min_confidence": cmd.params.get("min_confidence", 0.05),
            },
            success_criteria=SuccessCriteria(timeout_sec=cmd.timeout_sec or 60.0),
            abort_conditions=self._build_abort_conditions(ext),
        )

    def _translate_halt(self, cmd: AbstractCommand, ext: dict) -> RobotCommand:
        return RobotCommand(
            command_id=cmd.command_id,
            entity_id=cmd.entity_id,
            command_type="WAIT",
            execution_params={},
            success_criteria=SuccessCriteria(timeout_sec=5.0),
            abort_conditions=[],
        )

    def _translate_follow_by_path(self, cmd: AbstractCommand, ext: dict) -> RobotCommand:
        locomotion = ext.get("locomotion", {})
        waypoints = cmd.params.get("waypoints", [])
        return RobotCommand(
            command_id=cmd.command_id,
            entity_id=cmd.entity_id,
            command_type="FOLLOW_PATH",
            execution_params={
                "waypoints": waypoints,
                "speed": cmd.params.get("speed") or locomotion.get("max_speed", 200.0),
                "loop": cmd.params.get("loop", False),
                "tolerance": cmd.params.get("tolerance", 50.0),
            },
            success_criteria=SuccessCriteria(
                position_tolerance=cmd.params.get("tolerance", 50.0),
                timeout_sec=cmd.timeout_sec or 300.0,
            ),
            abort_conditions=self._build_abort_conditions(ext),
            fallback_behavior="standby",
        )

    def _translate_generic(self, cmd: AbstractCommand, ext: dict) -> RobotCommand:
        logger.warning("Generic translation for intent '%s'", cmd.intent)
        return RobotCommand(
            command_id=cmd.command_id,
            entity_id=cmd.entity_id,
            command_type=cmd.intent.upper().replace(".", "_"),
            execution_params=cmd.params,
            success_criteria=SuccessCriteria(timeout_sec=cmd.timeout_sec or 60.0),
            abort_conditions=self._build_abort_conditions(ext),
        )

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _build_abort_conditions(self, ext: dict) -> list[AbortCondition]:
        conditions = [
            AbortCondition(type="comm_lost", timeout_sec=10),
            AbortCondition(type="obstacle_stuck", duration_sec=15),
        ]
        if ext.get("battery"):
            conditions.append(AbortCondition(type="battery_low", threshold=0.15))
        return conditions
