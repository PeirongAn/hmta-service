"""Command Resolver — routes AbstractCommand to Robot or Human translator."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.schemas.command import AbstractCommand, CommandStatus, ResolveResult

if TYPE_CHECKING:
    from app.execution.command.human_translator import HumanTranslator
    from app.execution.command.robot_translator import RobotTranslator
    from app.zenoh_bridge import ZenohBridge

logger = logging.getLogger(__name__)


class CommandResolver:
    """
    Central dispatcher for the three-layer command model.

    Flow:
        AbstractCommand → SchemaRegistry lookup → capability check
        → entity_type branch → Translator (L2) → Zenoh publish (L3)
        → track CommandStatus
    """

    def __init__(
        self,
        schema_registry: dict,        # entity_id → entity dict (simple in-memory)
        capability_registry: dict,    # entity_id → set[capability_name]
        zenoh_bridge: "ZenohBridge",
    ):
        self._schema: dict = schema_registry
        self._capabilities: dict = capability_registry
        self._zenoh = zenoh_bridge
        self._commands: dict[str, CommandStatus] = {}
        self._directive_task_map: dict[str, str] = {}  # command_id → task_id

        from app.execution.command.robot_translator import RobotTranslator
        from app.execution.command.human_translator import HumanTranslator

        self._robot_tx: RobotTranslator = RobotTranslator()
        self._human_tx: HumanTranslator = HumanTranslator()

    # ── Public API ────────────────────────────────────────────────────────────

    _MOTION_INTENTS = frozenset({
        "navigate", "navigation", "move", "patrol", "follow_by_path",
    })

    _DETECT_INTENTS = frozenset({
        "detect", "scan", "surveil", "observe",
    })

    def resolve(self, command: AbstractCommand) -> ResolveResult:
        entity = self._schema.get(command.entity_id)
        if not entity:
            return ResolveResult(error="ENTITY_NOT_FOUND")

        if not self._has_capability(command.entity_id, command.intent):
            return ResolveResult(error="CAPABILITY_MISMATCH")

        if entity.get("status") == "offline":
            return ResolveResult(error="ENTITY_OFFLINE")

        if entity.get("comm_status") in ("offline", "comm_lost"):
            return ResolveResult(error="COMM_LOST")

        # Per-entity motion mutex: cancel any in-flight motion command for
        # the same entity before dispatching a new one. UE can only process
        # one movement command per entity; sending two causes the first to
        # hang forever without an action_result.
        intent_lower = (command.intent or "").lower().rsplit(".", 1)[-1]
        if intent_lower in self._MOTION_INTENTS:
            for cmd_id, status in list(self._commands.items()):
                if (
                    status.entity_id == command.entity_id
                    and status.state in ("dispatched", "running")
                ):
                    self.update_status(cmd_id, "cancelled", error="superseded by new motion command")
                    logger.warning(
                        "Cancelled stale command %s for entity %s (superseded by new %s)",
                        cmd_id, command.entity_id, command.intent,
                    )

        try:
            if entity.get("entity_type") == "robot":
                typed_cmd = self._robot_tx.translate(command, entity)
                from app.execution.command.ue_adapter import to_ue_payload
                ue_payload = to_ue_payload(typed_cmd.model_dump())
                if ue_payload.get("_invalid_target"):
                    logger.warning(
                        "Blocking NAVIGATE with invalid target (0,0,0) for %s — would cause entity loss in UE",
                        command.entity_id,
                    )
                    return ResolveResult(
                        error=f"INVALID_TARGET: navigation target is origin (0,0,0) for {command.entity_id}, "
                              "likely missing zone/waypoint data — check ParamResolver or bt_builder",
                    )
                logger.info("UE payload for %s → %s: %s", command.entity_id, ue_payload.get("actionType"), ue_payload)
                self._zenoh.publish_robot_command(command.entity_id, ue_payload)

                # Task-level detection config override: when dispatching a
                # DETECT/SCAN command with target_classes, also push a
                # detection config so UE updates its alert filter in real time.
                if intent_lower in self._DETECT_INTENTS:
                    self._push_task_detection_config(command)
            else:
                directive = self._human_tx.translate(command, entity)
                self._zenoh.publish_human_directive(command.entity_id, directive.model_dump())
        except Exception as exc:
            logger.exception("Translation/dispatch error for %s", command.command_id)
            return ResolveResult(error=f"DISPATCH_ERROR: {exc}")

        status = CommandStatus(
            command_id=command.command_id,
            entity_id=command.entity_id,
            node_id=command.node_id or "",
            state="dispatched",
        )
        self._commands[command.command_id] = status
        logger.info("Dispatched command_id=%s intent=%s entity=%s node_id=%s",
                     command.command_id, command.intent, command.entity_id, command.node_id)
        return ResolveResult(command_id=command.command_id)

    def get_status(self, command_id: str) -> CommandStatus:
        return self._commands.get(command_id, CommandStatus(state="unknown"))

    def update_status(self, command_id: str, state: str, error: str | None = None) -> None:
        if command_id in self._commands:
            self._commands[command_id].state = state  # type: ignore[assignment]
            self._commands[command_id].error = error
        else:
            self._commands[command_id] = CommandStatus(command_id=command_id, state=state, error=error)  # type: ignore[arg-type]

    def cancel(self, command_id: str) -> None:
        self.update_status(command_id, "cancelled")

    def complete_by_entity(self, entity_id: str, error: str | None = None) -> bool:
        """
        Mark the most-recently dispatched in-flight command for *entity_id* as
        completed (or failed).  Called by BlackboardSync when UE state indicates
        the entity has returned to idle — no explicit callback required.

        Returns True if a command was found and updated.
        """
        # Walk commands newest-first (dict preserves insertion order in Python 3.7+)
        for cmd_id, status in reversed(list(self._commands.items())):
            if status.entity_id != entity_id:
                continue
            if status.state in ("dispatched", "running"):
                new_state = "failed" if error else "completed"
                self.update_status(cmd_id, new_state, error=error)
                logger.info(
                    "State-inferred %s: command_id=%s entity=%s",
                    new_state, cmd_id, entity_id,
                )
                return True
        return False

    def complete_by_action_result(self, entity_id: str, data: dict) -> bool:
        """Handle explicit action result from UE.

        Matches the in-flight command by ``nodeId`` (preferred) then falls back
        to ``entity_id``.  ``data["result"]`` must be ``"SUCCESS"`` or ``"FAILURE"``.
        """
        node_id = data.get("nodeId", "")
        result = (data.get("result") or "").upper()
        error = data.get("message") if result == "FAILURE" else None

        if node_id:
            for cmd_id, status in reversed(list(self._commands.items())):
                if status.node_id == node_id and status.state in ("dispatched", "running"):
                    new_state = "failed" if result == "FAILURE" else "completed"
                    self.update_status(cmd_id, new_state, error=error)
                    logger.info(
                        "Explicit %s (nodeId match): command_id=%s entity=%s nodeId=%s",
                        new_state, cmd_id, entity_id, node_id,
                    )
                    return True

        return self.complete_by_entity(entity_id, error=error)

    # ── Directive query API (used by HumanMonitor) ─────────────────────────────

    def get_open_directives(self, entity_id: str) -> list[str]:
        """Return command_ids of in-flight directives for *entity_id*."""
        return [
            cmd_id
            for cmd_id, status in self._commands.items()
            if status.entity_id == entity_id
            and status.state in ("dispatched", "running")
        ]

    def get_directive_response(self, directive_id: str) -> dict | None:
        """Return stored response data for a completed directive.

        Returns ``None`` if the directive is still in-flight or unknown.
        The response dict is populated by ``ResponseResolver`` when the
        human submits their decision.
        """
        status = self._commands.get(directive_id)
        if not status:
            return None
        if status.state not in ("completed", "failed"):
            return None
        return {
            "status": status.state,
            "error": status.error,
            "task_id": self._directive_task_map.get(directive_id, ""),
            "params": (status.result or {}).get("params", {}),
            "decision": (status.result or {}).get("decision", status.state),
        }

    def set_directive_task_id(self, command_id: str, task_id: str) -> None:
        """Associate a directive command with its originating task_id."""
        self._directive_task_map[command_id] = task_id

    def sync_capabilities_from_graph(self, capability_registry) -> None:
        """Bulk-import capabilities from the HyperGraph-backed CapabilityRegistry.

        Called once when the BT is loaded, so that CommandResolver has the
        correct capability set even if the Zenoh entity registration events
        were published before this service subscribed.
        """
        try:
            graph = capability_registry.get_graph_ref()
            for nid, node in graph.nodes.items():
                if node.kind != "entity":
                    continue
                caps: set[str] = set()
                for edge in graph.edges_of(nid, "has_capability"):
                    for peer in edge.nodes:
                        if peer != nid:
                            caps.add(peer)
                if caps:
                    existing = self._capabilities.get(nid, set())
                    merged = existing | caps
                    self._capabilities[nid] = merged
                    logger.info(
                        "sync_capabilities_from_graph: %s → %s", nid, merged,
                    )
                # Ensure entity_type is set in the schema registry
                if nid not in self._schema:
                    self._schema[nid] = {"entity_id": nid}
                etype = node.attrs.get("entity_type", "")
                if etype:
                    self._schema[nid]["entity_type"] = etype
        except Exception as exc:
            logger.warning("sync_capabilities_from_graph failed: %s", exc)

    # ── Task-level detection config ─────────────────────────────────────────

    def _push_task_detection_config(self, command: AbstractCommand) -> None:
        """Push ``actor/{id}/detection/config`` when a DETECT/SCAN task
        carries ``target_classes`` — acts as a temporary per-task override
        on the UE side.
        """
        params = command.params or {}
        target_classes = params.get("target_classes", "")
        if not target_classes:
            return
        alert_classes = [c.strip() for c in str(target_classes).split(",") if c.strip()]
        if not alert_classes:
            return
        config = {
            "alert_classes": alert_classes,
            "min_confidence": float(params.get("min_confidence", 0.3)),
            "stop_on_alert": True,
            "stop_instead_of_pause": False,
        }
        self._zenoh.publish_detection_config(command.entity_id, config)

    # ── Registry helpers ──────────────────────────────────────────────────────

    def register_entity(self, entity: dict) -> None:
        """Upsert entity into the in-memory registry.

        Only overwrites capabilities when the payload explicitly contains
        ``capabilities`` or ``structured_capabilities``.  State-only updates
        (position, battery, etc.) preserve the previously registered set.
        """
        eid = entity.get("entity_id", "")
        self._schema[eid] = entity

        has_cap_fields = (
            "capabilities" in entity
            or "structured_capabilities" in entity
            or "structuredCapabilities" in entity
        )
        if not has_cap_fields:
            # State-only update — preserve existing capabilities
            return

        caps: set[str] = set()
        for c in entity.get("capabilities", []):
            caps.add(c.get("name", "") if isinstance(c, dict) else str(c))
        sc_list = entity.get("structured_capabilities") or entity.get("structuredCapabilities", [])
        for sc in sc_list:
            if isinstance(sc, dict):
                name = sc.get("id") or sc.get("name") or ""
                if name:
                    caps.add(str(name))
        caps.discard("")
        if caps or eid not in self._capabilities:
            self._capabilities[eid] = caps
            logger.debug("Registered capabilities for %s: %s", eid, caps)

    # Directive intents that humans can always receive regardless of
    # which capabilities are registered in their profile.
    _HUMAN_DIRECTIVE_INTENTS = frozenset({
        "path_planning", "plan_path", "plan_patrol", "plan_route",
        "approve", "approval", "command.approve",
        "observation", "observe", "reconnaissance", "survey",
        "override", "manual_control", "command.override",
        "remote_operate", "remote_control",
        "report", "system.report",
        "follow_by_path",
        "task.assist", "task_assist",
        "goal_confirmation",
    })

    def _has_capability(self, entity_id: str, intent: str) -> bool:
        entity = self._schema.get(entity_id, {})
        if entity.get("entity_type") == "human":
            if intent in self._HUMAN_DIRECTIVE_INTENTS:
                return True
            from app.capability.ontology import resolve_alias
            resolved = resolve_alias(intent)
            if resolved in self._HUMAN_DIRECTIVE_INTENTS:
                return True
        caps = self._capabilities.get(entity_id, set())
        if intent in caps:
            return True
        from app.capability.ontology import resolve_alias
        resolved = resolve_alias(intent)
        for cap in caps:
            if resolve_alias(cap) == resolved:
                return True
        logger.warning(
            "CAPABILITY_MISMATCH detail: entity=%s intent=%r resolved=%r caps=%s",
            entity_id, intent, resolved, caps,
        )
        return False
