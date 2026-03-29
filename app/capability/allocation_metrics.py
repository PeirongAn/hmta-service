"""Allocation quality metrics — computed from allocation trace + profiler data."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class AllocationQualityMetrics:
    coverage_ratio: float = 0.0
    avg_score: float = 0.0
    attention_utilization: float = 0.0
    reallocation_count: int = 0
    human_idle_ratio: float = 0.0
    robot_idle_ratio: float = 0.0
    mode_distribution: dict[str, int] = field(default_factory=dict)
    constraint_violations: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_allocation_quality(
    allocation_trace: list[dict],
    profiler_data: dict | None = None,
    attention_summary: dict[str, float] | None = None,
    attention_budget: float = 1.0,
) -> AllocationQualityMetrics:
    if not allocation_trace:
        return AllocationQualityMetrics()

    total = len(allocation_trace)
    assigned = sum(1 for t in allocation_trace if t.get("assigned"))
    scores = [
        t["robot_scores"][0]["total"]
        for t in allocation_trace
        if t.get("robot_scores")
    ]

    mode_dist: dict[str, int] = {}
    for t in allocation_trace:
        mode = t.get("collaboration", "unknown")
        mode_dist[mode] = mode_dist.get(mode, 0) + 1

    att_util = 0.0
    if attention_summary and attention_budget > 0:
        total_spent = sum(attention_summary.values())
        att_util = total_spent / attention_budget

    human_idle = 0.0
    robot_idle = 0.0
    realloc_count = 0
    violations = 0

    if profiler_data:
        subtask_data = profiler_data.get("subtask_data", {})
        total_duration = max(sum(s.get("duration_ms", 0) for s in subtask_data.values()), 1)

        human_time = sum(
            s.get("duration_ms", 0) for s in subtask_data.values()
            if s.get("human_supervisor")
        )
        human_idle = max(0, 1.0 - human_time / total_duration)

        active_robots = set()
        for s in subtask_data.values():
            for r in s.get("assigned", []):
                active_robots.add(r)
        robot_count = max(len(active_robots), 1)
        robot_active_time = sum(s.get("duration_ms", 0) for s in subtask_data.values())
        robot_idle = max(0, 1.0 - robot_active_time / (total_duration * robot_count))

        violations = sum(
            1 for s in subtask_data.values()
            if s.get("safety_events", 0) > 0
        )

        realloc_count = sum(
            1 for s in subtask_data.values()
            if s.get("reallocated", False)
        )

    return AllocationQualityMetrics(
        coverage_ratio=assigned / total if total > 0 else 0.0,
        avg_score=sum(scores) / len(scores) if scores else 0.0,
        attention_utilization=att_util,
        reallocation_count=realloc_count,
        human_idle_ratio=round(human_idle, 3),
        robot_idle_ratio=round(robot_idle, 3),
        mode_distribution=mode_dist,
        constraint_violations=violations,
    )
