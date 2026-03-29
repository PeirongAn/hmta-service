"""T8 — Scenario compiler tests."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.build.compiler import compile_scenario, validate_package
from app.capability.hypergraph import HEdge, HNode, HyperGraph


@pytest.fixture
def graph() -> HyperGraph:
    g = HyperGraph()
    g.add_node(HNode(id="robot_A", kind="entity", attrs={"type": "robot", "mode": "autonomous"}))
    g.add_node(HNode(id="move", kind="capability"))
    g.add_node(HNode(id="task_1", kind="task"))
    g.add_edge(HEdge(id="hc_A_move", kind="has_capability",
                      nodes=frozenset(["robot_A", "move"]), weight=0.9))
    g.add_edge(HEdge(id="req_t1_move", kind="requires",
                      nodes=frozenset(["task_1", "move"]), weight=1.0))
    return g


@pytest.fixture
def build_dir(tmp_path: Path) -> Path:
    return tmp_path / "build"


class TestCompileScenario:
    def test_full_compile(self, graph: HyperGraph, build_dir: Path):
        pkg = compile_scenario(
            scenario_name="test_scenario",
            behavior_tree={"tree_id": "bt1", "nodes": {}},
            fsm_definitions=[{"entity_id": "robot_A", "states": []}],
            blackboard_init={"/entities/robot_A/status": "idle"},
            capability_graph=graph,
            output_dir=build_dir,
        )
        assert pkg.exists()
        errors = validate_package(pkg)
        assert errors == [], f"Validation errors: {errors}"

    def test_manifest_content(self, graph: HyperGraph, build_dir: Path):
        pkg = compile_scenario(
            scenario_name="test_scenario",
            behavior_tree={"tree_id": "bt1", "nodes": {}},
            fsm_definitions=[],
            blackboard_init={},
            capability_graph=graph,
            output_dir=build_dir,
        )
        with open(pkg / "manifest.json") as f:
            manifest = json.load(f)
        assert manifest["scenario_name"] == "test_scenario"
        assert manifest["version"] == "1.0.0"
        assert "build_time" in manifest

    def test_allocation_rules_cover_tasks(self, graph: HyperGraph, build_dir: Path):
        pkg = compile_scenario(
            scenario_name="test_scenario",
            behavior_tree={"tree_id": "bt1", "nodes": {}},
            fsm_definitions=[],
            blackboard_init={},
            capability_graph=graph,
            output_dir=build_dir,
        )
        with open(pkg / "artifacts" / "allocation_rules.json") as f:
            rules = json.load(f)
        assert "task_1" in rules["ranked_candidates"]

    def test_command_mappings(self, graph: HyperGraph, build_dir: Path):
        pkg = compile_scenario(
            scenario_name="test_scenario",
            behavior_tree={"tree_id": "bt1", "nodes": {}},
            fsm_definitions=[],
            blackboard_init={},
            capability_graph=graph,
            output_dir=build_dir,
        )
        with open(pkg / "artifacts" / "command_mappings.json") as f:
            mappings = json.load(f)
        assert "robot_A" in mappings


class TestValidation:
    def test_missing_file_detected(self, tmp_path: Path):
        pkg = tmp_path / "bad_pkg"
        pkg.mkdir()
        errors = validate_package(pkg)
        assert len(errors) > 0


class TestEmptyGraph:
    def test_compile_with_empty_graph(self, build_dir: Path):
        g = HyperGraph()
        pkg = compile_scenario(
            scenario_name="empty",
            behavior_tree={"tree_id": "bt1", "nodes": {}},
            fsm_definitions=[],
            blackboard_init={},
            capability_graph=g,
            output_dir=build_dir,
        )
        assert (pkg / "manifest.json").exists()
