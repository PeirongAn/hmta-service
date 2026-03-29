"""Hypergraph data structure for capability modelling.

A hypergraph extends a normal graph: each *hyperedge* can connect an
arbitrary number of nodes.  This lets us naturally represent:

* ``has_capability``  — entity     ↔ capability          (weight = proficiency)
* ``requires``        — task       ↔ capabilities         (weight = importance)
* ``collaborates``    — {entities} ↔ task                 (weight = synergy)
* ``equips``          — entity     ↔ device               (human wears device)
* ``provides``        — device     ↔ channel              (device exposes channel)
* ``enables``         — {channels} ↔ capability           (channels unlock capability)

Node kinds:
* ``entity``      — robot or human executor
* ``capability``  — an ability (navigation, approve, path_planning …)
* ``task``        — a sub-task to be assigned
* ``device``      — wearable hardware (ring, xr_glasses, glove, headset)
* ``channel``     — interaction channel provided by a device (tap_command, spatial_view …)
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass
class HNode:
    id: str
    kind: Literal["entity", "capability", "task", "device", "channel"]
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass
class HEdge:
    id: str
    kind: Literal[
        "has_capability", "requires", "collaborates",
        "equips", "provides", "enables",
        # Causal / task-dependency edges (populated by derive_task_dependencies)
        "precondition",  # capability ↔ precondition token
        "effect",        # capability ↔ effect token
        "depends_on",    # task ↔ task (auto-derived from precondition/effect chains)
    ]
    nodes: frozenset[str]
    weight: float = 1.0
    attrs: dict[str, Any] = field(default_factory=dict)


class HyperGraph:
    """In-memory hypergraph with query helpers."""

    def __init__(self) -> None:
        self.nodes: dict[str, HNode] = {}
        self.edges: dict[str, HEdge] = {}

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def add_node(self, node: HNode) -> None:
        self.nodes[node.id] = node

    def remove_node(self, node_id: str) -> None:
        self.nodes.pop(node_id, None)
        to_remove = [eid for eid, e in self.edges.items() if node_id in e.nodes]
        for eid in to_remove:
            del self.edges[eid]

    def add_edge(self, edge: HEdge) -> None:
        self.edges[edge.id] = edge

    def remove_edge(self, edge_id: str) -> None:
        self.edges.pop(edge_id, None)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def neighbors(self, node_id: str, edge_kind: str | None = None) -> set[str]:
        """Return all node IDs reachable from *node_id* via a single hyperedge."""
        result: set[str] = set()
        for e in self.edges.values():
            if node_id not in e.nodes:
                continue
            if edge_kind and e.kind != edge_kind:
                continue
            result.update(e.nodes)
        result.discard(node_id)
        return result

    def edges_of(self, node_id: str, edge_kind: str | None = None) -> list[HEdge]:
        out: list[HEdge] = []
        for e in self.edges.values():
            if node_id not in e.nodes:
                continue
            if edge_kind and e.kind != edge_kind:
                continue
            out.append(e)
        return out

    def entities_with_capability(self, cap_id: str) -> list[tuple[str, float]]:
        """Return ``[(entity_id, proficiency)]`` sorted by proficiency desc."""
        results: list[tuple[str, float]] = []
        for e in self.edges.values():
            if e.kind != "has_capability" or cap_id not in e.nodes:
                continue
            for nid in e.nodes:
                n = self.nodes.get(nid)
                if n and n.kind == "entity":
                    results.append((nid, e.weight))
        results.sort(key=lambda t: t[1], reverse=True)
        return results

    def capabilities_for_task(self, task_id: str) -> list[tuple[str, float]]:
        """Return ``[(capability_id, importance)]`` for *task_id*."""
        results: list[tuple[str, float]] = []
        for e in self.edges.values():
            if e.kind != "requires" or task_id not in e.nodes:
                continue
            for nid in e.nodes:
                n = self.nodes.get(nid)
                if n and n.kind == "capability":
                    results.append((nid, e.weight))
        return results

    def find_collaborations(self, task_id: str) -> list[HEdge]:
        return [
            e
            for e in self.edges.values()
            if e.kind == "collaborates" and task_id in e.nodes
        ]

    def tasks_depending_on(self, task_id: str) -> list[str]:
        """Return task IDs that directly depend on *task_id* (via depends_on edges)."""
        result: list[str] = []
        for e in self.edges.values():
            if e.kind != "depends_on" or task_id not in e.nodes:
                continue
            for nid in e.nodes:
                if nid != task_id:
                    result.append(nid)
        return result

    def task_dependencies(self, task_id: str) -> list[str]:
        """Return task IDs that *task_id* depends on (providers, not dependents)."""
        result: list[str] = []
        for e in self.edges.values():
            if e.kind != "depends_on":
                continue
            # depends_on edge attrs carry 'dependent' and 'provider' for clarity
            dep = e.attrs.get("dependent")
            prov = e.attrs.get("provider")
            if dep == task_id and prov:
                result.append(prov)
            elif dep is None and task_id in e.nodes:
                # legacy: both nodes in the edge; the other is the provider
                for nid in e.nodes:
                    if nid != task_id:
                        result.append(nid)
        return result

    # ------------------------------------------------------------------
    # Device / Channel / Human capability queries
    # ------------------------------------------------------------------

    def devices_of(self, entity_id: str) -> list[HNode]:
        """Return device nodes worn by *entity_id*."""
        result: list[HNode] = []
        for e in self.edges.values():
            if e.kind != "equips" or entity_id not in e.nodes:
                continue
            for nid in e.nodes:
                n = self.nodes.get(nid)
                if n and n.kind == "device" and nid != entity_id:
                    result.append(n)
        return result

    def channels_of_device(self, device_id: str) -> list[str]:
        """Return channel IDs provided by *device_id*."""
        channels: list[str] = []
        for e in self.edges.values():
            if e.kind != "provides" or device_id not in e.nodes:
                continue
            for nid in e.nodes:
                n = self.nodes.get(nid)
                if n and n.kind == "channel" and nid != device_id:
                    channels.append(nid)
        return channels

    def available_channels(self, entity_id: str) -> set[str]:
        """Return the union of all channels available to *entity_id* via online devices."""
        result: set[str] = set()
        for device in self.devices_of(entity_id):
            if device.attrs.get("status", "online") == "offline":
                continue
            result.update(self.channels_of_device(device.id))
        return result

    def capabilities_enabled_by(self, channel_ids: set[str]) -> list[str]:
        """Return capability IDs whose ``enables`` hyperedge is fully satisfied by *channel_ids*."""
        enabled: list[str] = []
        for e in self.edges.values():
            if e.kind != "enables":
                continue
            cap_nodes = [nid for nid in e.nodes if self.nodes.get(nid, HNode("", "channel")).kind == "capability"]
            ch_nodes  = [nid for nid in e.nodes if self.nodes.get(nid, HNode("", "capability")).kind == "channel"]
            if not cap_nodes:
                continue
            if set(ch_nodes) <= channel_ids:
                enabled.extend(cap_nodes)
        return enabled

    def subgraph(self, node_ids: set[str]) -> HyperGraph:
        """Return a new graph containing only *node_ids* and their edges."""
        sub = HyperGraph()
        for nid in node_ids:
            n = self.nodes.get(nid)
            if n:
                sub.add_node(copy.deepcopy(n))
        for e in self.edges.values():
            if e.nodes <= node_ids:
                sub.add_edge(copy.deepcopy(e))
        return sub

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "nodes": {
                nid: {"kind": n.kind, "attrs": n.attrs}
                for nid, n in self.nodes.items()
            },
            "edges": {
                eid: {
                    "kind": e.kind,
                    "nodes": sorted(e.nodes),
                    "weight": e.weight,
                    "attrs": e.attrs,
                }
                for eid, e in self.edges.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> HyperGraph:
        g = cls()
        for nid, ndata in data.get("nodes", {}).items():
            g.add_node(HNode(id=nid, kind=ndata["kind"], attrs=ndata.get("attrs", {})))
        for eid, edata in data.get("edges", {}).items():
            g.add_edge(
                HEdge(
                    id=eid,
                    kind=edata["kind"],
                    nodes=frozenset(edata["nodes"]),
                    weight=edata.get("weight", 1.0),
                    attrs=edata.get("attrs", {}),
                )
            )
        return g
