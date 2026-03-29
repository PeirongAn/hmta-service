"""Experiment controller — manages experiment plans and provides per-subtask overrides."""

from __future__ import annotations

import logging
import time
import uuid
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class TrialStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class ExperimentTrial(BaseModel):
    """Single trial: force a specific allocation for matching subtasks."""

    trial_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8])
    subtask_match: dict[str, Any] = Field(default_factory=dict)
    forced_collaboration: str | None = None
    forced_bt_pattern: str | None = None
    forced_human_involvement: float | None = None
    forced_robot: str | None = None
    forced_human: str | None = None
    description: str = ""
    status: TrialStatus = TrialStatus.PENDING
    started_at: float | None = None
    completed_at: float | None = None


class ExperimentPlan(BaseModel):
    """A named experiment plan containing multiple trials."""

    plan_id: str = Field(default_factory=lambda: uuid.uuid4().hex[:10])
    name: str = "unnamed"
    description: str = ""
    trials: list[ExperimentTrial] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)
    repeat_count: int = 1
    current_repeat: int = 0
    status: str = "idle"


COLLAB_TO_BT: dict[str, str] = {
    "task_based": "autonomous",
    "partner": "human_plan_execute",
    "proxy": "human_plan_execute",
}


class ExperimentController:
    """Manages the active experiment plan and provides per-subtask overrides."""

    def __init__(self) -> None:
        self._active_plan: ExperimentPlan | None = None
        self._trial_index: int = 0
        self._history: list[ExperimentPlan] = []

    @property
    def active_plan(self) -> ExperimentPlan | None:
        return self._active_plan

    def load_plan(self, plan: ExperimentPlan) -> None:
        if self._active_plan and self._active_plan.status == "running":
            self._active_plan.status = "aborted"
            self._history.append(self._active_plan)
        self._active_plan = plan
        self._trial_index = 0
        plan.status = "idle"
        logger.info("Loaded experiment plan '%s' with %d trials", plan.name, len(plan.trials))

    def start(self) -> None:
        if not self._active_plan:
            raise RuntimeError("No experiment plan loaded")
        self._active_plan.status = "running"
        self._active_plan.current_repeat = 0
        self._trial_index = 0
        logger.info("Experiment '%s' started", self._active_plan.name)

    def get_override_for_subtask(self, subtask: dict) -> dict[str, Any] | None:
        """Return forced allocation for a subtask if it matches a pending trial."""
        if not self._active_plan or self._active_plan.status != "running":
            return None

        stid = subtask.get("task_id") or subtask.get("id", "")
        required_caps = {
            r if isinstance(r, str) else r.get("name", "")
            for r in subtask.get("required_capabilities", [])
        }

        for trial in self._active_plan.trials:
            if trial.status != TrialStatus.PENDING:
                continue
            if not self._matches(trial.subtask_match, stid, required_caps, subtask):
                continue

            trial.status = TrialStatus.RUNNING
            trial.started_at = time.time()

            override: dict[str, Any] = {"trial_id": trial.trial_id}
            if trial.forced_collaboration:
                override["collaboration"] = trial.forced_collaboration
                override["bt_pattern"] = trial.forced_bt_pattern or COLLAB_TO_BT.get(
                    trial.forced_collaboration, "autonomous"
                )
            if trial.forced_bt_pattern:
                override["bt_pattern"] = trial.forced_bt_pattern
            if trial.forced_human_involvement is not None:
                override["human_involvement"] = trial.forced_human_involvement
            if trial.forced_robot:
                override["forced_robot"] = trial.forced_robot
            if trial.forced_human:
                override["forced_human"] = trial.forced_human

            logger.info(
                "Experiment override for subtask '%s': %s (trial %s)",
                stid, override, trial.trial_id,
            )
            return override

        return None

    def mark_trial_complete(self, trial_id: str, success: bool = True) -> None:
        if not self._active_plan:
            return
        for trial in self._active_plan.trials:
            if trial.trial_id == trial_id:
                trial.status = TrialStatus.COMPLETED if success else TrialStatus.FAILED
                trial.completed_at = time.time()
                break
        self._check_plan_completion()

    def get_status(self) -> dict[str, Any]:
        if not self._active_plan:
            return {"status": "no_plan", "trials_total": 0, "trials_completed": 0}
        p = self._active_plan
        completed = sum(1 for t in p.trials if t.status in (TrialStatus.COMPLETED, TrialStatus.FAILED))
        pending = sum(1 for t in p.trials if t.status == TrialStatus.PENDING)
        return {
            "plan_id": p.plan_id,
            "name": p.name,
            "status": p.status,
            "trials_total": len(p.trials),
            "trials_completed": completed,
            "trials_pending": pending,
            "trials_running": sum(1 for t in p.trials if t.status == TrialStatus.RUNNING),
            "current_repeat": p.current_repeat,
            "repeat_count": p.repeat_count,
            "trials": [t.model_dump() for t in p.trials],
        }

    def abort(self) -> None:
        if self._active_plan:
            self._active_plan.status = "aborted"
            for t in self._active_plan.trials:
                if t.status in (TrialStatus.PENDING, TrialStatus.RUNNING):
                    t.status = TrialStatus.SKIPPED
            self._history.append(self._active_plan)
            self._active_plan = None

    def _matches(
        self,
        match: dict[str, Any],
        subtask_id: str,
        required_caps: set[str],
        subtask: dict,
    ) -> bool:
        if not match:
            return True
        if "subtask_id" in match and match["subtask_id"] != subtask_id:
            return False
        if "capability" in match and match["capability"] not in required_caps:
            return False
        if "priority" in match and subtask.get("priority") != match["priority"]:
            return False
        return True

    def _check_plan_completion(self) -> None:
        if not self._active_plan:
            return
        all_done = all(
            t.status in (TrialStatus.COMPLETED, TrialStatus.FAILED, TrialStatus.SKIPPED)
            for t in self._active_plan.trials
        )
        if all_done:
            p = self._active_plan
            p.current_repeat += 1
            if p.current_repeat < p.repeat_count:
                for t in p.trials:
                    t.status = TrialStatus.PENDING
                    t.started_at = None
                    t.completed_at = None
                logger.info("Experiment repeat %d/%d", p.current_repeat + 1, p.repeat_count)
            else:
                p.status = "completed"
                self._history.append(p)
                logger.info("Experiment '%s' completed", p.name)

    # ------------------------------------------------------------------
    # Auto mode (Sprint 4: GP-based experiment suggestion)
    # ------------------------------------------------------------------

    def enable_auto_mode(
        self,
        learner: Any,
        safety_threshold: float = 0.95,
        auto_execute_safety_min: float = 0.9,
        zenoh: Any = None,
    ) -> dict[str, Any]:
        """Generate an experiment plan from GP suggestions.

        1. learner.suggest_next_experiment(safety_threshold)
        2. safety_probability >= auto_execute_safety_min → auto-execute
        3. safety_probability < auto_execute_safety_min → mark "needs_confirmation"
        """
        suggestion = learner.suggest_next_experiment(safety_threshold)

        safe_enough = suggestion.safety_probability >= auto_execute_safety_min
        status = "auto" if safe_enough else "needs_confirmation"

        trial = ExperimentTrial(
            subtask_match={},
            forced_collaboration=COLLAB_TO_BT.get(
                {1: "task_based", 2: "task_based", 3: "partner", 4: "partner", 5: "proxy"}.get(
                    suggestion.suggested_mode_idx, "task_based"
                ), "task_based"
            ),
            forced_human_involvement=suggestion.suggested_x,
            description=suggestion.rationale,
        )

        # Map mode_idx to collaboration
        mode_collab_map = {1: "task_based", 2: "task_based", 3: "partner", 4: "partner", 5: "proxy"}
        trial.forced_collaboration = mode_collab_map.get(suggestion.suggested_mode_idx, "task_based")

        plan = ExperimentPlan(
            name=f"auto_gp_{int(time.time())}",
            description=f"GP-suggested experiment (safety_prob={suggestion.safety_probability:.3f})",
            trials=[trial],
        )

        if safe_enough:
            self.load_plan(plan)
            self.start()
            logger.info("Auto mode: executing suggested experiment (safety_prob=%.3f)", suggestion.safety_probability)
        else:
            self._pending_suggestion = plan
            logger.warning(
                "Auto mode: suggestion needs confirmation (safety_prob=%.3f < %.3f)",
                suggestion.safety_probability, auto_execute_safety_min,
            )
            if zenoh:
                try:
                    zenoh.publish("zho/experiment/needs_confirmation", {
                        "suggestion": suggestion.model_dump(),
                        "plan": plan.model_dump(),
                    })
                except Exception:
                    logger.exception("Failed to publish confirmation request")

        return {
            "status": status,
            "suggestion": suggestion.model_dump(),
            "plan_id": plan.plan_id,
            "safety_probability": suggestion.safety_probability,
        }

    def confirm_pending(self) -> bool:
        """Confirm and start a pending auto-mode suggestion."""
        if hasattr(self, "_pending_suggestion") and self._pending_suggestion:
            self.load_plan(self._pending_suggestion)
            self.start()
            self._pending_suggestion = None
            return True
        return False
