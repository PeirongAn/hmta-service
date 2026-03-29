"""ScenarioCompiler — distill design-time artifacts into a Runtime package."""

from __future__ import annotations

import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.build.distiller import distill_allocation_rules
from app.capability.hypergraph import HyperGraph

logger = logging.getLogger(__name__)


def compile_scenario(
    *,
    scenario_name: str,
    behavior_tree: dict,
    fsm_definitions: list[dict],
    blackboard_init: dict,
    capability_graph: HyperGraph | dict,
    ontology_config: dict | None = None,
    utility_weights: dict | None = None,
    command_mappings: dict | None = None,
    performance_history: list[dict] | None = None,
    output_dir: Path | str = "build",
    version: str = "1.0.0",
) -> Path:
    """Compile a complete Runtime package.

    Returns the path to the generated scenario directory.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    pkg_name = f"scenario-{scenario_name}-{version}"
    root = Path(output_dir) / pkg_name

    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)

    artifacts_dir = root / "artifacts"
    artifacts_dir.mkdir()
    config_dir = root / "config"
    config_dir.mkdir()

    # 1. Freeze core artifacts
    _write_json(artifacts_dir / "behavior_tree.json", behavior_tree)
    _write_json(artifacts_dir / "fsm_definitions.json", fsm_definitions)
    _write_json(artifacts_dir / "blackboard_init.json", blackboard_init)

    # 2. Distill allocation rules
    graph_dict = capability_graph if isinstance(capability_graph, dict) else capability_graph.to_dict()
    graph = HyperGraph.from_dict(graph_dict) if isinstance(capability_graph, dict) else capability_graph

    allocation_rules = distill_allocation_rules(graph=graph, weights=utility_weights or {})
    _write_json(artifacts_dir / "allocation_rules.json", allocation_rules)

    # 3. Command mappings
    cmd_map = command_mappings or _derive_command_mappings(graph)
    _write_json(artifacts_dir / "command_mappings.json", cmd_map)

    # 4. Contingency plans
    contingency = _generate_contingency_plans(graph, allocation_rules)
    _write_json(artifacts_dir / "contingency_plans.json", contingency)

    # 5. Capability snapshot
    _write_json(artifacts_dir / "capability_snapshot.json", graph_dict)

    # 6. Runtime config
    _write_json(config_dir / "runtime.yaml", {
        "tick_rate_hz": 10,
        "heartbeat_timeout_sec": 5,
        "ide_callback_url": "",
        "zenoh_router": "tcp/localhost:7447",
    })
    _write_json(config_dir / "reallocation_rules.yaml", {
        "on_entity_offline": "replace_from_allocation_rules",
        "on_new_entity": "add_to_candidate_pool",
        "on_all_candidates_gone": "callback_ide",
    })

    # 7. Manifest
    manifest = {
        "scenario_name": scenario_name,
        "version": version,
        "build_time": ts,
        "ide_version": "1.0.0",
        "artifact_files": sorted(
            str(p.relative_to(root)) for p in artifacts_dir.iterdir()
        ),
    }
    _write_json(root / "manifest.json", manifest)

    logger.info("Compiled scenario package -> %s", root)
    return root


def validate_package(package_dir: Path | str) -> list[str]:
    """Return a list of errors (empty = valid)."""
    root = Path(package_dir)
    errors: list[str] = []
    required = [
        "manifest.json",
        "artifacts/behavior_tree.json",
        "artifacts/fsm_definitions.json",
        "artifacts/blackboard_init.json",
        "artifacts/allocation_rules.json",
        "artifacts/command_mappings.json",
        "artifacts/contingency_plans.json",
        "artifacts/capability_snapshot.json",
    ]
    for rel in required:
        if not (root / rel).exists():
            errors.append(f"Missing: {rel}")
    return errors


def _write_json(path: Path, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)


def _derive_command_mappings(graph: HyperGraph) -> dict:
    mappings: dict[str, dict[str, str]] = {}
    for edge in graph.edges.values():
        if edge.kind != "has_capability":
            continue
        entity_ids = [
            n for n in edge.nodes
            if graph.nodes.get(n) and graph.nodes[n].kind == "entity"
        ]
        cap_ids = [
            n for n in edge.nodes
            if graph.nodes.get(n) and graph.nodes[n].kind == "capability"
        ]
        for eid in entity_ids:
            if eid not in mappings:
                mappings[eid] = {}
            for cid in cap_ids:
                cap_node = graph.nodes[cid]
                cmd_type = cap_node.attrs.get("command_type", cid.upper())
                mappings[eid][cid] = cmd_type
    return mappings


def _generate_contingency_plans(
    graph: HyperGraph,
    allocation_rules: dict,
) -> dict:
    plans: dict[str, Any] = {}
    ranked = allocation_rules.get("ranked_candidates", {})
    for task_type, candidates in ranked.items():
        for i, candidate in enumerate(candidates):
            eid = candidate.get("entity_id", "")
            if eid not in plans:
                plans[eid] = {"replacement_order": []}
            alternates = [
                c["entity_id"]
                for c in candidates[i + 1:]
                if c["entity_id"] != eid
            ]
            plans[eid]["replacement_order"].extend(alternates)

    for eid in plans:
        seen: set[str] = set()
        unique: list[str] = []
        for r in plans[eid]["replacement_order"]:
            if r not in seen:
                seen.add(r)
                unique.append(r)
        plans[eid]["replacement_order"] = unique

    return plans
