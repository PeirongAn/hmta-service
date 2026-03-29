"""Blackboard Sync — keeps py_trees Blackboard up-to-date from Zenoh entity state."""

from __future__ import annotations

import logging

import py_trees

logger = logging.getLogger(__name__)


class BlackboardSync:
    """
    Subscribes to Zenoh entity state and writes normalized values into the
    py_trees Blackboard.

    Ownership model: this client ('zenoh_sync') is the sole WRITE owner of all
    entity runtime-state keys (position, status, comm_status, battery).
    Keys are registered lazily on first encounter so that entities not present
    in the generated BT (e.g. environment bystanders) are handled gracefully.

    Completion detection: UE does NOT send explicit command callbacks — it
    continuously publishes entity state.  When an entity's status returns to a
    terminal/idle state after having been in a busy state, BlackboardSync infers
    that the in-flight command has completed and notifies CommandResolver.
    """

    # States that mean "entity is executing a command" (lowercase — raw values are normalised)
    _BUSY_STATES: frozenset[str] = frozenset(
        {"moving", "navigating", "scanning", "executing", "busy", "working", "running"}
    )
    # States that mean "entity is done / available"
    _IDLE_STATES: frozenset[str] = frozenset(
        {"idle", "ready", "arrived", "completed", "standby", "waiting"}
    )
    # States that mean the command failed
    _FAIL_STATES: frozenset[str] = frozenset(
        {"stuck", "error", "fault", "failed", "comm_lost", "offline",
         "disconnected", "dead", "destroyed"}
    )

    def __init__(self, zenoh_bridge, command_resolver=None):
        self._zenoh = zenoh_bridge
        self._command_resolver = command_resolver
        self._oracle_service = None
        self._bb = py_trees.blackboard.Client(name="zenoh_sync")
        self._writable: set[str] = set()
        self._attempted: set[str] = set()
        self._last_status: dict[str, str] = {}
        # Dynamic detection configuration (set by ExecutionEngine on mission load)
        self._active_alert_classes: set[str] | None = None
        self._mission_goal_key: str | None = None

    def set_oracle_service(self, oracle) -> None:
        """Inject OracleService for detection validation."""
        self._oracle_service = oracle

    def configure_detection(
        self,
        alert_classes: list[str],
        goal_key: str | None = None,
    ) -> None:
        """Set dynamic detection classes and mission goal key.

        When *alert_classes* is non-empty, ``_on_entity_event`` uses them
        instead of the static ``_BOMB_KEYWORDS`` for threat matching.
        When *goal_key* is set (e.g. ``"person_found"``), the key is also
        written to the Blackboard on detection so the engine's structured
        goal check can trigger.
        """
        if alert_classes:
            self._active_alert_classes = {c.lower() for c in alert_classes}
        else:
            self._active_alert_classes = None
        self._mission_goal_key = goal_key
        logger.info(
            "BlackboardSync detection configured: alert_classes=%s goal_key=%s",
            self._active_alert_classes, self._mission_goal_key,
        )

    def start(self) -> None:
        self._zenoh.subscribe_entity_states(self._on_entity_state)
        self._zenoh.subscribe_action_results(self._on_action_result)
        self._zenoh.subscribe_param_responses(self._on_param_response)
        self._zenoh.subscribe_entity_events(self._on_entity_event)
        self._zenoh.subscribe_entity_offline(self._on_entity_offline)
        logger.info("BlackboardSync started (entity states + action results + param responses + entity events + entity offline)")

    def _ensure_writable(self, key: str) -> bool:
        """
        Register *key* for WRITE access on first encounter.
        Returns True if this client can write to the key.
        """
        if key in self._writable:
            return True
        if key in self._attempted:
            return False  # Already failed — don't retry

        self._attempted.add(key)
        try:
            self._bb.register_key(key=key, access=py_trees.common.Access.WRITE)
            self._writable.add(key)
            return True
        except AttributeError:
            # Another client already owns WRITE access for this key
            logger.warning(
                "BlackboardSync: cannot claim WRITE for '%s' — owned by another client", key
            )
            return False

    @staticmethod
    def _normalize_state(raw: str) -> str:
        """Normalize UE state enum values to plain lowercase tokens.

        UE sends prefixed enums like ``ELS_IDLE``, ``ELS_MOVING``.
        Strip common prefixes so they match the canonical state sets.
        """
        s = raw.strip().lower()
        for prefix in ("els_", "entity_state_", "es_"):
            if s.startswith(prefix):
                s = s[len(prefix):]
                break
        return s

    def _on_entity_state(self, entity_id: str, data: dict) -> None:
        # UE publishes "state" + "pose"; some adapters normalize to "status" + "position".
        # Support both field name conventions.
        raw_status: str = (
            data.get("status")          # normalized form
            or data.get("state")        # UE native form  (e.g. "ELS_IDLE", "ELS_MOVING")
            or "unknown"
        )
        new_status = self._normalize_state(raw_status)

        # Position: UE sends {"pose": {"x":…,"y":…,"z":…}} or {"position": {…}}
        position = data.get("position") or data.get("pose") or {}

        writes: dict[str, object] = {
            f"entities/{entity_id}/position":    position,
            f"entities/{entity_id}/status":      new_status,
            f"entities/{entity_id}/comm_status": data.get("comm_status", "online"),
        }
        battery = data.get("battery_level") or data.get("battery")
        if battery is not None:
            writes[f"entities/{entity_id}/battery"] = battery

        for key, value in writes.items():
            if not self._ensure_writable(key):
                continue
            try:
                self._bb.set(key, value)
            except Exception as exc:
                logger.warning("BlackboardSync: set('%s') failed: %s", key, exc)

        # ── Command completion inference from state transitions ────────────────
        # UE does not send explicit callbacks; instead it continuously updates
        # entity state.  Detect busy→idle (completed) and any→fail transitions.
        self._detect_completion(entity_id, new_status)

    def _on_action_result(self, entity_id: str, data: dict) -> None:
        """Handle explicit action completion from UE."""
        result = (data.get("result") or "").upper()
        action_type = data.get("actionType", "?")
        node_id = data.get("nodeId", "?")
        event_type = (data.get("event_type") or "").lower()
        logger.info(
            "Action result received: entity=%s action=%s nodeId=%s result=%s event_type=%s",
            entity_id, action_type, node_id, result, event_type or "-",
        )

        if event_type in self._THREAT_EVENT_TYPES or event_type == "bomb_detected":
            bb = py_trees.blackboard.Blackboard.storage
            bb["/bomb_detected"] = True
            bb["bomb_detected"] = True
            position = data.get("position") or {}
            loc = {
                "zone_id": data.get("zone_id", ""),
                "position": position,
                "entity_id": entity_id,
                "event_type": event_type,
            }
            bb["/bomb_location"] = loc
            bb["bomb_location"] = loc
            logger.warning(
                "BOMB DETECTED via action_result from %s — bomb_detected=True on Blackboard",
                entity_id,
            )

            if self._oracle_service and position:
                try:
                    self._oracle_service.judge_detection(entity_id, position, data)
                except Exception:
                    logger.debug("Oracle judge_detection failed", exc_info=True)

        if not self._command_resolver:
            logger.warning("Action result received but no CommandResolver attached")
            return

        # Ignore benign UE state updates that look like action results but
        # carry no actionable payload (UNKNOWN action, empty nodeId, empty
        # result).  These come from generic entity-state broadcasts and
        # would otherwise cause spurious command failures.
        is_benign = (
            (action_type in ("?", "UNKNOWN", ""))
            and (not node_id or node_id == "?")
        )
        if is_benign:
            logger.debug(
                "Ignoring benign action_result from %s (action=%s nodeId=%s)",
                entity_id, action_type, node_id,
            )
            return

        found = self._command_resolver.complete_by_action_result(entity_id, data)
        if not found:
            logger.warning(
                "Action result for %s (nodeId=%s) did not match any in-flight command",
                entity_id, node_id,
            )

    def _on_param_response(self, data: dict) -> None:
        """Handle parameter responses from the IDE operator.

        Writes the response into the Blackboard keyed by ``param_response/{node_id_suffix}``
        so that the corresponding ``CommandAction`` can pick it up on next tick.
        """
        node_id = data.get("node_id", "")
        params = data.get("params", {})
        suffix = node_id.replace("py-", "") if node_id.startswith("py-") else node_id
        bb_key = f"param_response/{suffix}"
        py_trees.blackboard.Blackboard.storage[bb_key] = params
        logger.info("Param response written to Blackboard[%s]: %s", bb_key, list(params.keys()))

    _THREAT_EVENT_TYPES: frozenset[str] = frozenset({
        "bomb_detected", "threat_detected", "explosive_found",
        "suspicious_object", "scan_positive",
    })

    _BOMB_KEYWORDS: frozenset[str] = frozenset({
        "bomb", "explosive", "ied", "threat", "suspicious",
    })

    def _on_entity_event(self, entity_id: str, data: dict) -> None:
        """Handle entity events — in particular detection alerts from UE.

        Keyword matching uses ``_active_alert_classes`` (set by
        ``configure_detection``) when available, falling back to the
        static ``_BOMB_KEYWORDS`` for backward compatibility.
        """
        event_type = (data.get("event_type") or data.get("type") or "").lower()
        logger.info("Entity event received: entity=%s type=%s", entity_id, event_type)

        if event_type == "entity_offline":
            self._on_entity_offline(entity_id, data)
            return

        is_threat = event_type in self._THREAT_EVENT_TYPES
        detected = ""

        if not is_threat and event_type == "object_detected":
            nested = data.get("data") or {}
            detected = (
                data.get("objectType")
                or data.get("detectedClass")
                or data.get("detected_class")
                or data.get("className")
                or (nested.get("class") if isinstance(nested, dict) else None)
                or (nested.get("objectType") if isinstance(nested, dict) else None)
                or data.get("message")
                or ""
            ).lower()
            keywords = (
                self._active_alert_classes
                if self._active_alert_classes is not None
                else self._BOMB_KEYWORDS
            )
            is_threat = any(kw in detected for kw in keywords)

        if is_threat:
            bb = py_trees.blackboard.Blackboard.storage
            bb["/bomb_detected"] = True
            bb["bomb_detected"] = True

            nested = data.get("data") if isinstance(data.get("data"), dict) else {}
            zone_id = data.get("zone_id") or nested.get("zone_id", "")
            position = data.get("position") or data.get("pose") or nested.get("position") or {}
            loc = {
                "zone_id": zone_id,
                "position": position,
                "entity_id": entity_id,
                "event_type": event_type,
                "detected_class": detected,
                "confidence": nested.get("confidence"),
            }
            bb["/bomb_location"] = loc
            bb["bomb_location"] = loc

            # Write mission-specific goal key so structured goal check triggers
            if self._mission_goal_key:
                bb[f"/{self._mission_goal_key}"] = True
                bb[self._mission_goal_key] = True

            if zone_id:
                bb[f"zones/{zone_id}/scan_result"] = "bomb_found"

            logger.warning(
                "DETECTION ALERT by %s (type=%s, zone=%s, class=%s) — "
                "bomb_detected=True%s on Blackboard",
                entity_id, event_type, zone_id,
                nested.get("class", "?") if isinstance(nested, dict) else "?",
                f", {self._mission_goal_key}=True" if self._mission_goal_key else "",
            )

            if self._oracle_service and position:
                try:
                    self._oracle_service.judge_detection(entity_id, position, data)
                except Exception:
                    logger.debug("Oracle judge_detection failed", exc_info=True)

    def _on_entity_offline(self, entity_id: str, data: dict) -> None:
        """Handle offline notification from Node.js NodeRegistry via Zenoh."""
        logger.warning("Entity %s reported offline by NodeRegistry", entity_id)
        storage = py_trees.blackboard.Blackboard.storage
        for key, val in [
            (f"entities/{entity_id}/status", "offline"),
            (f"entities/{entity_id}/comm_status", "offline"),
        ]:
            storage[f"/{key}"] = val
            storage[key] = val
            if self._ensure_writable(key):
                try:
                    self._bb.set(key, val)
                except Exception:
                    pass

    def _detect_completion(self, entity_id: str, new_status: str) -> None:
        """Infer command completion from entity status transitions."""
        if not self._command_resolver:
            return

        prev_status = self._last_status.get(entity_id)
        self._last_status[entity_id] = new_status

        if prev_status is None:
            return  # First update — no transition yet

        if prev_status != new_status:
            logger.info(
                "BlackboardSync: %s state transition: '%s' → '%s'",
                entity_id, prev_status, new_status,
            )

        prev_busy = prev_status in self._BUSY_STATES
        now_idle  = new_status  in self._IDLE_STATES
        now_fail  = new_status  in self._FAIL_STATES

        if prev_busy and now_idle:
            logger.info("BlackboardSync: %s completed (busy→idle)", entity_id)
            self._command_resolver.complete_by_entity(entity_id)
        elif now_fail and prev_status not in self._FAIL_STATES:
            logger.warning("BlackboardSync: %s failed → '%s'", entity_id, new_status)
            self._command_resolver.complete_by_entity(entity_id, error=new_status)
