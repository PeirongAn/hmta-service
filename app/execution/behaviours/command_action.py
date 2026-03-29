"""CommandAction — py_trees Behaviour that sends an AbstractCommand and polls status.

Integrates with ParamResolver to resolve parameters from multiple sources
and supports a ``waiting_params`` state that pauses execution until a
human operator supplies missing required parameters via the IDE.
"""

from __future__ import annotations

import logging
import time

import py_trees

from app.execution.param_resolver import ParamResolver

logger = logging.getLogger(__name__)

_param_resolver = ParamResolver()

_PARAM_WAIT_TIMEOUT_SEC = 120.0

_DEFAULT_EXECUTION_TIMEOUT: dict[str, float] = {
    "navigate": 120.0,
    "move": 120.0,
    "navigation": 120.0,
    "patrol": 300.0,
    "follow_by_path": 300.0,
    "scan": 30.0,
    "detect": 30.0,
    "wait": 600.0,
    "halt": 600.0,
    "disarm": 120.0,
    "mark_target": 60.0,
}
_FALLBACK_EXECUTION_TIMEOUT = 120.0

_NAVIGATION_INTENTS = frozenset({
    "navigate", "navigation", "move", "patrol", "follow_by_path",
})
_SCAN_INTENTS = frozenset({"scan", "detect"})


