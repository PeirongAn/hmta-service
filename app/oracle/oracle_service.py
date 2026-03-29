"""OracleService — judges robot/human capabilities against ground truth.

Pure observer: produces and publishes ``OracleJudgment`` facts.  Does NOT
update ``CapabilityRegistry`` directly — downstream consumers (performance
panel, capability feedback service) decide whether/how to apply updates.

Oracle sources:
  - UE ground truth positions  → automatic detection validation
  - Human operator rejection   → GoalConfirmationGate false-positive signal
  - UE NavMesh                 → waypoint reachability (future)
  - Mission conclusion         → task-level success/failure judgment
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from app.oracle.ground_truth_store import GroundTruthStore
from app.oracle.types import OracleJudgment

if TYPE_CHECKING:
    from app.experiment.proficiency_store import ProficiencyStore
    from app.zenoh_bridge import ZenohBridge

logger = logging.getLogger(__name__)

# Intents whose success is defined by human confirmation of a robot detection.
# "Find the bomb" → only success if robot detects + human confirms.
# NOTE: "scan" is intentionally excluded — a zone-coverage scan succeeds when
# all tasks complete, not when a human confirms a specific detection.
_CONFIRMATION_INTENTS: frozenset[str] = frozenset({
    "detect", "search", "find", "identify",
})

# Intents whose success is defined by all assigned tasks reaching "completed".
# Includes scan/surveil/observe — zone coverage missions succeed on task completion.
_COMPLETION_INTENTS: frozenset[str] = frozenset({
    "navigate", "move", "patrol", "escort", "deliver", "transport", "go", "travel",
    "scan", "surveil", "observe", "monitor",
})

# Map well-known success_condition keys to their canonical intent.
_CONDITION_KEY_TO_INTENT: dict[str, str] = {
    "bomb_detected":   "detect",
    "threat_detected": "detect",
    "target_found":    "search",
    "person_found":    "search",
}


def _safe_resolve(intent: str) -> str:
    try:
        from app.capability.ontology import resolve_alias
        return resolve_alias(intent)
    except Exception:
        return intent


class OracleService:
    """Stateful oracle that accumulates judgments during a mission."""

    def __init__(
        self,
        zenoh: "ZenohBridge",
        ground_truth: GroundTruthStore,
        config: dict,
        store: "ProficiencyStore | None" = None,
    ) -> None:
        self._zenoh = zenoh
        self._ground_truth = ground_truth
        self._config = config
        self._store = store
        self._task_id: str = ""
        self._judgments: list[OracleJudgment] = []

    # ── Mission lifecycle ─────────────────────────────────────────────────

    def set_task_id(self, task_id: str) -> None:
        self._task_id = task_id

    def reset(self) -> None:
        """Clear state for a new mission."""
        self._judgments.clear()
        self._ground_truth.clear()
        self._task_id = ""

    def publish_mission_goal(
        self,
        objective: str,
        mission_goal: dict | None,
        task_count: int = 0,
        entities: list[str] | None = None,
    ) -> None:
        """Publish the parsed mission goal so the Oracle panel can display it immediately.

        Sent as a ``judgment_type="mission_goal_parsed"`` record through the
        same ``zho/oracle/capability_judgments`` channel.
        """
        judgment = OracleJudgment(
            entity_id="system",
            capability_id="mission",
            entity_type="system",
            judgment_type="mission_goal_parsed",
            outcome="info",
            source="goal_extractor",
            task_id=self._task_id,
            confidence=1.0,
            details={
                "objective": objective,
                "mission_goal": mission_goal or {},
                "task_count": task_count,
                "entities": entities or [],
            },
        )
        self._record_and_publish(judgment)
        logger.info(
            "[OracleService] mission_goal_parsed published: objective=%s task_count=%d",
            objective[:60], task_count,
        )

    # ── Ground truth ingestion ────────────────────────────────────────────

    def on_ground_truth_received(self, data: dict) -> None:
        """Handle ``zho/sim/ground_truth`` payload from UE."""
        targets = data.get("targets", [])
        if targets:
            self._ground_truth.register_targets(targets)

    # ── Detection judgment ────────────────────────────────────────────────

    def judge_detection(
        self,
        entity_id: str,
        detection_position: dict[str, float],
        detection_data: dict[str, Any] | None = None,
    ) -> OracleJudgment | None:
        """Judge a robot detection event against ground truth.

        Called from ``BlackboardSync`` when ``bomb_detected`` fires.
        If no ground truth is registered, returns ``None`` (cannot judge
        without reference data).
        """
        if self._ground_truth.target_count == 0:
            logger.debug(
                "[OracleService] No ground truth registered — skipping "
                "automatic detection judgment for %s",
                entity_id,
            )
            return None

        oracle_cfg = self._config.get("oracle", {})
        threshold = float(oracle_cfg.get("detection_match_threshold_cm", 200.0))
        matched = self._ground_truth.match_detection(detection_position, threshold)

        if matched:
            judgment = OracleJudgment(
                entity_id=entity_id,
                capability_id=_safe_resolve("detect"),
                entity_type="robot",
                judgment_type="detection_accuracy",
                outcome="true_positive",
                source="ue_ground_truth",
                task_id=self._task_id,
                details={
                    "matched_target_id": matched.target_id,
                    "matched_target_type": matched.target_type,
                    "detection_position": detection_position,
                    "target_position": matched.position,
                },
            )
        else:
            judgment = OracleJudgment(
                entity_id=entity_id,
                capability_id=_safe_resolve("detect"),
                entity_type="robot",
                judgment_type="detection_accuracy",
                outcome="false_positive",
                source="ue_ground_truth",
                task_id=self._task_id,
                details={
                    "detection_position": detection_position,
                    "nearest_target_count": self._ground_truth.target_count,
                },
            )

        self._record_and_publish(judgment)
        return judgment

    # ── Human oracle (GoalConfirmationGate) ───────────────────────────────

    def on_goal_rejected(
        self,
        detecting_entity_id: str,
        condition_key: str,
        goal_data: dict | None = None,
    ) -> OracleJudgment:
        """Human operator rejected the detection — robot made a false positive.

        Called from ``GoalConfirmationGate._handle_rejection()``.
        """
        judgment = OracleJudgment(
            entity_id=detecting_entity_id,
            capability_id=_safe_resolve("detect"),
            entity_type="robot",
            judgment_type="detection_accuracy",
            outcome="false_positive",
            source="human_operator",
            task_id=self._task_id,
            details={
                "condition_key": condition_key,
                "goal_data": goal_data or {},
            },
        )
        self._record_and_publish(judgment)
        return judgment

    def on_goal_confirmed(
        self,
        detecting_entity_id: str,
        condition_key: str,
    ) -> OracleJudgment:
        """Human operator confirmed the detection — robot was correct.

        Called from ``GoalConfirmationGate`` on successful confirmation.
        """
        judgment = OracleJudgment(
            entity_id=detecting_entity_id,
            capability_id=_safe_resolve("detect"),
            entity_type="robot",
            judgment_type="detection_accuracy",
            outcome="true_positive",
            source="human_operator",
            task_id=self._task_id,
            details={"condition_key": condition_key},
        )
        self._record_and_publish(judgment)
        return judgment

    # ── Task-level outcome judgment ───────────────────────────────────────

    def judge_task_outcome(
        self,
        mission_goal: dict | None,
        task_queue: list[dict],
        end_reason: str,
        goal_confirmed: bool,
    ) -> OracleJudgment:
        """Judge the overall task success/failure at mission conclusion.

        Called from ``ExecutionEngine._publish_mission_summary()`` once, when
        the mission ends.  The success/failure definition depends on task
        intent:

        - Detection/search intents  → success **requires** human confirmation
          of the robot's detection (``goal_confirmed=True``).  Ending without
          confirmation is a task failure (target not found / all false
          positives).
        - Navigation/movement intents → success requires all tasks to have
          reached ``status="completed"``.
        - Unknown intent → falls back to ``end_reason == "mission_goal_met"``
          or ``goal_confirmed``.

        Produces and publishes an ``OracleJudgment`` with
        ``judgment_type="task_outcome"`` and ``outcome`` in
        ``{"success", "failure", "timeout"}``.
        """
        intent = self._resolve_primary_intent(mission_goal, task_queue)
        completed_count = sum(1 for t in task_queue if t.get("status") == "completed")
        failed_count    = sum(1 for t in task_queue if t.get("status") == "failed")
        total           = len(task_queue)

        if intent in _CONFIRMATION_INTENTS:
            # Human must confirm the machine's detection for the task to succeed.
            if goal_confirmed:
                outcome = "success"
                source  = "human_confirmed"
            elif end_reason == "timeout":
                outcome = "timeout"
                source  = "system"
            else:
                # Mission ended (queue_completed or mission_goal_met without
                # confirmation) — target was never confirmed found.
                outcome = "failure"
                source  = "system"

        elif intent in _COMPLETION_INTENTS:
            # For movement tasks: all assignments must finish successfully.
            all_ok = total > 0 and completed_count == total
            outcome = "success" if all_ok else "failure"
            source  = "system"

        else:
            # Fallback for unknown intents.
            if goal_confirmed or end_reason == "mission_goal_met":
                outcome = "success"
                source  = "human_confirmed" if goal_confirmed else "system"
            else:
                outcome = "failure"
                source  = "system"

        # Representing entity: sole robot, or "team" for multi-entity missions.
        entities = [t.get("entity", "") for t in task_queue if t.get("entity")]
        unique_entities = list(dict.fromkeys(entities))  # preserve order, dedupe
        entity_id   = unique_entities[0] if len(unique_entities) == 1 else "team"
        entity_type = "robot" if entity_id != "team" else "team"

        judgment = OracleJudgment(
            entity_id=entity_id,
            capability_id=intent,
            entity_type=entity_type,
            judgment_type="task_outcome",
            outcome=outcome,
            source=source,
            task_id=self._task_id,
            confidence=1.0,
            details={
                "intent": intent,
                "end_reason": end_reason,
                "goal_confirmed": goal_confirmed,
                "task_count": total,
                "completed_count": completed_count,
                "failed_count": failed_count,
                "success_condition": (mission_goal or {}).get("success_condition"),
                "entities": unique_entities,
            },
        )
        self._record_and_publish(judgment)
        logger.info(
            "[OracleService] task_outcome judgment: intent=%s outcome=%s source=%s "
            "entity=%s task_id=%s",
            intent, outcome, source, entity_id, self._task_id,
        )
        return judgment

    def _resolve_primary_intent(
        self,
        mission_goal: dict | None,
        task_queue: list[dict],
    ) -> str:
        """Infer the dominant task intent for this mission.

        Priority:
        1. ``mission_goal.task_intent`` or ``mission_goal.intent`` (explicit)
        2. ``mission_goal.success_condition.key`` → well-known intent table
        3. Most common intent across tasks in the queue
        4. ``"unknown"`` fallback
        """
        if mission_goal:
            explicit = (
                mission_goal.get("task_intent")
                or mission_goal.get("intent")
                or ""
            )
            if explicit:
                return _safe_resolve(explicit.lower())

            cond_key = (mission_goal.get("success_condition") or {}).get("key", "")
            if cond_key in _CONDITION_KEY_TO_INTENT:
                return _CONDITION_KEY_TO_INTENT[cond_key]

        intents = [t.get("intent", "").lower() for t in task_queue if t.get("intent")]
        if intents:
            return Counter(intents).most_common(1)[0][0]

        return "unknown"

    # ── Aggregation queries ───────────────────────────────────────────────

    def get_entity_accuracy(
        self,
        entity_id: str,
        capability_id: str,
    ) -> dict[str, Any]:
        """Aggregate TP/FP counts for this mission.

        For cross-mission accuracy, use ``ProficiencyStore.get_oracle_accuracy()``.
        """
        cap = _safe_resolve(capability_id)
        relevant = [
            j for j in self._judgments
            if j.entity_id == entity_id and j.capability_id == cap
        ]
        tp = sum(1 for j in relevant if j.outcome == "true_positive")
        fp = sum(1 for j in relevant if j.outcome == "false_positive")
        total = tp + fp
        return {
            "entity_id": entity_id,
            "capability_id": cap,
            "true_positives": tp,
            "false_positives": fp,
            "total_judgments": total,
            "accuracy_rate": tp / total if total > 0 else None,
        }

    @property
    def judgments(self) -> list[OracleJudgment]:
        return list(self._judgments)

    # ── Internal ──────────────────────────────────────────────────────────

    def _record_and_publish(self, judgment: OracleJudgment) -> None:
        self._judgments.append(judgment)

        if self._store:
            try:
                self._store.save_oracle_judgment(judgment)
            except Exception:
                logger.exception(
                    "[OracleService] Failed to persist judgment %s",
                    judgment.judgment_id,
                )

        payload = {
            "task_id": self._task_id,
            "judgments": [asdict(judgment)],
        }
        try:
            self._zenoh.publish_oracle_judgments(payload)
        except Exception:
            logger.exception("[OracleService] Failed to publish judgment")

        logger.info(
            "[OracleService] %s judgment: entity=%s cap=%s outcome=%s source=%s",
            judgment.judgment_type,
            judgment.entity_id,
            judgment.capability_id,
            judgment.outcome,
            judgment.source,
        )
