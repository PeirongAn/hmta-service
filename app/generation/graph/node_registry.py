"""Declarative node registry for the LangGraph generation pipeline.

Instead of hard-coding nodes and edges in coordinator.py, this module
lets each node *register* itself.  The pipeline topology is driven by
``configs/pipeline.yaml`` so new agents can be added without touching
framework code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml
from langgraph.graph import END, StateGraph

from app.generation.graph.state import GenerationState

logger = logging.getLogger(__name__)

_PIPELINE_YAML = Path(__file__).resolve().parents[3] / "configs" / "pipeline.yaml"


@dataclass
class NodeSpec:
    """Metadata that describes a single pipeline node."""

    name: str
    handler: Callable[[GenerationState], dict]
    node_type: str = "llm"  # "llm" | "quantitative" | "rule" | "template"
    enabled: bool = True
    retry_target: str | None = None
    max_iterations: int = 3


class NodeRegistry:
    """Collects :class:`NodeSpec` instances and compiles them into a LangGraph."""

    def __init__(self) -> None:
        self._specs: dict[str, NodeSpec] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, spec: NodeSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"Node '{spec.name}' is already registered")
        self._specs[spec.name] = spec

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def apply_config(self, config_path: Path | str | None = None) -> None:
        """Override ``enabled`` / ``retry_target`` / ``max_iterations`` from
        a YAML pipeline config.  Unknown names in the YAML are silently
        skipped so that nodes can be pre-declared for future use.
        """
        path = Path(config_path) if config_path else _PIPELINE_YAML
        if not path.exists():
            logger.info("No pipeline config at %s — using defaults", path)
            return

        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

        entries: list[dict[str, Any]] = data.get("pipeline", [])
        config_order: dict[str, int] = {}

        for idx, entry in enumerate(entries):
            name = entry.get("name", "")
            config_order[name] = idx
            spec = self._specs.get(name)
            if spec is None:
                continue
            if "enabled" in entry:
                spec.enabled = bool(entry["enabled"])
            if "retry_target" in entry:
                spec.retry_target = entry["retry_target"] or None
            if "max_iterations" in entry:
                spec.max_iterations = int(entry["max_iterations"])

        self._config_order = config_order

    # ------------------------------------------------------------------
    # Graph compilation
    # ------------------------------------------------------------------

    def build_graph(self) -> Any:
        """Compile all *enabled* specs into a LangGraph ``CompiledGraph``."""
        self.apply_config()

        enabled = [s for s in self._specs.values() if s.enabled]
        if not enabled:
            raise RuntimeError("No enabled pipeline nodes — cannot build graph")

        order = getattr(self, "_config_order", {})
        enabled.sort(key=lambda s: order.get(s.name, 999))

        graph = StateGraph(GenerationState)

        for spec in enabled:
            graph.add_node(spec.name, spec.handler)

        graph.set_entry_point(enabled[0].name)

        for i, spec in enumerate(enabled):
            if spec.retry_target and spec.retry_target in {s.name for s in enabled}:
                graph.add_conditional_edges(
                    spec.name,
                    _make_router(spec),
                    {
                        "retry": spec.retry_target,
                        "proceed": enabled[i + 1].name if i + 1 < len(enabled) else END,
                        "give_up": END,
                    },
                )
            else:
                next_node = enabled[i + 1].name if i + 1 < len(enabled) else END
                graph.add_edge(spec.name, next_node)

        return graph.compile()

    def describe_pipeline(self, config_path: Path | str | None = None) -> dict[str, Any]:
        """Return YAML-ordered steps plus execution/retry edges (matches :meth:`build_graph`).

        Used by HTTP ``GET /api/v1/generation/pipeline`` for IDE import.
        """
        self.apply_config(config_path)
        path = Path(config_path) if config_path else _PIPELINE_YAML

        ordered_steps: list[dict[str, Any]] = []
        if path.exists():
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            entries: list[dict[str, Any]] = data.get("pipeline", [])
        else:
            entries = []

        for entry in entries:
            name = entry.get("name", "")
            if not name:
                continue
            spec = self._specs.get(name)
            if spec:
                ordered_steps.append(
                    {
                        "name": name,
                        "node_type": spec.node_type,
                        "enabled": spec.enabled,
                        "retry_target": spec.retry_target,
                        "max_iterations": spec.max_iterations,
                        "registered": True,
                    }
                )
            else:
                ordered_steps.append(
                    {
                        "name": name,
                        "node_type": "unknown",
                        "enabled": bool(entry.get("enabled", True)),
                        "retry_target": entry.get("retry_target"),
                        "max_iterations": int(entry.get("max_iterations", 3)),
                        "registered": False,
                    }
                )

        enabled = [s for s in self._specs.values() if s.enabled]
        order = getattr(self, "_config_order", {})
        enabled.sort(key=lambda s: order.get(s.name, 999))

        execution_edges: list[dict[str, str]] = []
        retry_edges: list[dict[str, str]] = []
        if not enabled:
            return {
                "ordered_steps": ordered_steps,
                "execution_edges": execution_edges,
                "retry_edges": retry_edges,
            }

        enabled_names = {s.name for s in enabled}
        for i in range(len(enabled) - 1):
            execution_edges.append(
                {
                    "from": enabled[i].name,
                    "to": enabled[i + 1].name,
                    "kind": "main",
                }
            )
        for s in enabled:
            if s.retry_target and s.retry_target in enabled_names:
                retry_edges.append(
                    {
                        "from": s.name,
                        "to": s.retry_target,
                        "kind": "retry_on_fail",
                    }
                )

        return {
            "ordered_steps": ordered_steps,
            "execution_edges": execution_edges,
            "retry_edges": retry_edges,
        }


def _make_router(spec: NodeSpec) -> Callable[[GenerationState], str]:
    """Return an edge-routing function for a validator-like node."""

    def _router(state: GenerationState) -> str:
        report = state.get("validation_report") or {}
        if report.get("validation_result") == "PASSED":
            return "proceed"
        if state.get("iteration_count", 0) >= state.get(
            "max_iterations", spec.max_iterations
        ):
            logger.warning(
                "[%s] %s FAILED after %d iterations — giving up",
                state.get("task_id"),
                spec.name,
                state.get("iteration_count"),
            )
            return "give_up"
        return "retry"

    return _router


# ── Module-level singleton ────────────────────────────────────────────────────

_registry = NodeRegistry()


def get_registry() -> NodeRegistry:
    return _registry
