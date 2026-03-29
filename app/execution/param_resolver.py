"""ParamResolver — resolves action parameters from multiple sources.

For each parameter defined in a skill's ``input_schema``, the resolver
walks the ``source`` priority chain (e.g. ``"runtime|config|human"``)
and attempts to fill the value from the first available source.

If a required parameter cannot be resolved from any non-human source
and its chain includes ``"human"``, it is marked as ``pending_human``
so the caller can pause execution and request operator input.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.capability.ontology import get_input_schema

logger = logging.getLogger(__name__)


@dataclass
class ParamResolution:
    """Result of parameter resolution for a single action invocation."""

    resolved: dict[str, Any] = field(default_factory=dict)
    pending_human: dict[str, dict[str, Any]] = field(default_factory=dict)
    all_resolved: bool = False
    error: str | None = None


class ParamResolver:
    """Resolve action parameters using the ontology-defined source chain.

    Resolution order per param:
    1. Explicit value in raw_params (from BT JSON)
    2. Source chain walk (runtime → config → preset → llm)
    3. Geometric derivation (zone_id → center, patrol → waypoints)
    4. Human input (pending_human)
    """

    _BB_ALIASES: dict[str, list[str]] = {
        "waypoints": ["planned_waypoints"],
    }

    _zone_registry: dict[str, dict[str, Any]] = {}

    @classmethod
    def set_zone_registry(cls, zones: dict[str, dict[str, Any]]) -> None:
        """Inject zone data from environment so _derive_end can work
        even if py_trees Blackboard key registration fails."""
        cls._zone_registry = dict(zones)
        logger.info("ParamResolver zone registry loaded: %d zones", len(zones))

    @classmethod
    def get_zone_data(cls, zone_id: str) -> dict[str, Any] | None:
        return cls._zone_registry.get(zone_id)

    def resolve(
        self,
        skill_name: str,
        raw_params: dict[str, Any] | None,
        entity_state: dict[str, Any] | None = None,
        blackboard: dict[str, Any] | None = None,
    ) -> ParamResolution:
        schema = get_input_schema(skill_name)
        if not schema:
            return ParamResolution(
                resolved=dict(raw_params or {}),
                all_resolved=True,
            )

        raw = dict(raw_params or {})
        state = entity_state or {}
        bb = blackboard or {}
        resolved: dict[str, Any] = {}
        pending_human: dict[str, dict[str, Any]] = {}
        errors: list[str] = []

        for param_name, param_def in schema.items():
            required = param_def.get("required", False)
            sources = [s.strip() for s in str(param_def.get("source", "config")).split("|")]
            default_value = param_def.get("default_value")

            value = self._try_resolve(param_name, sources, raw, state, bb, default_value)

            # Reject origin (0,0,0) for spatial params — LLM sometimes copies
            # ring-zone center {"x":0,"y":0} which is inside impassable geometry
            if value is not None and param_name in ("end", "goal", "target", "destination"):
                if isinstance(value, dict) and self._is_origin(value):
                    logger.warning(
                        "Param '%s' resolved to origin (0,0) — discarding to trigger geometric derivation",
                        param_name,
                    )
                    raw.pop(param_name, None)
                    value = None

            # Geometric derivation fallback
            if value is None:
                value = self._derive_geometric(param_name, raw, bb)

            if value is None and param_name in ("end", "goal", "target", "destination"):
                logger.warning(
                    "Param '%s' still None after geometric derivation (zone_id=%s). "
                    "BB zone keys: %s",
                    param_name,
                    raw.get("zone_id"),
                    [k for k in bb if isinstance(k, str) and str(raw.get("zone_id", "???")) in k][:8],
                )

            if value is not None:
                resolved[param_name] = value
            elif required and "human" in sources:
                pending_human[param_name] = {
                    "type": param_def.get("type", "string"),
                    "input_mode": param_def.get("input_mode", "text_input"),
                    "description": param_def.get("description", ""),
                    "enum": param_def.get("enum"),
                }
            elif required:
                errors.append(f"Required param '{param_name}' unresolved (sources: {sources})")
            elif default_value is not None:
                resolved[param_name] = default_value

        for key, val in raw.items():
            if key not in resolved and key not in pending_human:
                resolved[key] = val

        error_msg = "; ".join(errors) if errors else None
        return ParamResolution(
            resolved=resolved,
            pending_human=pending_human,
            all_resolved=(not pending_human and not errors),
            error=error_msg,
        )

    # ── Source chain resolution ────────────────────────────────────────────────

    def _try_resolve(
        self,
        param_name: str,
        sources: list[str],
        raw_params: dict[str, Any],
        entity_state: dict[str, Any],
        blackboard: dict[str, Any],
        default_value: Any,
    ) -> Any:
        """Walk the source chain and return the first non-None value found."""
        if param_name in raw_params and raw_params[param_name] is not None:
            return raw_params[param_name]

        aliases = self._BB_ALIASES.get(param_name, [])

        for source in sources:
            if source == "runtime":
                val = entity_state.get(param_name) or blackboard.get(param_name)
                if val is not None:
                    return val
                for alias in aliases:
                    val = entity_state.get(alias) or blackboard.get(alias)
                    if val is not None:
                        return val
            elif source == "config":
                if default_value is not None:
                    return default_value
            elif source == "preset":
                val = (
                    blackboard.get(f"preset_{param_name}")
                    or blackboard.get(param_name)
                )
                if val is not None:
                    return val
                for alias in aliases:
                    val = blackboard.get(f"preset_{alias}") or blackboard.get(alias)
                    if val is not None:
                        return val
                val = blackboard.get(f"task_context/{param_name}")
                if val is not None:
                    return val
            elif source == "llm":
                val = raw_params.get(param_name)
                if val is not None:
                    return val

        return None

    # ── Geometric derivation ──────────────────────────────────────────────────

    def _derive_geometric(
        self,
        param_name: str,
        raw_params: dict[str, Any],
        blackboard: dict[str, Any],
    ) -> Any:
        """Derive geometric params (vec3, waypoints) from zone data on the blackboard.

        Derivation rules:
        - ``end`` → look up zone center from ``zones/{zone_id}/center``
        - ``waypoints`` → look up zone-specific or global waypoints
        - ``zone_id`` → pick first available zone from blackboard
        """
        if param_name == "end":
            return self._derive_end(raw_params, blackboard)
        if param_name == "waypoints":
            return self._derive_waypoints(raw_params, blackboard)
        if param_name == "zone_id":
            return self._derive_zone_id(raw_params, blackboard)
        return None

    @staticmethod
    def _is_origin(vec: dict[str, Any]) -> bool:
        """True if a vec3 is effectively (0,0,0) — navigating there usually destroys the entity in UE."""
        try:
            return abs(float(vec.get("x", 0))) < 1 and abs(float(vec.get("y", 0))) < 1
        except (TypeError, ValueError):
            return False

    def _derive_end(self, raw_params: dict[str, Any], bb: dict[str, Any]) -> Any:
        """Derive navigation target from zone waypoints, zone center, or global patrol points.

        Prefers zone-specific waypoints over the raw center because many zones
        (e.g. circular rings) have their geometric center inside impassable geometry.
        """
        zone_id = (
            raw_params.get("zone_id")
            or raw_params.get("target_zone")
            or raw_params.get("zone")
            or bb.get("preset_zone_id")
        )
        if zone_id:
            zone_keys = [k for k in bb if isinstance(k, str) and str(zone_id) in str(k)]
            logger.info(
                "_derive_end: zone_id=%s, BB zone keys=%s",
                zone_id, zone_keys[:8],
            )
            # 1. Zone waypoints (preferred — avoids navigating to geometric center of ring zones)
            zone_wps = bb.get(f"zones/{zone_id}/waypoints")
            if zone_wps and isinstance(zone_wps, list) and len(zone_wps) > 0:
                first = zone_wps[0]
                if isinstance(first, dict) and "x" in first:
                    logger.info("Derived 'end' from zone '%s' waypoint[0]: %s", zone_id, first)
                    return first

            # 2. Auto-generate waypoints for circle zones that lack pre-built waypoints
            zone_data = bb.get(f"zones/{zone_id}/data") or self.get_zone_data(zone_id) or {}
            if isinstance(zone_data, dict) and zone_data.get("shape") == "circle":
                zc = zone_data.get("center")
                zr = zone_data.get("radius")
                if zc and zr:
                    import math
                    import hashlib
                    z_range = zone_data.get("z_range", {})
                    z_val = (z_range.get("min", 0) + z_range.get("max", 0)) / 2 if z_range else 0
                    r = float(zr) * 0.8
                    # Disperse edge points by zone_id hash so different zones
                    # produce different angles instead of all landing on y=0
                    angle_hash = int(hashlib.md5(zone_id.encode()).hexdigest()[:8], 16)
                    angle_rad = math.radians(angle_hash % 360)
                    cx = float(zc.get("x", 0))
                    cy = float(zc.get("y", 0))
                    first_wp = {
                        "x": round(cx + r * math.cos(angle_rad), 1),
                        "y": round(cy + r * math.sin(angle_rad), 1),
                        "z": z_val,
                    }
                    if not self._is_origin(first_wp):
                        logger.info(
                            "Derived 'end' from circle zone '%s' edge (r=%.0f*0.8, angle=%d°): %s",
                            zone_id, float(zr), angle_hash % 360, first_wp,
                        )
                        return first_wp

            # 3. Zone center — but reject origin (0,0) which is inside impassable geometry
            center = bb.get(f"zones/{zone_id}/center")
            if not center and isinstance(zone_data, dict) and zone_data.get("center"):
                raw_c = zone_data["center"]
                z_range = zone_data.get("z_range", {})
                z_val = raw_c.get("z") or ((z_range.get("min", 0) + z_range.get("max", 0)) / 2 if z_range else 0)
                center = {"x": raw_c.get("x", 0), "y": raw_c.get("y", 0), "z": z_val}
            if center and isinstance(center, dict):
                if "z" not in center or center.get("z") is None:
                    z_range = zone_data.get("z_range", {}) if isinstance(zone_data, dict) else {}
                    if z_range:
                        center = {**center, "z": (z_range.get("min", 0) + z_range.get("max", 0)) / 2}
                    else:
                        center = {**center, "z": 0}
                if not self._is_origin(center):
                    logger.info("Derived 'end' from zone '%s' center: %s", zone_id, center)
                    return center
                else:
                    logger.warning(
                        "Zone '%s' center is origin (0,0) — skipping to avoid UE entity loss",
                        zone_id,
                    )

        waypoints = bb.get("preset_waypoints") or bb.get("waypoints")
        if waypoints and isinstance(waypoints, list) and len(waypoints) > 0:
            first = waypoints[0]
            if isinstance(first, dict):
                logger.info("Derived 'end' from first global patrol point: %s", first)
                return first

        return None

    def _derive_waypoints(self, raw_params: dict[str, Any], bb: dict[str, Any]) -> Any:
        """Derive waypoints from zone-specific or global patrol points."""
        zone_id = raw_params.get("zone_id") or raw_params.get("zone")
        if zone_id:
            zone_wps = bb.get(f"zones/{zone_id}/waypoints")
            if zone_wps:
                logger.info("Derived 'waypoints' from zone '%s': %d points", zone_id, len(zone_wps))
                return zone_wps

        # Global waypoints
        wps = bb.get("preset_waypoints") or bb.get("waypoints")
        if wps and isinstance(wps, list) and len(wps) > 0:
            logger.info("Derived 'waypoints' from global patrol points: %d points", len(wps))
            return wps

        return None

    def _derive_zone_id(self, raw_params: dict[str, Any], bb: dict[str, Any]) -> Any:
        """Pick a default zone_id from available zones."""
        for key in bb:
            if key.startswith("zones/") and key.endswith("/data"):
                zone_id = key.split("/")[1]
                if zone_id != "default":
                    logger.info("Derived 'zone_id' from available zone: %s", zone_id)
                    return zone_id
        return None


# ── Scan grid generation (Roomba-style cell map) ───────────────────────────────

_SCAN_CELL_SIZE: float = 200.0  # cm, matches scene coordinate units


def generate_scan_grid(
    zone_data: dict[str, Any],
    cell_size: float = _SCAN_CELL_SIZE,
) -> tuple[list[dict[str, Any]], list[dict[str, float]]]:
    """Generate a grid of scan cells and a serpentine coverage path.

    Each cell is a ``{row, col, cx, cy, cz, scanned}`` dict.
    The coverage path visits each cell center in serpentine order
    (row 0 left-to-right, row 1 right-to-left, ...).

    Returns:
        grid_cells:    list of cell dicts (scanned=False initially)
        coverage_path: list of {x, y, z} waypoints visiting each cell center
    """
    import math

    shape = zone_data.get("shape", "")
    center = zone_data.get("center", {})
    z_range = zone_data.get("z_range", {})
    z_val = float((z_range.get("min", 0) + z_range.get("max", 0)) / 2) if z_range else 0.0

    if shape == "circle" and center and zone_data.get("radius"):
        return _grid_circle(
            float(center.get("x", 0)), float(center.get("y", 0)),
            float(zone_data["radius"]), z_val, cell_size,
        )

    boundary = zone_data.get("boundary_2d")
    if boundary and len(boundary) >= 3:
        return _grid_polygon(boundary, z_val, cell_size)

    # Fallback: single cell at center
    if center:
        cx, cy = float(center.get("x", 0)), float(center.get("y", 0))
        cell = {"row": 0, "col": 0, "cx": cx, "cy": cy, "cz": z_val, "scanned": False}
        return [cell], [{"x": cx, "y": cy, "z": z_val}]

    return [], []


def _grid_circle(
    cx: float, cy: float, radius: float, z: float, cell_size: float,
) -> tuple[list[dict[str, Any]], list[dict[str, float]]]:
    """Grid for circular zone — include cells whose center is within radius."""
    half = cell_size / 2
    min_x = cx - radius
    min_y = cy - radius

    # Build all candidate cells
    cols = int((2 * radius) / cell_size) + 2
    rows = int((2 * radius) / cell_size) + 2
    cells_by_row: dict[int, list[dict[str, Any]]] = {}

    for row in range(rows):
        cell_cy = min_y + row * cell_size + half
        row_cells = []
        for col in range(cols):
            cell_cx = min_x + col * cell_size + half
            dist = ((cell_cx - cx) ** 2 + (cell_cy - cy) ** 2) ** 0.5
            if dist <= radius - half * 0.5:
                row_cells.append({
                    "row": row, "col": col,
                    "cx": round(cell_cx, 1), "cy": round(cell_cy, 1), "cz": z,
                    "scanned": False,
                })
        if row_cells:
            cells_by_row[row] = row_cells

    return _serpentine_order(cells_by_row)


def _grid_polygon(
    boundary: list[dict], z: float, cell_size: float,
) -> tuple[list[dict[str, Any]], list[dict[str, float]]]:
    """Grid for polygon zone — include cells whose center is inside the polygon."""
    xs = [p["x"] for p in boundary]
    ys = [p["y"] for p in boundary]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    half = cell_size / 2

    cols = int((max_x - min_x) / cell_size) + 2
    rows = int((max_y - min_y) / cell_size) + 2
    cells_by_row: dict[int, list[dict[str, Any]]] = {}

    for row in range(rows):
        cell_cy = min_y + row * cell_size + half
        row_cells = []
        for col in range(cols):
            cell_cx = min_x + col * cell_size + half
            if _point_in_polygon(cell_cx, cell_cy, boundary):
                row_cells.append({
                    "row": row, "col": col,
                    "cx": round(cell_cx, 1), "cy": round(cell_cy, 1), "cz": z,
                    "scanned": False,
                })
        if row_cells:
            cells_by_row[row] = row_cells

    return _serpentine_order(cells_by_row)


def _serpentine_order(
    cells_by_row: dict[int, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, float]]]:
    """Flatten cells into serpentine (boustrophedon) visit order."""
    all_cells: list[dict[str, Any]] = []
    path: list[dict[str, float]] = []
    for row_idx, row in enumerate(sorted(cells_by_row)):
        row_cells = cells_by_row[row]
        if row_idx % 2 == 1:
            row_cells = list(reversed(row_cells))
        all_cells.extend(row_cells)
        for c in row_cells:
            path.append({"x": c["cx"], "y": c["cy"], "z": c["cz"]})
    return all_cells, path


def _point_in_polygon(px: float, py: float, polygon: list[dict]) -> bool:
    """Ray-casting point-in-polygon test."""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]["x"], polygon[i]["y"]
        xj, yj = polygon[j]["x"], polygon[j]["y"]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside
