"""Robot Finite State Machine — python-statemachine declarative definition."""

from __future__ import annotations

from statemachine import State, StateMachine


class RobotFSM(StateMachine):
    """
    Models the logical state of a robot execution unit.

    States:  idle → moving / scanning / target_marked / disarming
    Exceptional:  stuck / sensor_fault / battery_low / offline
    """

    # ── States ────────────────────────────────────────────────────────────────
    idle = State(initial=True)
    moving = State()
    scanning = State()
    target_marked = State()
    disarming = State()
    stuck = State()
    sensor_fault = State()
    battery_low = State()
    offline = State()

    # ── Normal transitions ────────────────────────────────────────────────────
    move_command = idle.to(moving)
    waypoint_reached = moving.to(idle)
    scan_command = idle.to(scanning)
    scan_complete = scanning.to(idle)
    explosive_detected = scanning.to(target_marked)
    disarm_command = target_marked.to(disarming)
    disarm_complete = disarming.to(idle)

    # ── Recovery ──────────────────────────────────────────────────────────────
    navigation_failed = moving.to(stuck)
    stuck_resolved = stuck.to(idle)
    sensor_repaired = sensor_fault.to(idle)
    battery_recovered = battery_low.to(idle)

    # ── Global exceptional transitions ────────────────────────────────────────
    sensor_failure = idle.to(sensor_fault) | moving.to(sensor_fault) | scanning.to(sensor_fault)
    battery_low_event = idle.to(battery_low) | moving.to(battery_low) | scanning.to(battery_low)
    comm_lost = (
        idle.to(offline)
        | moving.to(offline)
        | scanning.to(offline)
        | target_marked.to(offline)
        | disarming.to(offline)
        | stuck.to(offline)
        | battery_low.to(offline)
    )
    comm_restored = offline.to(idle)

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def on_enter_state(self, state: State, event: str | None = None) -> None:
        if self._sync_callback:
            self._sync_callback(self.entity_id, state.id, event or "")

    def __init__(self, entity_id: str, sync_callback=None):
        self.entity_id = entity_id
        self._sync_callback = sync_callback
        super().__init__()
