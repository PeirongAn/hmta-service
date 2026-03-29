"""Profiler Publisher — accumulates BT node state transitions into Gantt data.

Publishes to ``zho/bt/profiler/gantt`` on every state change (not every tick)
so the frontend Gantt chart updates in real time.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import py_trees

logger = logging.getLogger(__name__)

# Map py_trees Status to Gantt status string
_STATUS_MAP: dict[str, str] = {
    "RUNNING": "running",
    "SUCCESS": "success",
    "FAILURE": "failure",
    "INVALID": "idle",
}


@dataclass
class _TaskRecord:
    node_id: str
    node_name: str
    entity_id: str
    intent: str
    started_at: float = 0.0
    ended_at: float | None = None
    status: str = "running"
    feedback: str = ""


@dataclass
class _LaneState:
    entity_id: str
    entity_type: str  # "machine" | "human"
    display_name: str
    tasks: list[_TaskRecord] = field(default_factory=list)


class ProfilerPublisher:
    """Tracks BT node transitions AND individual task_queue transitions,
    publishing structured Gantt data through Zenoh.

    For the generic HMTA template (EntityWorker-based), per-task bars are
    derived from the Blackboard task_queue rather than from the EntityWorker
    py_trees node (which stays RUNNING for its entire lifetime).

    Attach as a post-tick handler alongside SnapshotPublisher.
    """

    def __init__(self, zenoh_bridge, task_id: str = ""):
        self._zenoh = zenoh_bridge
        self._task_id = task_id
        self._start_time: float = 0.0
        self._lanes: dict[str, _LaneState] = {}
        self._active: dict[str, _TaskRecord] = {}  # node.name → record
        self._prev_status: dict[str, str] = {}      # node.name → last status
        self._dirty = False
        # Task-queue level tracking (generic HMTA mode)
        self._prev_task_status: dict[str, str] = {}   # task_id → last status
        self._task_records: dict[str, _TaskRecord] = {}  # task_id → record
        # Human intervention tracking per task
        self._prev_intervention_count: dict[str, int] = {}
        self._prev_intervention_ms: dict[str, float] = {}
        self._intervention_records: dict[str, _TaskRecord] = {}

    # ── Planning phase injection ─────────────────────────────────────────────

    def inject_planning_record(self, planning_ms: float, mission_started_at: float) -> None:
        """Add a completed planning-phase bar to the *system* lane.

        Called once from the engine after the generation pipeline finishes,
        so the Gantt chart shows how long planning took before execution.
        """
        if self._start_time == 0.0:
            self._start_time = mission_started_at

        entity_id = "system"
        if entity_id not in self._lanes:
            self._lanes[entity_id] = _LaneState(
                entity_id=entity_id,
                entity_type="machine",
                display_name="system",
            )

        record = _TaskRecord(
            node_id="planning-phase",
            node_name="任务规划",
            entity_id=entity_id,
            intent="task_planning",
            started_at=mission_started_at,
            ended_at=mission_started_at + planning_ms,
            status="success",
        )
        self._lanes[entity_id].tasks.append(record)
        self._dirty = True
        self._publish_gantt(mission_started_at + planning_ms)
        self._dirty = False
        logger.info("Profiler: injected planning record (%.1fs)", planning_ms / 1000)

    def inject_review_record(self, review_ms: float, started_at_ms: float) -> None:
        """Add a completed plan-review (operator approval wait) bar.

        Rendered in the ``operator`` human lane so it stands out from machine tasks.
        Called once from ExecutionEngine.start() right before the tick loop begins.
        """
        lane_id = "operator"
        if lane_id not in self._lanes:
            self._lanes[lane_id] = _LaneState(
                entity_id=lane_id,
                entity_type="human",
                display_name="operator 计划审批",
            )

        record = _TaskRecord(
            node_id="plan-review",
            node_name="计划审批",
            entity_id=lane_id,
            intent="plan_review",
            started_at=started_at_ms,
            ended_at=started_at_ms + review_ms,
            status="success",
        )
        self._lanes[lane_id].tasks.append(record)
        self._dirty = True
        self._publish_gantt(started_at_ms + review_ms)
        self._dirty = False
        logger.info("Profiler: injected plan-review record (%.1fs)", review_ms / 1000)

    def publish(self, tree: py_trees.trees.BehaviourTree) -> None:
        """Called after each tick.  Scans all nodes for state changes."""
        now = time.time() * 1000  # epoch ms

        if self._start_time == 0.0:
            self._start_time = now

        for node in tree.root.iterate():
            try:
                status_str = node.status.value
            except AttributeError:
                continue

            gantt_status = _STATUS_MAP.get(status_str, "idle")
            prev = self._prev_status.get(node.name)
            self._prev_status[node.name] = gantt_status

            if prev == gantt_status:
                continue  # no change

            # Transition detected
            if gantt_status == "running" and prev != "running":
                self._on_start(node, now)
            elif prev == "running" and gantt_status in ("success", "failure"):
                self._on_end(node, now, gantt_status)

        self._track_task_queue(now)
        self._track_human_review_sessions(now)

        if self._dirty:
            self._publish_gantt(now)
            self._dirty = False

    # ── Internal — node-level tracking ────────────────────────────────────────

    def _on_start(self, node: py_trees.behaviour.Behaviour, now: float) -> None:
        from app.execution.behaviours.command_action import CommandAction
        from app.execution.behaviours.entity_worker import EntityWorker
        from app.execution.behaviours.human_gate import HumanGate
        from app.execution.behaviours.human_monitor import HumanMonitor

        # EntityWorker / HumanMonitor stay RUNNING for their entire lifetime;
        # individual tasks inside them are tracked via _track_task_queue.
        if isinstance(node, (EntityWorker, HumanMonitor)):
            return

        entity_id = getattr(node, "entity_id", "") or "system"
        intent = getattr(node, "intent", "") or node.name

        entity_type = "machine"
        if isinstance(node, HumanGate):
            entity_type = "human"

        if entity_id not in self._lanes:
            self._lanes[entity_id] = _LaneState(
                entity_id=entity_id,
                entity_type=entity_type,
                display_name=entity_id,
            )

        record = _TaskRecord(
            node_id=f"py-{node.id.hex[:8]}",
            node_name=node.name,
            entity_id=entity_id,
            intent=intent,
            started_at=now,
        )
        self._active[node.name] = record
        self._lanes[entity_id].tasks.append(record)
        self._dirty = True
        logger.debug("Profiler: %s started (%s)", node.name, entity_id)

    def _on_end(self, node: py_trees.behaviour.Behaviour, now: float, status: str) -> None:
        record = self._active.pop(node.name, None)
        if record:
            record.ended_at = now
            record.status = status
            record.feedback = node.feedback_message or ""
            self._dirty = True
            logger.debug("Profiler: %s ended → %s (%.1fs)",
                         node.name, status, (now - record.started_at) / 1000)

    # ── Internal — task-queue level tracking (generic HMTA) ───────────────────

    def _track_task_queue(self, now: float) -> None:
        """Read BB task_queue and create per-task Gantt entries."""
        try:
            storage = py_trees.blackboard.Blackboard.storage
            queue = storage.get("/task_queue") or storage.get("task_queue")
            if not queue:
                return

            for task in queue:
                task_id = task.get("id", "")
                if not task_id:
                    continue

                # Human intervention tracking runs on every tick,
                # independent of whether the task status changed.
                self._track_interventions(task_id, task, now)

                status = task.get("status", "pending")
                prev = self._prev_task_status.get(task_id)
                if prev == status:
                    continue
                self._prev_task_status[task_id] = status

                entity_id = task.get("entity", "unknown")
                intent = task.get("intent", "")

                if status == "executing" and prev != "executing":
                    started_s = task.get("started_at")
                    started_ms = (started_s * 1000) if started_s else now

                    if entity_id not in self._lanes:
                        self._lanes[entity_id] = _LaneState(
                            entity_id=entity_id,
                            entity_type="machine",
                            display_name=entity_id,
                        )

                    record = _TaskRecord(
                        node_id=f"task-{task_id}",
                        node_name=task.get("description") or intent or task_id,
                        entity_id=entity_id,
                        intent=intent,
                        started_at=started_ms,
                    )
                    self._task_records[task_id] = record
                    self._lanes[entity_id].tasks.append(record)
                    self._dirty = True

                elif status in ("completed", "failed", "skipped"):
                    record = self._task_records.get(task_id)
                    if record:
                        ended_s = task.get("completed_at")
                        record.ended_at = (ended_s * 1000) if ended_s else now
                        record.status = "success" if status == "completed" else "failure"
                        parts: list[str] = []
                        if task.get("failure_reason"):
                            parts.append(task["failure_reason"])
                        hic = task.get("human_intervention_count", 0)
                        if hic:
                            him = task.get("human_intervention_ms", 0)
                            parts.append(f"人工介入 {hic}次 {him/1000:.1f}s")
                        record.feedback = " | ".join(parts)
                        self._dirty = True

                    self._close_intervention(task_id, now, status)
        except Exception:
            logger.debug("_track_task_queue failed", exc_info=True)

    # ── Human intervention tracking ───────────────────────────────────────────

    def _track_interventions(self, task_id: str, task: dict, now: float) -> None:
        """Detect human-fallback start/end by monitoring intervention counters.

        Intervention bars are placed in the **entity's own lane** so the
        operator can see exactly when each dog was taken over by a human.
        """
        hic = task.get("human_intervention_count", 0)
        him = task.get("human_intervention_ms", 0)
        prev_hic = self._prev_intervention_count.get(task_id, 0)
        prev_him = self._prev_intervention_ms.get(task_id, 0)
        self._prev_intervention_count[task_id] = hic
        self._prev_intervention_ms[task_id] = him

        if hic > prev_hic:
            self._close_intervention(task_id, now, "success")

            entity_id = task.get("entity", "unknown")
            if entity_id not in self._lanes:
                self._lanes[entity_id] = _LaneState(
                    entity_id=entity_id,
                    entity_type="machine",
                    display_name=entity_id,
                )

            reason = (task.get("human_intervention_reason", "")
                      or task.get("failure_reason", "")
                      or task.get("intent", ""))
            record = _TaskRecord(
                node_id=f"intervention-{task_id}-{hic}",
                node_name=f"人工介入 #{hic}",
                entity_id=entity_id,
                intent="human_intervention",
                started_at=now,
                feedback=reason,
            )
            self._intervention_records[task_id] = record
            self._lanes[entity_id].tasks.append(record)
            self._dirty = True
            logger.debug("Profiler: intervention #%d started for task %s on %s (reason: %s)",
                         hic, task_id, entity_id, reason)

        elif him > prev_him and task_id in self._intervention_records:
            record = self._intervention_records.pop(task_id)
            record.ended_at = now
            record.status = "success"
            self._dirty = True
            logger.debug("Profiler: intervention ended for task %s (%.1fs)",
                         task_id, (him - prev_him) / 1000)

    def _close_intervention(self, task_id: str, now: float, final_status: str) -> None:
        """Close an open intervention bar when the parent task terminates."""
        record = self._intervention_records.pop(task_id, None)
        if record:
            record.ended_at = now
            record.status = "failure" if final_status == "failed" else "success"
            self._dirty = True

    # ── Human review session tracking (GoalConfirmationGate via BB) ──────────

    def _track_human_review_sessions(self, now: float) -> None:
        """Read BB human_review_sessions and create/close Gantt bars.

        GoalConfirmationGate writes entries when it enters/leaves the confirming phase.
        Each entry maps to a bar in the operator's human lane.
        """
        try:
            storage = py_trees.blackboard.Blackboard.storage
            sessions: list[dict] | None = storage.get("human_review_sessions")
            if not sessions:
                return

            for session in sessions:
                sid = session.get("id", "")
                if not sid:
                    continue

                entity_id = session.get("entity_id", "operator")
                session_type = session.get("type", "review")
                label = session.get("label") or session_type
                started_s = session.get("started_at")
                ended_s = session.get("ended_at")
                status_str = session.get("status", "waiting")

                if started_s is None:
                    continue

                started_ms = started_s * 1000
                lane_id = f"{entity_id}:review"

                if sid not in self._active:
                    # New session — create bar
                    if lane_id not in self._lanes:
                        self._lanes[lane_id] = _LaneState(
                            entity_id=lane_id,
                            entity_type="human",
                            display_name=f"{entity_id} 审批等待",
                        )
                    record = _TaskRecord(
                        node_id=f"review-{sid}",
                        node_name=label,
                        entity_id=lane_id,
                        intent=session_type,
                        started_at=started_ms,
                    )
                    self._active[sid] = record
                    self._lanes[lane_id].tasks.append(record)
                    self._dirty = True
                    logger.debug("Profiler: review session started: %s (%s)", sid, label)

                record = self._active.get(sid)
                if record and ended_s is not None and record.ended_at is None:
                    record.ended_at = ended_s * 1000
                    record.status = (
                        "success" if status_str in ("success", "confirmed")
                        else "failure"
                    )
                    self._active.pop(sid, None)
                    self._dirty = True
                    logger.debug("Profiler: review session closed: %s → %s", sid, status_str)
        except Exception:
            logger.debug("_track_human_review_sessions failed", exc_info=True)

    def get_summary(self) -> dict:
        """Return a summary dict keyed by subtask/node name for the collector."""
        now = time.time() * 1000
        subtasks: dict[str, dict] = {}
        for lane in self._lanes.values():
            for t in lane.tasks:
                duration = (t.ended_at or now) - t.started_at
                subtasks[t.node_name] = {
                    "node_id": t.node_id,
                    "entity_id": t.entity_id,
                    "status": t.status.upper(),
                    "duration_ms": duration,
                    "started_at": t.started_at,
                    "ended_at": t.ended_at,
                }
        return {
            "task_id": self._task_id,
            "start_time": self._start_time,
            "total_duration_ms": max(now - self._start_time, 0) if self._start_time else 0,
            "subtasks": subtasks,
        }

    def _publish_gantt(self, now: float) -> None:
        total_ms = max(now - self._start_time, 1)

        lanes = []
        for lane in self._lanes.values():
            tasks = []
            for t in lane.tasks:
                duration = (t.ended_at or now) - t.started_at
                tasks.append({
                    "node_id": t.node_id,
                    "node_name": t.node_name,
                    "entity_id": t.entity_id,
                    "intent": t.intent,
                    "started_at": t.started_at,
                    "ended_at": t.ended_at,
                    "duration_ms": duration,
                    "status": t.status,
                    "feedback": t.feedback,
                })
            lanes.append({
                "entity_id": lane.entity_id,
                "entity_type": lane.entity_type,
                "display_name": lane.display_name,
                "tasks": tasks,
            })

        payload = {
            "task_id": self._task_id,
            "start_time": self._start_time,
            "total_duration_ms": total_ms,
            "lanes": lanes,
        }

        try:
            self._zenoh.publish_profiler_gantt(payload)
        except Exception:
            logger.exception("Failed to publish profiler gantt data")
