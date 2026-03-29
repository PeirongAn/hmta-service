"""Handle runtime interventions from any operator terminal.

Subscribes to ``zho/human/intervention`` and dispatches to the
execution engine.  The protocol is device-agnostic: MR headset,
tablet, PC browser, or command-vehicle screen all send the same
Zenoh message.

Intervention types
------------------
* ``pause``            — pause the BT tick loop
* ``resume``           — resume the BT tick loop
* ``takeover``         — suspend a specific entity's branch
* ``abort_entity``     — remove an entity from the BT
* ``modify_blackboard``— write an arbitrary key/value to the BB
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.execution.engine import ExecutionEngine
    from app.zenoh_bridge import ZenohBridge

logger = logging.getLogger(__name__)

_TOPIC = "zho/human/intervention"
_ACK_TOPIC = "zho/human/intervention/ack"


class InterventionHandler:
    """Stateless dispatcher — one instance per engine lifetime."""

    def __init__(self, zenoh: "ZenohBridge", engine: "ExecutionEngine") -> None:
        self._zenoh = zenoh
        self._engine = engine
        self._sub = None

    def start(self) -> None:
        self._sub = self._zenoh.subscribe(_TOPIC, self._on_message)
        logger.info("InterventionHandler listening on %s", _TOPIC)

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
            logger.warning("InterventionHandler: invalid payload")
            return

        itype = data.get("intervention_type", "")
        operator = data.get("operator_id", "unknown")
        target = data.get("target_entity_id")
        params = data.get("params", {})

        logger.info(
            "Intervention [%s] from %s target=%s params=%s",
            itype, operator, target, params,
        )

        handler = {
            "pause": self._handle_pause,
            "resume": self._handle_resume,
            "takeover": self._handle_takeover,
            "abort_entity": self._handle_abort_entity,
            "modify_blackboard": self._handle_modify_bb,
        }.get(itype)

        if handler is None:
            logger.warning("Unknown intervention type: %s", itype)
            self._ack(itype, operator, False, f"unknown type: {itype}")
            return

        try:
            handler(operator, target, params)
            self._ack(itype, operator, True)
        except Exception as exc:
            logger.exception("Intervention [%s] failed", itype)
            self._ack(itype, operator, False, str(exc))

    # ------------------------------------------------------------------

    def _handle_pause(self, operator: str, target: str | None, params: dict) -> None:
        self._engine.pause()

    def _handle_resume(self, operator: str, target: str | None, params: dict) -> None:
        self._engine.resume()

    def _handle_takeover(self, operator: str, target: str | None, params: dict) -> None:
        if not target:
            raise ValueError("takeover requires target_entity_id")
        self._engine.takeover_entity(target)

    def _handle_abort_entity(self, operator: str, target: str | None, params: dict) -> None:
        if not target:
            raise ValueError("abort_entity requires target_entity_id")
        self._engine.abort_entity(target)

    def _handle_modify_bb(self, operator: str, target: str | None, params: dict) -> None:
        key = params.get("key")
        value = params.get("value")
        if not key:
            raise ValueError("modify_blackboard requires params.key")
        self._engine.set_blackboard(key, value)

    # ------------------------------------------------------------------

    def _ack(self, itype: str, operator: str, success: bool, error: str | None = None) -> None:
        msg = {
            "intervention_type": itype,
            "operator_id": operator,
            "success": success,
        }
        if error:
            msg["error"] = error
        try:
            self._zenoh.publish(_ACK_TOPIC, msg)
        except Exception:
            pass
