"""Generation pipeline progress publisher via Zenoh.

Uses ``contextvars`` so concurrent generation requests each publish
to their own task_id without race conditions.

Usage in pipeline nodes::

    from app.generation.graph.progress import publish_step

    publish_step("task_planner", "started", message="LLM 分解任务中…")
    # ... do work ...
    publish_step("task_planner", "completed", duration_ms=1234, message="分解为 5 个子任务")
"""

from __future__ import annotations

import contextvars
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.zenoh_bridge import ZenohBridge

logger = logging.getLogger(__name__)

_bridge_var: contextvars.ContextVar[ZenohBridge | None] = contextvars.ContextVar(
    "gen_progress_bridge", default=None,
)
_task_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "gen_progress_task_id", default="unknown",
)


def set_context(bridge: ZenohBridge, task_id: str) -> contextvars.Token:
    """Call once at the start of a generation run (inside _run_generation)."""
    _bridge_var.set(bridge)
    return _task_id_var.set(task_id)


def publish_step(step: str, status: str, **kwargs: Any) -> None:
    """Publish a progress event for the current generation run.

    Parameters match ``ZenohBridge.publish_progress`` kwargs:
      - message: str — human-readable description
      - duration_ms: int — elapsed time for this step
      - iteration: int — current iteration (for bt_builder/validator loop)
      - max_iterations: int — max allowed iterations
      - node: str — alias for step (frontend accepts both)
    """
    bridge = _bridge_var.get(None)
    task_id = _task_id_var.get("unknown")
    if bridge is None:
        logger.debug("progress.publish_step called without bridge context: %s/%s", step, status)
        return
    try:
        bridge.publish_progress(task_id, step, status, **kwargs)
    except Exception:
        logger.warning("Failed to publish progress: step=%s status=%s", step, status, exc_info=True)
