"""Capability graph API — expose hypergraph for IDE visualisation."""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/v1/capability", tags=["capability"])


@router.get("/graph")
async def get_capability_graph(request: Request):
    """Full hypergraph snapshot (nodes + edges)."""
    registry = request.app.state.capability_registry
    graph = registry.get_graph_snapshot()
    return graph.to_dict()


@router.get("/graph/stats")
async def get_graph_stats(request: Request):
    """Summary statistics: node/edge counts by kind."""
    registry = request.app.state.capability_registry
    graph = registry.get_graph_ref()
    node_kinds: dict[str, int] = {}
    for n in graph.nodes.values():
        node_kinds[n.kind] = node_kinds.get(n.kind, 0) + 1
    edge_kinds: dict[str, int] = {}
    for e in graph.edges.values():
        edge_kinds[e.kind] = edge_kinds.get(e.kind, 0) + 1
    return {
        "total_nodes": len(graph.nodes),
        "total_edges": len(graph.edges),
        "node_kinds": node_kinds,
        "edge_kinds": edge_kinds,
    }


@router.get("/graph/entity/{entity_id}")
async def get_entity_subgraph(entity_id: str, request: Request):
    """1-hop subgraph centred on *entity_id*."""
    registry = request.app.state.capability_registry
    graph = registry.get_graph_ref()
    if entity_id not in graph.nodes:
        return {"error": f"entity {entity_id} not found", "nodes": {}, "edges": {}}
    neighbors = graph.neighbors(entity_id)
    subgraph = graph.subgraph(neighbors | {entity_id})
    return subgraph.to_dict()


@router.get("/experiment/count")
async def get_experiment_count(request: Request):
    """Quick check: how many experiment records exist."""
    store = getattr(request.app.state, "experiment_store", None)
    if store is None:
        return {"count": 0, "status": "store_not_initialised"}
    return {"count": store.count()}
