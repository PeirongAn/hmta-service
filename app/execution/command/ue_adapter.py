"""UE Format Adapter — converts internal RobotCommand to Unreal Engine's expected payload.

UE subscribes to ``zho/command/{entity_id}`` and expects a flat structure with
``actionType``, ``nodeId``, and type-specific top-level fields.  This module
bridges the gap between HMTA's internal ``RobotCommand`` schema and UE's
protocol.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# HMTA command_type → UE actionType mapping
_ACTION_TYPE_MAP: dict[str, str] = {
    "FOLLOW_BY_PATH": "FOLLOW_PATH",
    "FOLLOW_PATH": "FOLLOW_PATH",
    "NAVIGATE": "NAVIGATE",
    "NAVIGATION": "NAVIGATE",
    "SCAN": "SCAN",
    "DETECT": "DETECT",
    "PATROL": "PATROL",
    "HALT": "WAIT",
    "WAIT": "WAIT",
    "DISARM": "DISARM",
    "MARK_TARGET": "MARK_TARGET",
}


def to_ue_payload(robot_cmd: dict) -> dict[str, Any]:
    """Convert a ``RobotCommand.model_dump()`` dict to UE's expected format.

    The output is a flat JSON object with ``entity_id``, ``actionType``,
    ``nodeId``, plus action-specific fields at root level.
    """
    cmd_type: str = robot_cmd.get("command_type", "")
    action_type = _ACTION_TYPE_MAP.get(cmd_type, cmd_type)
    params = robot_cmd.get("execution_params", {})

    payload: dict[str, Any] = {
        "entity_id": robot_cmd.get("entity_id", ""),
        "actionType": action_type,
        "nodeId": robot_cmd.get("node_id", ""),
    }

    handler = _FORMATTERS.get(action_type, _format_generic)
    handler(payload, params)

    return payload


# ── Per-action formatters ──────────────────────────────────────────────────────

def _format_follow_path(payload: dict, params: dict) -> None:
    payload["waypoints"] = params.get("waypoints", [])
    payload["params"] = {
        "speed": str(params.get("speed", "200.0")),
        "tolerance": str(params.get("tolerance", "50.0")),
    }
    if params.get("loop"):
        payload["params"]["loop"] = "true"


def _is_origin_or_empty(vec: Any) -> bool:
    """Return True if a vec3-like value is (0,0,0), empty, or missing real coordinates.

    Zone-based targets like ``{"zone": "inner_courtyard"}`` are considered
    valid (not empty) because UE resolves the zone name to coordinates.
    """
    if not vec or not isinstance(vec, dict):
        return True
    if "zone" in vec:
        return False
    try:
        x, y, z = float(vec.get("x", 0)), float(vec.get("y", 0)), float(vec.get("z", 0))
    except (TypeError, ValueError):
        return True
    return abs(x) < 1.0 and abs(y) < 1.0 and abs(z) < 1.0


def _try_parse_vec3_string(s: str) -> dict[str, float] | None:
    """Try to parse '100,200,300' or '100 200 300' into {x, y, z}."""
    import re
    parts = re.split(r'[,\s]+', s.strip())
    nums = []
    for p in parts:
        try:
            nums.append(float(p))
        except ValueError:
            return None
    if len(nums) >= 2:
        return {"x": nums[0], "y": nums[1], "z": nums[2] if len(nums) > 2 else 0.0}
    return None


def _format_navigate(payload: dict, params: dict) -> None:
    target = params.get("target")
    if isinstance(target, dict) and ("x" in target or "X" in target):
        payload["end"] = target
    elif isinstance(target, str):
        parsed = _try_parse_vec3_string(target)
        payload["end"] = parsed if parsed else {"zone": target}
    elif isinstance(target, list) and len(target) >= 2:
        payload["end"] = {"x": float(target[0]), "y": float(target[1]), "z": float(target[2]) if len(target) > 2 else 0.0}
    else:
        raw_end = params.get("end", params.get("target_position", {}))
        if isinstance(raw_end, str):
            parsed_end = _try_parse_vec3_string(raw_end)
            payload["end"] = parsed_end if parsed_end else raw_end
        else:
            payload["end"] = raw_end

    if _is_origin_or_empty(payload.get("end")):
        logger.warning(
            "NAVIGATE target is origin/empty for %s — UE may destroy the entity. "
            "Check ParamResolver / bt_builder output.",
            payload.get("entity_id"),
        )
        payload["_invalid_target"] = True

    start = params.get("start")
    if isinstance(start, dict) and ("x" in start or "X" in start):
        payload["start"] = start
    elif isinstance(start, list) and len(start) >= 2:
        payload["start"] = {"x": float(start[0]), "y": float(start[1]), "z": float(start[2]) if len(start) > 2 else 0.0}
    else:
        payload["start"] = start or {}

    payload["params"] = {
        "speed": str(params.get("speed", 200.0)),
    }


def _format_scan(payload: dict, params: dict) -> None:
    payload["params"] = {
        "zone_id": str(params.get("zone_id", "")),
        "mode": str(params.get("mode", "horizontal")),
        "duration_sec": str(params.get("duration_sec", 5)),
        "fov": str(params.get("fov", 180)),
        "pitch_fov": str(params.get("pitch_fov", 60)),
    }


def _format_patrol(payload: dict, params: dict) -> None:
    payload["waypoints"] = params.get("waypoints", [])
    payload["params"] = {
        "zone_id": str(params.get("zone_id", "default")),
        "pattern": str(params.get("pattern", "perimeter")),
        "speed": str(params.get("speed", 200)),
    }


def _format_detect(payload: dict, params: dict) -> None:
    p: dict[str, str] = {
        "duration_sec": str(params.get("duration_sec", 5)),
        "min_confidence": str(params.get("min_confidence", 0.05)),
    }
    target_classes = params.get("target_classes", "")
    if target_classes:
        p["target_classes"] = str(target_classes)
    payload["params"] = p


def _format_wait(payload: dict, params: dict) -> None:
    pass


def _format_halt(payload: dict, params: dict) -> None:
    pass


def _format_generic(payload: dict, params: dict) -> None:
    payload["params"] = {k: str(v) for k, v in params.items() if v is not None}


_FORMATTERS: dict[str, Any] = {
    "FOLLOW_PATH": _format_follow_path,
    "NAVIGATE": _format_navigate,
    "SCAN": _format_scan,
    "PATROL": _format_patrol,
    "DETECT": _format_detect,
    "WAIT": _format_wait,
    "HALT": _format_halt,
}
