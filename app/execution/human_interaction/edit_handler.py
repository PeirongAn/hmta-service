"""Handle BT edit requests from any operator terminal.

Subscribes to ``zho/bt/edit`` and applies structural changes to the
running behaviour tree or triggers a full re-generation via the
pipeline.

Edit types
----------
* ``reassign``   — change the entity assigned to a BT node
* ``add_node``   — insert a new node into the BT
* ``remove_node``— remove a node from the BT
* ``modify_node``— change properties of an existing node
* ``regenerate`` — discard current BT, re-run the generation pipeline
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from app.execution.engine import ExecutionEngine
    from app.zenoh_bridge import ZenohBridge

logger = logging.getLogger(__name__)

_TOPIC = "zho/bt/edit"
_ACK_TOPIC = "zho/bt/edit/ack"


class EditHandler:
    """Handles live BT edits and re-generation requests."""

    def __init__(
        self,
        zenoh: "ZenohBridge",
        engine: "ExecutionEngine",
        regenerate_callback: Callable[[], None] | None = None,
    ) -> None:
        self._zenoh = zenoh
        self._engine = engine
        self._regenerate_callback = regenerate_callback
        self._sub = None

    def start(self) -> None:
        self._sub = self._zenoh.subscribe(_TOPIC, self._on_message)
        logger.info("EditHandler listening on %s", _TOPIC)

    def stop(self) -> None:
        if self._sub:
            try:
                self._sub.undeclare()
            except Exception:
                pass
            self._sub = None

    # ------------------------------------------------------------------

    def _on_message(self, sample: Any) -> None:
        try:
            data = json.loads(bytes(sample.payload))
        except Exception:
            logger.warning("EditHandler: invalid payload")
            return

        edit_type = data.get("edit_type", "")
        operator = data.get("operator_id", "unknown")
        params = data.get("params", {})

        logger.info("BT edit [%s] from %s params=%s", edit_type, operator, params)

        handler = {
            "reassign": self._handle_reassign,
            "add_node": self._handle_add_node,
            "remove_node": self._handle_remove_node,
            "modify_node": self._handle_modify_node,
            "regenerate": self._handle_regenerate,
        }.get(edit_type)

        if handler is None:
            logger.warning("Unknown edit type: %s", edit_type)
            self._ack(edit_type, operator, False, f"unknown type: {edit_type}")
            return

        try:
            handler(operator, params)
            self._ack(edit_type, operator, True)
        except Exception as exc:
            logger.exception("BT edit [%s] failed", edit_type)
            self._ack(edit_type, operator, False, str(exc))

    # ------------------------------------------------------------------

    def _handle_reassign(self, operator: str, params: dict) -> None:
        node_id = params.get("node_id")
        new_entity = params.get("new_entity")
        if not node_id or not new_entity:
            raise ValueError("reassign requires node_id and new_entity")
        self._engine.reassign_node(node_id, new_entity)

    def _handle_add_node(self, operator: str, params: dict) -> None:
        node_def = params.get("node")
        parent_id = params.get("parent_id")
        if not node_def:
            raise ValueError("add_node requires params.node")
        self._engine.add_bt_node(node_def, parent_id)

    def _handle_remove_node(self, operator: str, params: dict) -> None:
        node_id = params.get("node_id")
        if not node_id:
            raise ValueError("remove_node requires node_id")
        self._engine.remove_bt_node(node_id)

    def _handle_modify_node(self, operator: str, params: dict) -> None:
        node_id = params.get("node_id")
        updates = params.get("updates", {})
        if not node_id:
            raise ValueError("modify_node requires node_id")
        self._engine.modify_bt_node(node_id, updates)

    def _handle_regenerate(self, operator: str, params: dict) -> None:
        if self._regenerate_callback:
            self._regenerate_callback()
        else:
            raise RuntimeError("No regenerate callback configured")

    # ------------------------------------------------------------------

    def _ack(self, edit_type: str, operator: str, success: bool, error: str | None = None) -> None:
        msg = {
            "edit_type": edit_type,
            "operator_id": operator,
            "success": success,
        }
        if error:
            msg["error"] = error
        try:
            self._zenoh.publish(_ACK_TOPIC, msg)
        except Exception:
            pass
