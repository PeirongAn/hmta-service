"""LangGraph StateGraph coordinator — registry-driven pipeline.

All pipeline nodes register themselves via :func:`get_registry().register()`.
The actual topology is driven by ``configs/pipeline.yaml``.
"""

from __future__ import annotations

import logging

from app.capability.allocator import allocator_node
from app.generation.graph.bt_builder import bt_builder_node
from app.generation.graph.bt_template_builder import bt_template_builder_node
from app.generation.graph.constraint_validator import validator_node
from app.generation.graph.coverage_ensurer import coverage_ensurer_node
from app.generation.graph.fsm_bb_init import fsm_bb_init_node
from app.generation.graph.goal_extractor import goal_extractor_node
from app.generation.graph.node_registry import NodeSpec, get_registry
from app.generation.graph.task_planner import task_planner_node

logger = logging.getLogger(__name__)


# ── Register built-in nodes ──────────────────────────────────────────────────

def _register_builtins() -> None:
    """Register the core pipeline nodes once."""
    registry = get_registry()

    builtins = [
        NodeSpec(name="goal_extractor", handler=goal_extractor_node, node_type="llm"),
        NodeSpec(name="task_planner", handler=task_planner_node, node_type="llm"),
        NodeSpec(name="coverage_ensurer", handler=coverage_ensurer_node, node_type="rule"),
        NodeSpec(name="allocator", handler=allocator_node, node_type="quantitative"),
        NodeSpec(name="bt_builder", handler=bt_builder_node, node_type="llm"),
        NodeSpec(
            name="validator",
            handler=validator_node,
            node_type="rule",
            retry_target="bt_builder",
            max_iterations=3,
        ),
        NodeSpec(
            name="bt_template_builder",
            handler=bt_template_builder_node,
            node_type="template",
        ),
        NodeSpec(name="fsm_bb_init", handler=fsm_bb_init_node, node_type="template"),
    ]

    for spec in builtins:
        try:
            registry.register(spec)
        except ValueError:
            pass  # already registered (module re-import)


_register_builtins()


# ── Graph factory (lazy singleton) ───────────────────────────────────────────

_graph = None


def build_generation_graph():
    """Build and compile the generation pipeline from the registry."""
    return get_registry().build_graph()


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_generation_graph()
    return _graph
