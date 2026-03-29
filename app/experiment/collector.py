"""PerformanceCollector — hooks into generation + execution to produce (f, x, P) records."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from app.capability.ontology import resolve_alias
from app.experiment.store import ExperimentRecord, ExperimentStore

if TYPE_CHECKING:
    from app.zenoh_bridge import ZenohBridge

logger = logging.getLogger(__name__)


# Collaboration mode → (continuous x, discrete index)
_MODE_MAP: dict[tuple[str, str], tuple[float, int]] = {
    ("task_based", "autonomous"):        (0.00, 1),
    ("task_based", "supervised_report"):  (0.15, 2),
    ("partner", "human_approve_execute"): (0.40, 3),
    ("partner", "human_plan_execute"):    (0.65, 4),
    ("proxy", "human_remote_control"):    (0.90, 5),
}

# Priority label → urgency float
_URGENCY_MAP: dict[str, float] = {
    "critical": 1.0,
    "urgent": 0.6,
    "normal": 0.3,
}


class PerformanceCollector:
    """Collects (f_i, x_i) at generation time, fills P_i at execution end."""

    def __init__(self, store: ExperimentStore, zenoh: "ZenohBridge | None" = None) -> None:
        self._store = store
        self._zenoh = zenoh
        # Pending records keyed by subtask_id — waiting for execution results
        self._pending: dict[str, ExperimentRecord] = {}
        # Track previous subtask for cognitive switch cost
        self._prev_subtask: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Phase 1: generation complete → extract (f_i, x_i)
    # ------------------------------------------------------------------

    def on_generation_complete(self, task_id: str, final_state: dict) -> None:
        task_plan = final_state.get("task_plan", {})
        subtasks = task_plan.get("subtasks", task_plan.get("phases", []))
        entities = final_state.get("entities", [])
        allocation_trace = final_state.get("allocation_trace", [])

        # ── Fallback: bt_template_builder path has no task_plan subtasks ──────
        # When the generic HMTA template is used the task_planner does not run,
        # so subtasks == [].  Synthesise one record per robot entity so that the
        # experiments table always receives at least a (f, x) skeleton that will
        # be completed with P_i at execution end.
        if not subtasks and entities:
            subtasks = [
                {
                    "task_id": f"{task_id}::{e['entity_id']}",
                    "id":      f"{task_id}::{e['entity_id']}",
                    "name": e.get("display_name", e["entity_id"]),
                    "required_capabilities": [
                        c if isinstance(c, str) else c.get("name", "")
                        for c in (e.get("structured_capabilities") or e.get("capabilities") or [])
                    ],
                    "interaction": {},
                    "assigned_entity_ids": [e["entity_id"]],
                    "priority": "normal",
                    "_synthetic": True,   # marker for execution matching
                }
                for e in entities
                if e.get("entity_type", "") != "human"
                   and e.get("category", "") not in ("ENTITY_HUMAN", "human")
            ]
            logger.info(
                "[Collector] bt_template_builder path: synthesised %d entity records for task %s",
                len(subtasks), task_id,
            )
        # ─────────────────────────────────────────────────────────────────────

        trace_by_subtask = {
            t.get("subtask_id"): t for t in allocation_trace
        }

        prev_subtask = None
        for subtask in subtasks:
            stid = subtask.get("task_id") or subtask.get("id", "")
            trace = trace_by_subtask.get(stid, {})

            features = self._extract_task_features(subtask, final_state.get("environment", {}))
            switch_cost = self._compute_cognitive_switch_cost(subtask, prev_subtask)
            x_val, mode_idx = self._compute_human_involvement(subtask.get("interaction", {}))

            # Robot snapshot
            assigned = subtask.get("assigned_entity_ids", [])
            robot_id = assigned[0] if assigned else ""
            robot_ent = next((e for e in entities if e.get("entity_id") == robot_id), {})

            # Operator snapshot
            supervisor = (subtask.get("interaction") or {}).get("human_supervisor")
            op_ent = next((e for e in entities if e.get("entity_id") == supervisor), {}) if supervisor else {}
            cog_profile = op_ent.get("cognitive_profile", {})

            required_caps = [
                resolve_alias(r if isinstance(r, str) else r.get("name", ""))
                for r in subtask.get("required_capabilities", [])
            ]

            record = ExperimentRecord(
                task_id=task_id,
                subtask_id=stid,
                timestamp=time.time(),
                # f_i
                complexity=features["complexity"],
                urgency=features["urgency"],
                risk=features["risk"],
                ambiguity=features["ambiguity"],
                time_pressure=features["time_pressure"],
                needs_human_input=features["needs_human_input"],
                cognitive_switch_cost=switch_cost,
                # x_i
                collaboration_mode=(subtask.get("interaction") or {}).get("collaboration", "task_based"),
                collaboration_mode_idx=mode_idx,
                bt_pattern=(subtask.get("interaction") or {}).get("bt_pattern", "autonomous"),
                human_involvement=x_val,
                human_supervisor=supervisor,
                assigned_robot=robot_id,
                # operator snapshot
                operator_cognitive_load=op_ent.get("cognitive_load", 0.0),
                operator_fatigue=op_ent.get("fatigue_level", 0.0),
                operator_task_count=op_ent.get("current_task_count", 0),
                operator_devices_online=len([
                    d for d in op_ent.get("devices", [])
                    if d.get("status", "online") == "online"
                ]),
                # robot snapshot
                robot_battery=robot_ent.get("battery", 1.0),
                robot_distance_to_target=subtask.get("allocation_score", 0.0),
                robot_proficiency=float(
                    robot_ent.get("structured_capabilities", [{}])[0].get("proficiency", 1.0)
                    if robot_ent.get("structured_capabilities") else 1.0
                ),
                # capability context
                required_capabilities=required_caps,
                primary_capability=required_caps[0] if required_caps else "",
            )

            self._pending[stid] = record
            prev_subtask = subtask

        logger.info(
            "[Collector] generation complete for %s: %d subtask records pending",
            task_id, len(subtasks),
        )

    # ------------------------------------------------------------------
    # Phase 2: execution complete → fill P_i and persist
    # ------------------------------------------------------------------

    def on_execution_complete(
        self,
        task_id: str,
        profiler_data: dict,
        bb_snapshot: dict,
    ) -> list[ExperimentRecord]:
        """Fill performance metrics and save all pending records for *task_id*."""
        completed: list[ExperimentRecord] = []
        subtask_profiles = profiler_data.get("subtasks", {})

        # Build a secondary index: entity_id → best profiler profile.
        # Profiler keys by node_name, so subtask_id direct lookup usually misses.
        # We fall back to entity_id matching so that bt_template_builder records
        # (and LLM records where IDs differ) still receive real performance data.
        entity_to_profile: dict[str, dict] = {}
        for prof in subtask_profiles.values():
            eid = prof.get("entity_id", "")
            if eid and eid not in entity_to_profile:
                entity_to_profile[eid] = prof

        for stid, record in list(self._pending.items()):
            if record.task_id != task_id:
                continue

            # Primary lookup: exact subtask_id match (LLM path with aligned IDs)
            profile = subtask_profiles.get(stid, {})
            # Secondary lookup: entity_id match (bt_template path + LLM with mismatched IDs)
            if not profile and record.assigned_robot:
                profile = entity_to_profile.get(record.assigned_robot, {})

            record.outcome_success = profile.get("status") == "SUCCESS"
            record.actual_duration_ms = float(profile.get("duration_ms", 0.0))
            record.expected_duration_ms = profile.get("expected_duration_ms")
            record.human_response_time_ms = profile.get("human_response_time_ms")
            record.safety_events = int(profile.get("safety_events", 0))

            record.performance_obj = self._compute_performance_obj(record)
            record.safety_score = self._compute_safety_score(record)
            record.battery_consumed = float(profile.get("battery_consumed", 0.0))
            record.resource_feasible = record.battery_consumed < 0.5

            self._store.save(record)
            completed.append(record)
            del self._pending[stid]

        # For pending records without profiler data, save with defaults
        for stid in [s for s, r in self._pending.items() if r.task_id == task_id]:
            record = self._pending.pop(stid)
            record.performance_obj = self._compute_performance_obj(record)
            record.safety_score = self._compute_safety_score(record)
            self._store.save(record)
            completed.append(record)

        if self._zenoh and completed:
            self._zenoh.publish(
                f"zho/allocation/{task_id}/feedback",
                {"task_id": task_id, "record_count": len(completed)},
            )

        logger.info(
            "[Collector] execution complete for %s: saved %d records (total in DB: %d)",
            task_id, len(completed), self._store.count(),
        )
        return completed

    # ------------------------------------------------------------------
    # Feature extraction helpers
    # ------------------------------------------------------------------

    def _extract_task_features(self, subtask: dict, environment: dict) -> dict:
        required_caps = subtask.get("required_capabilities", [])
        priority = subtask.get("priority", "normal")

        complexity = min(len(required_caps) / 3.0, 1.0)
        urgency = _URGENCY_MAP.get(priority, 0.3)
        risk = float(subtask.get("risk", environment.get("risk_level", 0.0)))
        ambiguity = float(subtask.get("ambiguity", environment.get("ambiguity", 0.0)))
        time_pressure = 1.0 if subtask.get("deadline") else 0.0
        needs_human = bool(subtask.get("interaction", {}).get("human_supervisor"))

        return {
            "complexity": complexity,
            "urgency": urgency,
            "risk": risk,
            "ambiguity": ambiguity,
            "time_pressure": time_pressure,
            "needs_human_input": needs_human,
        }

    def _compute_cognitive_switch_cost(
        self, current: dict, prev: dict | None,
    ) -> float:
        if prev is None:
            return 0.0

        cur_caps = set(
            resolve_alias(r if isinstance(r, str) else r.get("name", ""))
            for r in current.get("required_capabilities", [])
        )
        prev_caps = set(
            resolve_alias(r if isinstance(r, str) else r.get("name", ""))
            for r in prev.get("required_capabilities", [])
        )

        if not cur_caps and not prev_caps:
            return 0.0
        jaccard = 1.0 - len(cur_caps & prev_caps) / max(len(cur_caps | prev_caps), 1)

        cur_mode = (current.get("interaction") or {}).get("collaboration", "task_based")
        prev_mode = (prev.get("interaction") or {}).get("collaboration", "task_based")
        mode_switch = 0.3 if cur_mode != prev_mode else 0.0

        return min(jaccard * 0.7 + mode_switch, 1.0)

    def _compute_human_involvement(self, interaction: dict) -> tuple[float, int]:
        collab = interaction.get("collaboration", "task_based")
        pattern = interaction.get("bt_pattern", "autonomous")
        return _MODE_MAP.get((collab, pattern), (0.0, 1))

    def _compute_performance_obj(self, record: ExperimentRecord) -> float:
        success_rate = 1.0 if record.outcome_success else 0.0

        if record.expected_duration_ms and record.expected_duration_ms > 0:
            time_eff = min(record.expected_duration_ms / max(record.actual_duration_ms, 1.0), 1.0)
        elif record.actual_duration_ms > 0:
            time_eff = min(30000.0 / record.actual_duration_ms, 1.0)
        else:
            time_eff = 0.5

        human_load = record.operator_cognitive_load if record.human_involvement > 0 else 0.0

        return 0.40 * success_rate + 0.35 * time_eff + 0.25 * (1.0 - human_load)

    def _compute_safety_score(self, record: ExperimentRecord) -> float:
        if record.safety_events == 0:
            return 1.0
        return max(1.0 - record.safety_events * 0.2, 0.0)
