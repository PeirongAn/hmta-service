"""T9 — Runtime engine tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.build.compiler import compile_scenario
from app.capability.hypergraph import HEdge, HNode, HyperGraph
from app.runtime.artifact_loader import ArtifactLoader
from app.runtime.health_monitor import HealthMonitor
from app.runtime.ide_callback import IDECallback
from app.runtime.rule_reallocator import RuleReallocator


@pytest.fixture
def scenario_dir(tmp_path: Path) -> Path:
    g = HyperGraph()
    g.add_node(HNode(id="robot_A", kind="entity", attrs={"type": "robot"}))
    g.add_node(HNode(id="robot_B", kind="entity", attrs={"type": "robot"}))
    g.add_node(HNode(id="move", kind="capability"))
    g.add_node(HNode(id="task_1", kind="task"))
    g.add_edge(HEdge(id="hc_A_move", kind="has_capability",
                      nodes=frozenset(["robot_A", "move"]), weight=0.9))
    g.add_edge(HEdge(id="hc_B_move", kind="has_capability",
                      nodes=frozenset(["robot_B", "move"]), weight=0.6))
    g.add_edge(HEdge(id="req_t1_move", kind="requires",
                      nodes=frozenset(["task_1", "move"]), weight=1.0))

    return compile_scenario(
        scenario_name="runtime_test",
        behavior_tree={"tree_id": "bt1", "nodes": {}},
        fsm_definitions=[],
        blackboard_init={},
        capability_graph=g,
        output_dir=tmp_path / "build",
    )


class TestArtifactLoader:
    def test_load_success(self, scenario_dir: Path):
        loader = ArtifactLoader(scenario_dir)
        loader.load()
        assert loader.manifest["scenario_name"] == "runtime_test"
        assert loader.behavior_tree["tree_id"] == "bt1"

    def test_load_missing_dir(self, tmp_path: Path):
        loader = ArtifactLoader(tmp_path / "nonexistent")
        with pytest.raises(FileNotFoundError):
            loader.load()


class TestRuleReallocator:
    def test_offline_replacement(self, scenario_dir: Path):
        loader = ArtifactLoader(scenario_dir)
        loader.load()
        r = RuleReallocator(loader.allocation_rules, loader.contingency_plans)
        result = r.on_entity_offline("robot_A")
        if result:
            assert result["replacement"] != "robot_A"

    def test_all_offline_triggers_callback(self, scenario_dir: Path):
        loader = ArtifactLoader(scenario_dir)
        loader.load()
        r = RuleReallocator(loader.allocation_rules, loader.contingency_plans)
        r.on_entity_offline("robot_A")
        r.on_entity_offline("robot_B")
        assert r.needs_ide_callback() is True

    def test_entity_back_online(self, scenario_dir: Path):
        loader = ArtifactLoader(scenario_dir)
        loader.load()
        r = RuleReallocator(loader.allocation_rules, loader.contingency_plans)
        r.on_entity_offline("robot_A")
        r.on_entity_online("robot_A")
        assert r.needs_ide_callback() is False


class TestHealthMonitor:
    def test_heartbeat_and_check(self):
        import time
        offline: list[str] = []
        m = HealthMonitor(timeout_sec=0.01, on_offline=lambda eid: offline.append(eid))
        m.heartbeat("robot_A")
        time.sleep(0.05)
        newly = m.check()
        assert "robot_A" in newly

    def test_no_timeout_when_alive(self):
        offline: list[str] = []
        m = HealthMonitor(timeout_sec=9999.0, on_offline=lambda eid: offline.append(eid))
        m.heartbeat("robot_A")
        m.check()
        assert len(offline) == 0


class TestIDECallback:
    def test_no_transport(self):
        cb = IDECallback()
        result = cb.request_replan("test", {}, ["robot_A"])
        assert result is None
