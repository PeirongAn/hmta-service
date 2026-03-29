"""FSM Manager — creates, drives, and syncs entity FSM instances."""

from __future__ import annotations

import logging
import time

import py_trees
from statemachine import StateMachine

from app.execution.fsm.human_fsm import HumanFSM
from app.execution.fsm.robot_fsm import RobotFSM

logger = logging.getLogger(__name__)


def _get_state_id(fsm: StateMachine) -> str:
    """Read the current FSM state without relying on deprecated properties."""
    current_state = getattr(fsm, "current_state_value", None)
    if current_state is not None:
        return str(current_state)
    return fsm.current_state.id


class FSMManager:
    """
    Lifecycle manager for per-entity StateMachine instances.

    Responsibilities:
    - create_instance(): instantiate the correct FSM class for each entity
    - trigger(): fire a named event, silently ignore invalid transitions
    - sync_to_blackboard(): called every BT tick (pre_tick_handler)
    - get_transition_history(): for debugging and Task Profiler
    """

    def __init__(self):
        self.instances: dict[str, StateMachine] = {}
        self._history: list[dict] = []
        # Persistent Blackboard client — keys registered lazily on first sync
        self._bb = py_trees.blackboard.Client(name="fsm_manager")
        self._bb_writable: set[str] = set()
        self._bb_attempted: set[str] = set()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def create_instance(self, entity_id: str, entity_type: str) -> None:
        if entity_id in self.instances:
            logger.debug("FSM already exists for %s — skipping", entity_id)
            return
        if entity_type == "robot":
            self.instances[entity_id] = RobotFSM(entity_id, sync_callback=self._on_state_change)
        elif entity_type == "human":
            self.instances[entity_id] = HumanFSM(entity_id, sync_callback=self._on_state_change)
        else:
            logger.warning("Unknown entity_type '%s' for %s", entity_type, entity_id)
            return
        logger.info("Created %s FSM for entity %s", entity_type, entity_id)

    def load_definitions(self, fsm_defs: list[dict]) -> None:
        """Bulk-create FSMs from generation output (fsm_bb_init_node)."""
        for fsm_def in fsm_defs:
            self.create_instance(fsm_def["entity_id"], fsm_def["entity_type"])

    # ── Event dispatch ────────────────────────────────────────────────────────

    def trigger(self, entity_id: str, event: str) -> bool:
        """Fire a named event on the entity's FSM. Returns True on success."""
        fsm = self.instances.get(entity_id)
        if not fsm:
            logger.warning("FSM not found for entity '%s' (event=%s)", entity_id, event)
            return False
        try:
            fsm.send(event)
            return True
        except Exception as exc:
            # Invalid transition for current state — silently ignore + log
            logger.debug("FSM transition skipped: %s.%s → %s", entity_id, event, exc)
            return False

    def get_state(self, entity_id: str) -> str | None:
        fsm = self.instances.get(entity_id)
        return _get_state_id(fsm) if fsm else None

    # ── Blackboard sync (called every BT pre_tick) ────────────────────────────

    def sync_to_blackboard(self) -> None:
        for entity_id, fsm in self.instances.items():
            key = f"entities/{entity_id}/fsm_state"
            # Register key on first encounter
            if key not in self._bb_attempted:
                self._bb_attempted.add(key)
                try:
                    self._bb.register_key(key=key, access=py_trees.common.Access.WRITE)
                    self._bb_writable.add(key)
                except AttributeError:
                    logger.warning("FSMManager: cannot claim WRITE for '%s'", key)
            if key not in self._bb_writable:
                continue
            try:
                self._bb.set(key, _get_state_id(fsm))
            except Exception as exc:
                logger.warning("FSMManager: set('%s') failed: %s", key, exc)

    # ── History / diagnostics ─────────────────────────────────────────────────

    def get_transition_history(self, entity_id: str | None = None) -> list[dict]:
        if entity_id:
            return [r for r in self._history if r["entity_id"] == entity_id]
        return list(self._history)

    def clear_instances(self) -> None:
        self.instances.clear()
        logger.info("All FSM instances cleared")

    # ── Internal callback ─────────────────────────────────────────────────────

    def _on_state_change(self, entity_id: str, new_state: str, event: str) -> None:
        record = {
            "entity_id": entity_id,
            "to": new_state,
            "trigger": event,
            "timestamp": time.time(),
        }
        self._history.append(record)
        logger.info("FSM [%s] → %s (trigger=%s)", entity_id, new_state, event)
