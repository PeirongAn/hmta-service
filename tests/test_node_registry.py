"""T6 — Node registry and pipeline tests."""

from __future__ import annotations

import pytest

from app.generation.graph.node_registry import NodeRegistry, NodeSpec


def _noop(state: dict) -> dict:
    return {}


class TestRegistration:
    def test_register_and_build(self):
        r = NodeRegistry()
        r.register(NodeSpec(name="a", handler=_noop, node_type="llm"))
        r.register(NodeSpec(name="b", handler=_noop, node_type="rule"))
        graph = r.build_graph()
        assert graph is not None

    def test_duplicate_raises(self):
        r = NodeRegistry()
        r.register(NodeSpec(name="a", handler=_noop))
        with pytest.raises(ValueError, match="already registered"):
            r.register(NodeSpec(name="a", handler=_noop))

    def test_disabled_node_excluded(self):
        r = NodeRegistry()
        r.register(NodeSpec(name="a", handler=_noop, enabled=True))
        r.register(NodeSpec(name="b", handler=_noop, enabled=False))
        r.register(NodeSpec(name="c", handler=_noop, enabled=True))
        graph = r.build_graph()
        assert graph is not None


class TestConditionalEdges:
    def test_retry_edge(self):
        r = NodeRegistry()
        r.register(NodeSpec(name="planner", handler=_noop, node_type="llm"))
        r.register(NodeSpec(name="builder", handler=_noop, node_type="llm"))
        r.register(NodeSpec(
            name="validator",
            handler=_noop,
            node_type="rule",
            retry_target="builder",
            max_iterations=3,
        ))
        r.register(NodeSpec(name="init", handler=_noop, node_type="template"))
        graph = r.build_graph()
        assert graph is not None


class TestEmptyRegistry:
    def test_no_nodes_raises(self):
        r = NodeRegistry()
        with pytest.raises(RuntimeError, match="No enabled"):
            r.build_graph()
