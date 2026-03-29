"""LangGraph state for the post-mission feedback / proficiency-learning pipeline."""

from __future__ import annotations

from typing import TypedDict


class FeedbackState(TypedDict, total=False):
    task_id: str
    task_queue: list[dict]
    profiler_summary: dict
    entity_metrics: dict
    bottleneck_data: dict | None
    proficiency_proposals: list[dict] | None
    error: str | None
