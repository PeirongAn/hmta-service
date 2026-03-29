"""Distill the hypergraph + utility model into deterministic lookup tables.

The Runtime uses these tables for rule-based reallocation instead of
re-running the full utility calculation at execution time.
"""

from __future__ import annotations

from typing import Any

from app.capability.hypergraph import HyperGraph
from app.capability.utility import (
    compute_human_utility,
    compute_robot_utility,
)


def distill_allocation_rules(
    graph: HyperGraph,
    weights: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Distill the hypergraph into a deterministic allocation lookup table.

    Returns::

        {
            "ranked_candidates": {
                "<task_type>": [
                    {"entity_id": "...", "score": 0.87, "mode": "autonomous"},
                    ...
                ]
            },
            "fallback_rules": { ... },
            "collaboration_groups": [ ... ],
        }
    """
    ranked: dict[str, list[dict]] = {}

    task_nodes = [n for n in graph.nodes.values() if n.kind == "task"]
    entity_nodes = [n for n in graph.nodes.values() if n.kind == "entity"]

    for task in task_nodes:
        scores = []
        for entity in entity_nodes:
            etype = entity.attrs.get("type", entity.attrs.get("entity_type", "robot"))
            if etype == "human":
                s = compute_human_utility(entity, task, graph)
            else:
                s = compute_robot_utility(entity, task, graph)
            scores.append({
                "entity_id": entity.id,
                "score": round(s.total, 4),
                "mode": entity.attrs.get("mode", "autonomous"),
                "type": etype,
            })
        scores.sort(key=lambda x: x["score"], reverse=True)
        ranked[task.id] = scores

    collab_groups = []
    for edge in graph.edges.values():
        if edge.kind == "collaborates":
            entities_in_edge = [
                nid for nid in edge.nodes
                if graph.nodes.get(nid) and graph.nodes[nid].kind == "entity"
            ]
            if len(entities_in_edge) > 1:
                collab_groups.append({
                    "entities": sorted(entities_in_edge),
                    "synergy": edge.weight,
                })

    return {
        "ranked_candidates": ranked,
        "fallback_rules": {
            "on_entity_offline": "use_next_ranked_candidate",
            "on_all_offline": "callback_ide",
        },
        "collaboration_groups": collab_groups,
    }