class CommandAction(py_trees.behaviour.Behaviour):
    """
    ActionNode: translates a BT action node into an AbstractCommand,
    dispatches it via CommandResolver, and polls the execution status each tick.

    Lifecycle:
    - initialise(): resolve params → dispatch (or enter waiting_params)
    - update():     poll status → SUCCESS / FAILURE / RUNNING
    - terminate():  cancel in-flight command if interrupted

    States:
    - "resolving":      initial param resolution
    - "waiting_params": paused, waiting for human to provide missing params
    - "executing":      command dispatched, polling for completion
    - "error":          unrecoverable error
    """

    def __init__(self, name: str, intent: str, entity_id: str, params: dict | None = None):
        super().__init__(name=name)
        self.intent = intent
        self.entity_id = entity_id
        self.params = params or {}
        self._command_resolver = None
        self._zenoh_bridge = None
        self._command_id: str | None = None
        self._state = "resolving"
        self._wait_start: float = 0.0
        self._exec_start: float = 0.0
        self._pending_human: dict = {}
        # Sweep scan state (grid-based Roomba coverage)
        self._sweep_waypoints: list[dict] = []
        self._sweep_index: int = 0
        self._sweep_grid_cells: list[dict] = []   # mutable cell state with scanned flag
        self._sweep_last_pub: float = 0.0         # debounce: max 2 Hz publish

    def set_command_resolver(self, resolver) -> None:
        self._command_resolver = resolver

    def set_zenoh_bridge(self, bridge) -> None:
        self._zenoh_bridge = bridge

    def set_zenoh(self, bridge) -> None:
        """Alias used by ExecutionEngine's dependency injection."""
        self._zenoh_bridge = bridge

    # ── py_trees lifecycle ────────────────────────────────────────────────────

    def initialise(self) -> None:
        if not self._command_resolver:
            logger.error("[%s] CommandResolver not injected", self.name)
            self._state = "error"
            return

        entity_state = self._get_entity_state()
        bb_storage = py_trees.blackboard.Blackboard.storage
        bb_dict = dict(bb_storage)

        zone_id = self.params.get("zone_id", "")
        if zone_id:
            zone_keys = [k for k in bb_dict if isinstance(k, str) and zone_id in str(k)]
            logger.info(
                "[%s] BB zone keys for '%s': %s (total BB keys: %d)",
                self.name, zone_id, zone_keys[:10], len(bb_dict),
            )

        # For follow_by_path, check if the preceding humanGate wrote waypoints
        # to pending_waypoints/{entity_id}. Inject them so resolution succeeds
        # without requiring a new param_request to the operator.
        if self.intent in ("follow_by_path",):
            pending_wps = py_trees.blackboard.Blackboard.storage.get(
                f"pending_waypoints/{self.entity_id}"
            )
            if pending_wps and "waypoints" not in self.params:
                self.params = {**self.params, "waypoints": pending_wps}
                # Consume the key so it's not reused by subsequent nodes
                del py_trees.blackboard.Blackboard.storage[
                    f"pending_waypoints/{self.entity_id}"
                ]
                logger.info(
                    "[%s] injected %d operator-provided waypoints from humanGate",
                    self.name, len(pending_wps),
                )
            # Rebuild bb_dict after potential BB mutation
            bb_dict = dict(py_trees.blackboard.Blackboard.storage)

        intent_key = self.intent.lower().rsplit(".", 1)[-1]

        # Skip navigation to an already-explored zone (avoids re-visiting on repeat loops)
        if intent_key in _NAVIGATION_INTENTS and zone_id:
            zone_cleared = (
                bb_dict.get(f"zones/{zone_id}/cleared")
                or bb_dict.get(f"/zones/{zone_id}/cleared")
            )
            if zone_cleared:
                logger.info(
                    "[%s] zone '%s' already cleared — skipping navigation",
                    self.name, zone_id,
                )
                self._state = "executing"
                self._command_id = "__skipped__"
                return

        # Sweep scan mode: for scan/detect intents, check if coverage
        # waypoints are available. If so, enter sweep mode instead of
        # the instant SCAN command.
        if intent_key in _SCAN_INTENTS and zone_id:
            # Skip if zone is already cleared (avoids re-scanning on repeat loops)
            zone_cleared = (
                bb_dict.get(f"zones/{zone_id}/cleared")
                or bb_dict.get(f"/zones/{zone_id}/cleared")
            )
            if zone_cleared:
                logger.info(
                    "[%s] zone '%s' already cleared — skipping scan",
                    self.name, zone_id,
                )
                self._state = "executing"
                self._command_id = "__skipped__"
                return

            # py_trees registers BB keys with a leading "/" prefix in storage,
            # so try both forms: "zones/..." and "/zones/..."
            scan_wps = (
                bb_dict.get(f"zones/{zone_id}/scan_waypoints")
                or bb_dict.get(f"/zones/{zone_id}/scan_waypoints")
            )
            grid_cells = (
                bb_dict.get(f"zones/{zone_id}/grid_cells")
                or bb_dict.get(f"/zones/{zone_id}/grid_cells")
            )
            if scan_wps and isinstance(scan_wps, list) and len(scan_wps) > 1:
                self._sweep_waypoints = list(scan_wps)
                self._sweep_index = 0
                # Deep-copy grid cells so we can mutate scanned flags in place
                self._sweep_grid_cells = [dict(c) for c in grid_cells] if grid_cells else []
                self._sweep_last_pub = 0.0
                self._state = "sweeping"
                self._update_zone_coverage("scanning")
                logger.info(
                    "[%s] entering sweep scan mode: %d waypoints, %d cells for zone '%s'",
                    self.name, len(self._sweep_waypoints),
                    len(self._sweep_grid_cells), zone_id,
                )
                # Publish initial full grid state (all gray) so minimap shows
                # the grid immediately before any cells are scanned.
                self._publish_scan_grid(delta_only=False)
                self._dispatch_sweep_navigate()
                return

        resolution = _param_resolver.resolve(
            skill_name=self.intent,
            raw_params=self.params,
            entity_state=entity_state,
            blackboard=bb_dict,
        )

        if resolution.error:
            self.feedback_message = resolution.error
            self._state = "error"
            logger.warning("[%s] param resolution error: %s", self.name, resolution.error)
            return

        if not resolution.all_resolved:
            self._pending_human = resolution.pending_human
            self._state = "waiting_params"
            self._wait_start = time.time()
            self.feedback_message = f"waiting for human input: {list(resolution.pending_human.keys())}"
            logger.info("[%s] entering waiting_params for: %s", self.name, list(resolution.pending_human.keys()))
            self._publish_param_request(resolution.pending_human)
            self.params.update(resolution.resolved)
            return

        self.params = resolution.resolved
        self._dispatch_command()

    def update(self) -> py_trees.common.Status:
        if self._state == "error":
            return py_trees.common.Status.FAILURE

        if self._state == "waiting_params":
            return self._poll_waiting_params()

        if self._state == "sweeping":
            return self._poll_sweep()

        if self._state == "executing":
            return self._poll_execution()

        return py_trees.common.Status.FAILURE

    def terminate(self, new_status: py_trees.common.Status) -> None:
        if (
            new_status == py_trees.common.Status.INVALID
            and self._command_id
            and self._command_resolver
        ):
            self._command_resolver.cancel(self._command_id)
            logger.debug("[%s] command cancelled (id=%s)", self.name, self._command_id)
        self._command_id = None

    # ── Internal: dispatch ────────────────────────────────────────────────────

    def _get_execution_timeout(self) -> float:
        """Determine execution timeout: BT params > ontology duration_sec > intent default."""
        explicit = self.params.get("timeout_sec") or self.params.get("execution_timeout_sec")
        if explicit is not None:
            try:
                return float(explicit)
            except (TypeError, ValueError):
                pass
        duration = self.params.get("duration_sec")
        if duration is not None:
            try:
                return float(duration) + 10.0
            except (TypeError, ValueError):
                pass
        intent_key = self.intent.lower().rsplit(".", 1)[-1]
        return _DEFAULT_EXECUTION_TIMEOUT.get(intent_key, _FALLBACK_EXECUTION_TIMEOUT)

    def _dispatch_command(self) -> None:
        from app.schemas.command import AbstractCommand

        cmd = AbstractCommand(
            intent=self.intent,
            entity_id=self.entity_id,
            params=self.params,
            node_id=f"py-{self.id.hex[:8]}",
        )
        result = self._command_resolver.resolve(cmd)
        if result.error:
            self.feedback_message = result.error
            self._state = "error"
            logger.warning("[%s] resolve error: %s", self.name, result.error)
        else:
            self._command_id = result.command_id
            self._state = "executing"
            self._exec_start = time.time()
            timeout = self._get_execution_timeout()
            logger.info("[%s] dispatched command_id=%s (exec timeout=%.0fs)", self.name, self._command_id, timeout)
            intent_key = self.intent.lower().rsplit(".", 1)[-1]
            if intent_key in _NAVIGATION_INTENTS:
                self._update_zone_coverage("en_route")
            elif intent_key in _SCAN_INTENTS:
                self._update_zone_coverage("scanning")

    # ── Internal: polling ─────────────────────────────────────────────────────

    def _poll_waiting_params(self) -> py_trees.common.Status:
        bb_storage = py_trees.blackboard.Blackboard.storage
        node_key = f"param_response/{self.id.hex[:8]}"
        response = bb_storage.get(node_key)

        if response and isinstance(response, dict):
            self.params.update(response)
            bb_storage.pop(node_key, None)
            logger.info("[%s] received human params: %s", self.name, list(response.keys()))
            self._dispatch_command()
            if self._state == "executing":
                return py_trees.common.Status.RUNNING
            return py_trees.common.Status.FAILURE

        elapsed = time.time() - self._wait_start
        if elapsed > _PARAM_WAIT_TIMEOUT_SEC:
            self.feedback_message = f"param wait timeout ({_PARAM_WAIT_TIMEOUT_SEC}s)"
            logger.warning("[%s] %s", self.name, self.feedback_message)
            return py_trees.common.Status.FAILURE

        return py_trees.common.Status.RUNNING

    def _poll_execution(self) -> py_trees.common.Status:
        if not self._command_id:
            return py_trees.common.Status.FAILURE

        # Skipped scan (zone already cleared) — succeed immediately
        if self._command_id == "__skipped__":
            return py_trees.common.Status.SUCCESS

        status = self._command_resolver.get_status(self._command_id)
        if status.state == "completed":
            logger.info("[%s] command completed (id=%s)", self.name, self._command_id)
            intent_key = self.intent.lower().rsplit(".", 1)[-1]
            if intent_key in _NAVIGATION_INTENTS:
                self._update_zone_coverage("arrived")
            elif intent_key in _SCAN_INTENTS:
                self._update_zone_coverage("scanned")
            self._write_effects()
            return py_trees.common.Status.SUCCESS
        elif status.state == "failed":
            self.feedback_message = status.error or "command failed"
            logger.warning("[%s] command failed: %s (id=%s)", self.name, self.feedback_message, self._command_id)
            intent_key = self.intent.lower().rsplit(".", 1)[-1]
            if intent_key in _NAVIGATION_INTENTS:
                py_trees.blackboard.Blackboard.storage["last_navigation_failure_reason"] = self.feedback_message
            if intent_key == "follow_by_path":
                # Publish a new param_request so the operator sees the card again
                # (the BT repeat wrapper will re-enter humanGate on next tick)
                logger.warning("[%s] follow_by_path failed for %s — BT repeat will re-enter humanGate", self.name, self.entity_id)
            return py_trees.common.Status.FAILURE

        timeout = self._get_execution_timeout()
        elapsed = time.time() - self._exec_start
        if elapsed > timeout:
            self.feedback_message = f"execution timeout ({timeout:.0f}s) — UE did not report completion"
            logger.warning("[%s] %s (id=%s, elapsed=%.1fs)", self.name, self.feedback_message, self._command_id, elapsed)
            self._command_resolver.cancel(self._command_id)
            return py_trees.common.Status.FAILURE

        return py_trees.common.Status.RUNNING

    # ── Internal: effects ─────────────────────────────────────────────────────

    def _write_effects(self) -> None:
        """Write skill effects to Blackboard on SUCCESS (Step 6)."""
        from app.capability.ontology import get_effects

        effects = get_effects(self.intent)
        for effect in effects:
            py_trees.blackboard.Blackboard.storage[effect] = {
                "value": True,
                "entity_id": self.entity_id,
                "timestamp": time.time(),
            }
        if effects:
            logger.info("[%s] wrote effects to blackboard: %s", self.name, effects)

    # ── Internal: param request ───────────────────────────────────────────────

    def _publish_param_request(self, pending: dict) -> None:
        if not self._zenoh_bridge:
            logger.debug("[%s] no ZenohBridge, cannot publish param_request", self.name)
            return
        missing_params = []
        for pname, pdef in pending.items():
            missing_params.append({
                "name": pname,
                "type": pdef.get("type", "string"),
                "input_mode": pdef.get("input_mode", "text_input"),
                "description": pdef.get("description", ""),
                "enum": pdef.get("enum"),
            })
        self._zenoh_bridge.publish_param_request({
            "node_id": f"py-{self.id.hex[:8]}",
            "tree_id": "",
            "intent": self.intent,
            "entity_id": self.entity_id,
            "missing_params": missing_params,
        })

    # ── Internal: sweep scan ─────────────────────────────────────────────────

    def _dispatch_sweep_navigate(self) -> None:
        """Dispatch a NAVIGATE command to the current sweep waypoint."""
        if self._sweep_index >= len(self._sweep_waypoints):
            return
        wp = self._sweep_waypoints[self._sweep_index]
        from app.schemas.command import AbstractCommand

        cmd = AbstractCommand(
            intent="navigation",
            entity_id=self.entity_id,
            params={"end": wp, "speed": self.params.get("speed", "120")},
            node_id=f"py-{self.id.hex[:8]}",
        )
        result = self._command_resolver.resolve(cmd)
        if result.error:
            logger.warning(
                "[%s] sweep navigate #%d failed: %s — skipping",
                self.name, self._sweep_index, result.error,
            )
            self._sweep_index += 1
            if self._sweep_index < len(self._sweep_waypoints):
                self._dispatch_sweep_navigate()
        else:
            self._command_id = result.command_id
            self._exec_start = time.time()
            progress = self._sweep_index / len(self._sweep_waypoints)
            self.feedback_message = (
                f"sweep scan {self._sweep_index + 1}/{len(self._sweep_waypoints)} "
                f"({progress:.0%})"
            )

    def _poll_sweep(self) -> py_trees.common.Status:
        """Poll the current sweep navigate command with per-tick cell marking."""
        # Per-tick: check robot position and mark any newly entered cells
        self._check_position_cells()

        if not self._command_id:
            if self._sweep_index >= len(self._sweep_waypoints):
                return self._finish_sweep()
            self._dispatch_sweep_navigate()
            return py_trees.common.Status.RUNNING

        status = self._command_resolver.get_status(self._command_id)
        if status.state == "completed":
            # Mark the target cell as scanned (backup for position-based check)
            if self._sweep_index < len(self._sweep_grid_cells):
                self._sweep_grid_cells[self._sweep_index]["scanned"] = True
            self._sweep_index += 1
            self._publish_scan_grid(delta_only=True)
            if self._sweep_index >= len(self._sweep_waypoints):
                return self._finish_sweep()
            self._command_id = None
            self._dispatch_sweep_navigate()
            return py_trees.common.Status.RUNNING

        elif status.state == "failed":
            logger.warning(
                "[%s] sweep waypoint #%d navigate failed — skipping",
                self.name, self._sweep_index,
            )
            self._sweep_index += 1
            self._command_id = None
            if self._sweep_index >= len(self._sweep_waypoints):
                return self._finish_sweep()
            self._dispatch_sweep_navigate()
            return py_trees.common.Status.RUNNING

        elapsed = time.time() - self._exec_start
        if elapsed > 30.0:
            logger.warning("[%s] sweep waypoint #%d timeout — skipping", self.name, self._sweep_index)
            self._command_resolver.cancel(self._command_id)
            self._sweep_index += 1
            self._command_id = None
            if self._sweep_index >= len(self._sweep_waypoints):
                return self._finish_sweep()
            self._dispatch_sweep_navigate()
            return py_trees.common.Status.RUNNING

        scanned = sum(1 for c in self._sweep_grid_cells if c.get("scanned"))
        total = len(self._sweep_grid_cells) or len(self._sweep_waypoints)
        progress = scanned / total if total else 0
        self.feedback_message = (
            f"sweep scan {scanned}/{total} cells ({progress:.0%})"
        )
        return py_trees.common.Status.RUNNING

    def _check_position_cells(self) -> None:
        """Check current robot pose and mark any cells the robot is inside as scanned."""
        if not self._sweep_grid_cells:
            return
        bb = py_trees.blackboard.Blackboard.storage

        # Try both key forms (with and without leading /)
        pose = (
            bb.get(f"/entities/{self.entity_id}/position")
            or bb.get(f"entities/{self.entity_id}/position")
            or bb.get(f"entity_state/{self.entity_id}", {}).get("position")
            or {}
        )
        # UE pose can be {x,y,z} directly, or nested in another dict
        rx = pose.get("x")
        ry = pose.get("y")
        if rx is None or ry is None:
            return

        try:
            rx = float(rx)
            ry = float(ry)
        except (TypeError, ValueError):
            return

        from app.execution.param_resolver import _SCAN_CELL_SIZE
        half = _SCAN_CELL_SIZE

        new_marked = 0
        for cell in self._sweep_grid_cells:
            if cell.get("scanned"):
                continue
            if abs(rx - cell["cx"]) <= half and abs(ry - cell["cy"]) <= half:
                cell["scanned"] = True
                new_marked += 1

        if new_marked > 0:
            scanned_total = sum(1 for c in self._sweep_grid_cells if c.get("scanned"))
            logger.info(
                "[%s] marked %d new cells at pos=(%.0f,%.0f), total scanned=%d/%d",
                self.name, new_marked, rx, ry, scanned_total, len(self._sweep_grid_cells),
            )
            now = time.time()
            if now - self._sweep_last_pub >= 0.5:
                self._sweep_last_pub = now
                self._publish_scan_grid(delta_only=True)

    def _finish_sweep(self) -> py_trees.common.Status:
        """Complete the sweep scan — mark remaining cells and zone as scanned."""
        for cell in self._sweep_grid_cells:
            cell["scanned"] = True
        scanned = len(self._sweep_grid_cells)
        zone_id = self.params.get("zone_id", "?")
        logger.info(
            "[%s] sweep scan complete: %d cells scanned in zone '%s'",
            self.name, scanned, zone_id,
        )
        self._update_zone_coverage("scanned")
        self._publish_scan_grid(delta_only=False)
        self._write_effects()

        bb = py_trees.blackboard.Blackboard.storage
        bb[f"zones/{zone_id}/scan_result"] = "clear"
        if not bb.get("bomb_detected"):
            bb["bomb_detected"] = False

        return py_trees.common.Status.SUCCESS

    def _publish_scan_grid(self, delta_only: bool = False) -> None:
        """Publish grid cell state for minimap visualization.

        delta_only=True  → only transmit currently-scanned cells (incremental update)
        delta_only=False → transmit all cells (initial full state or final)
        """
        if not self._zenoh_bridge:
            return
        zone_id = self.params.get("zone_id") or self.params.get("zone", "")
        total = len(self._sweep_grid_cells)
        scanned_cells = [c for c in self._sweep_grid_cells if c.get("scanned")]
        scanned_count = len(scanned_cells)

        if delta_only:
            # Only send scanned cells as delta; frontend merges into existing state
            cells_payload = [
                {"row": c["row"], "col": c["col"],
                 "cx": c["cx"], "cy": c["cy"], "scanned": True}
                for c in scanned_cells
            ]
        else:
            cells_payload = [
                {"row": c["row"], "col": c["col"],
                 "cx": c["cx"], "cy": c["cy"], "scanned": c.get("scanned", False)}
                for c in self._sweep_grid_cells
            ]

        self._zenoh_bridge.publish_scan_grid({
            "entity_id": self.entity_id,
            "zone_id": zone_id,
            "cells": cells_payload,
            "delta": delta_only,
            "progress": scanned_count / total if total > 0 else 1.0,
            "total": total,
            "scanned_count": scanned_count,
        })

    # ── Internal: zone coverage ───────────────────────────────────────────────

    def _update_zone_coverage(self, coverage_status: str) -> None:
        """Update zone coverage_status on the Blackboard and publish to Zenoh.

        States:
            en_route        -- navigation dispatched toward this zone
            arrived         -- navigation completed, entity reached the zone
            scanning        -- scan/detect dispatched for this zone
            scanned         -- scan completed, zone cleared
        """
        zone_id = self.params.get("zone_id") or self.params.get("zone")
        if not zone_id:
            return

        bb = py_trees.blackboard.Blackboard.storage
        bb[f"zones/{zone_id}/coverage_status"] = {
            "status": coverage_status,
            "entity_id": self.entity_id,
            "timestamp": time.time(),
        }
        if coverage_status == "arrived":
            bb[f"zones/{zone_id}/explored"] = True
        elif coverage_status == "scanned":
            bb[f"zones/{zone_id}/cleared"] = True
            self._check_all_zones_cleared(bb)

        if not self._zenoh_bridge:
            return

        # Collect full coverage map from Blackboard for the broadcast payload
        zones_coverage: dict = {}
        unchecked = 0
        for key, val in bb.items():
            if isinstance(key, str) and key.endswith("/coverage_status"):
                zid = key.split("/")[1]
                if isinstance(val, dict):
                    zones_coverage[zid] = val
                else:
                    zones_coverage[zid] = {"status": str(val)}
                if zones_coverage[zid].get("status", "unchecked") in ("unchecked",):
                    unchecked += 1

        total = len(zones_coverage)
        checked = total - unchecked
        self._zenoh_bridge.publish_zone_coverage({
            "zones": zones_coverage,
            "summary": {"total": total, "checked": checked, "unchecked": unchecked},
        })
        logger.info(
            "[%s] zone '%s' coverage_status → %s (%d/%d zones checked)",
            self.name, zone_id, coverage_status, checked, total,
        )

    def _check_all_zones_cleared(self, bb) -> None:
        """After marking a zone cleared, check if ALL navigable zones are now cleared."""
        from app.execution.param_resolver import ParamResolver

        zone_registry = ParamResolver.get_zone_registry()
        if not zone_registry:
            return

        zone_ids = [
            zid for zid, zdata in zone_registry.items()
            if zdata.get("mobility") != "constrained"
        ]
        if not zone_ids:
            return

        all_cleared = all(
            bb.get(f"zones/{zid}/cleared") or bb.get(f"/zones/{zid}/cleared")
            for zid in zone_ids
        )
        if all_cleared:
            bb["all_zones_cleared"] = True
            logger.info(
                "[%s] all %d navigable zones cleared — setting all_zones_cleared=True",
                self.name, len(zone_ids),
            )

    # ── Internal: helpers ─────────────────────────────────────────────────────

    def _get_entity_state(self) -> dict:
        bb_storage = py_trees.blackboard.Blackboard.storage
        return bb_storage.get(f"entity_state/{self.entity_id}", {})
