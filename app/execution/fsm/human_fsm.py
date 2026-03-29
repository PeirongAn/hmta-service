"""Human Finite State Machine — python-statemachine declarative definition."""

from __future__ import annotations

from statemachine import State, StateMachine


class HumanFSM(StateMachine):
    """
    Models the cognitive/task state of a human operator.

    Normal flow: idle → briefed → deciding → executing → reporting → idle
    Shortcut:    idle → deciding (approval_requested) → idle (approval_given)
    Exceptional: overloaded / fatigued / unresponsive
    """

    # ── States ────────────────────────────────────────────────────────────────
    idle = State(initial=True)
    briefed = State()
    deciding = State()
    executing = State()
    reporting = State()
    overloaded = State()
    fatigued = State()
    unresponsive = State()

    # ── Normal flow ───────────────────────────────────────────────────────────
    directive_received = idle.to(briefed)
    briefing_acknowledged = briefed.to(deciding)
    decision_made = deciding.to(executing)
    task_reported = executing.to(reporting)
    report_submitted = reporting.to(idle)

    # ── Approval shortcut ─────────────────────────────────────────────────────
    approval_requested = idle.to(deciding)
    approval_given = deciding.to(idle)

    # ── Global exceptions ──────────────────────────────────────────────────────
    overloaded_event = (
        idle.to(overloaded)
        | briefed.to(overloaded)
        | deciding.to(overloaded)
        | executing.to(overloaded)
    )
    fatigue_detected = idle.to(fatigued) | executing.to(fatigued)
    no_response = briefed.to(unresponsive) | deciding.to(unresponsive)
    recovered = unresponsive.to(idle) | overloaded.to(idle) | fatigued.to(idle)

    # ── Callbacks ─────────────────────────────────────────────────────────────

    def on_enter_state(self, state: State, event: str | None = None) -> None:
        if self._sync_callback:
            self._sync_callback(self.entity_id, state.id, event or "")

    def __init__(self, entity_id: str, sync_callback=None):
        self.entity_id = entity_id
        self._sync_callback = sync_callback
        super().__init__()
