"""Runtime capability registry — driven by Zenoh entity registration.

Internally backed by :class:`HyperGraph` so all queries are graph-native.

Robot registration path:
    register_entity() → HNode(entity) + HNode(capability) + HEdge(has_capability)

Human registration path:
    register_human_with_devices()
        → HNode(entity)
        → for each device: HNode(device) + HEdge(equips)
            → for each channel: HNode(channel) + HEdge(provides)
        → EffectiveCapabilityResolver.resolve_and_apply()
            → derives HEdge(enables) + HEdge(has_capability)

Device status update:
    update_device_status() → updates device node attrs.status
        → re-runs EffectiveCapabilityResolver → may add/remove has_capability
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Callable

import time as _time

from app.capability.effective_resolver import EffectiveCapabilityResolver
from app.capability.hypergraph import HEdge, HNode, HyperGraph
from app.capability.ontology import (
    mode_from_capability_mode,
    resolve_alias,
)

logger = logging.getLogger(__name__)


class CapabilityRegistry:
    """Thread-safe-ish registry.

    Mutations happen on the Zenoh callback thread; reads happen on the
    generation thread.  We rely on the GIL for dict-level atomicity,
    which is sufficient for the current single-writer / single-reader pattern.
    """

    def __init__(
        self,
        on_capabilities_changed: Callable[[str, set[str], set[str]], None] | None = None,
    ) -> None:
        self._graph = HyperGraph()
        self._resolver = EffectiveCapabilityResolver(
            on_capabilities_changed=on_capabilities_changed,
        )
        # DB-persisted proficiency: {(entity_id, capability_id): float}
        # Kept in memory so register_entity can override payload defaults
        # with previously learned values.
        self._persisted_proficiency: dict[tuple[str, str], float] = {}
        # Cached entity-only subgraph (entity/device/channel/capability nodes).
        # None means the cache is stale and must be rebuilt on next access.
        self._entity_subgraph: HyperGraph | None = None

    # ------------------------------------------------------------------
    # Robot registration
    # ------------------------------------------------------------------

    def register_entity(self, entity_data: dict[str, Any]) -> None:
        """Register or update a robot entity and its capabilities.

        Accepts both structured_capabilities (preferred) and flat
        capabilities string list (fallback).

        Expected shape::

            {
                "entity_id": "robot_A",
                "entity_type": "robot",
                "status": "online",
                "structured_capabilities": [
                    {
                        "name": "navigation.move_to",
                        "mode": "autonomous",
                        "proficiency": 0.9,
                        "params": {"max_speed": "3.0"},
                        "param_defs": [...]
                    }
                ],
                "capabilities": ["move", "detect"],   # flat fallback
            }
        """
        eid = entity_data.get("entity_id", "")
        if not eid:
            return

        entity_node = HNode(
            id=eid,
            kind="entity",
            attrs={
                k: v for k, v in entity_data.items()
                if k not in ("structured_capabilities", "capabilities", "devices")
            },
        )
        self._graph.add_node(entity_node)

        structured: list[dict] = entity_data.get("structured_capabilities", [])
        flat: list[str] = entity_data.get("capabilities", [])

        caps: list[dict[str, Any]] = (
            structured if structured
            else [{"name": c} if isinstance(c, str) else c for c in flat]
        )

        # Remove stale has_capability edges
        for edge_id in [e.id for e in self._graph.edges_of(eid, "has_capability")]:
            self._graph.remove_edge(edge_id)

        for cap_data in caps:
            raw_name = cap_data.get("name") or cap_data.get("id", "")
            if not raw_name:
                continue
            canonical = resolve_alias(str(raw_name))
            cap_node = HNode(
                id=canonical,
                kind="capability",
                attrs={k: v for k, v in cap_data.items() if k not in ("name", "id")},
            )
            self._graph.add_node(cap_node)

            mode_raw = cap_data.get("mode", "autonomous")
            prof = float(cap_data.get("proficiency", 1.0))

            persisted = self._persisted_proficiency.get((eid, canonical))
            if persisted is not None:
                prof = persisted

            self._graph.add_edge(HEdge(
                id=f"hc_{eid}_{canonical}",
                kind="has_capability",
                nodes=frozenset([eid, canonical]),
                weight=prof,
                attrs={
                    "mode": mode_raw,
                    "collab_mode": mode_from_capability_mode(mode_raw),
                },
            ))

        self._invalidate_entity_subgraph()
        logger.debug("Registered robot %s with %d capabilities", eid, len(caps))

    # ------------------------------------------------------------------
    # Human registration (device-aware)
    # ------------------------------------------------------------------

    def register_human_with_devices(self, human_data: dict[str, Any]) -> None:
        """Register a human entity with wearable devices.

        Derives effective capabilities via EffectiveCapabilityResolver.

        Expected shape::

            {
                "entity_id": "operator-01",
                "entity_type": "human",
                "authority_level": "operator",
                "devices": [
                    {
                        "device_id": "ring_01",
                        "type": "ring",
                        "status": "online",
                        "channels": ["tap_command", "haptic_alert"],
                        "constraints": {"battery": 0.95}
                    },
                    ...
                ],
                "cognitive_profile": {
                    "max_concurrent_tasks": 3,
                    "decision_accuracy": 0.85,
                    "avg_response_sec": 8.0
                }
            }
        """
        eid = human_data.get("entity_id", "")
        if not eid:
            return

        # Entity node
        entity_attrs = {
            k: v for k, v in human_data.items()
            if k not in ("devices",)
        }
        entity_attrs.setdefault("entity_type", "human")
        self._graph.add_node(HNode(id=eid, kind="entity", attrs=entity_attrs))

        # Remove stale device/equips structure
        for edge in list(self._graph.edges_of(eid, "equips")):
            device_id = next((n for n in edge.nodes if n != eid), None)
            if device_id:
                self._graph.remove_node(device_id)   # also removes provides edges
            self._graph.remove_edge(edge.id)

        # Build device → channel sub-graph
        for device_data in human_data.get("devices", []):
            did = device_data.get("device_id", "")
            if not did:
                continue

            device_node = HNode(
                id=did,
                kind="device",
                attrs={
                    "type": device_data.get("type", "unknown"),
                    "status": device_data.get("status", "online"),
                    "constraints": device_data.get("constraints", {}),
                },
            )
            self._graph.add_node(device_node)

            # equips edge: entity → device
            self._graph.add_edge(HEdge(
                id=f"eq_{eid}_{did}",
                kind="equips",
                nodes=frozenset([eid, did]),
                weight=1.0,
            ))

            # provides edges: device → channels (weight = channel quality)
            ch_quality = self._channel_quality(device_node)
            for ch_id in device_data.get("channels", []):
                if ch_id not in self._graph.nodes:
                    self._graph.add_node(HNode(id=ch_id, kind="channel", attrs={}))
                self._graph.add_edge(HEdge(
                    id=f"pv_{did}_{ch_id}",
                    kind="provides",
                    nodes=frozenset([did, ch_id]),
                    weight=ch_quality,
                ))

        # Derive effective capabilities from available channels
        self._resolver.resolve_and_apply(self._graph, eid)
        self._invalidate_entity_subgraph()
        logger.info(
            "Registered human %s with %d devices",
            eid, len(human_data.get("devices", [])),
        )

    # ------------------------------------------------------------------
    # Device status update (hot path — triggers capability re-derivation)
    # ------------------------------------------------------------------

    def update_device_status(self, entity_id: str, device_statuses: list[dict[str, Any]]) -> None:
        """Update the online/offline status of an operator's devices.

        Called on every human state message that includes ``device_status``.
        Re-derives effective capabilities and fires the change callback if needed.

        device_statuses: list of {device_id, status, battery?}
        """
        changed = False
        for ds in device_statuses:
            did = ds.get("device_id", "")
            if not did or did not in self._graph.nodes:
                continue
            node = self._graph.nodes[did]
            new_status = ds.get("status", "online")
            old_status = node.attrs.get("status", "online")
            if "battery" in ds:
                node.attrs.setdefault("constraints", {})["battery"] = ds["battery"]
            if new_status != old_status:
                node.attrs["status"] = new_status
                changed = True
                logger.info(
                    "[Registry] device %s of %s: %s → %s",
                    did, entity_id, old_status, new_status,
                )

            # Refresh provides edge weights based on device quality
            new_quality = self._channel_quality(node)
            for edge in list(self._graph.edges_of(did, "provides")):
                if abs(edge.weight - new_quality) > 0.01:
                    edge.weight = new_quality
                    changed = True

        if changed:
            self._resolver.resolve_and_apply(self._graph, entity_id)
            self._invalidate_entity_subgraph()

    # ------------------------------------------------------------------
    # Proficiency & cognitive profile updates (used by learner / rules)
    # ------------------------------------------------------------------

    def get_proficiency(self, entity_id: str, capability_id: str) -> float:
        """Return the ``has_capability`` edge weight for *entity_id* × *capability_id*.

        Uses the same edge id convention as :meth:`register_entity` /
        :meth:`update_proficiency`. If no edge exists, returns ``1.0`` (registration default).
        """
        canonical = resolve_alias(capability_id)
        edge_id = f"hc_{entity_id}_{canonical}"
        edge = self._graph.edges.get(edge_id)
        if edge and edge.kind == "has_capability":
            return float(edge.weight)
        return 1.0

    def load_persisted_proficiency(self, values: dict[tuple[str, str], float]) -> None:
        """Seed has_capability edge weights from ProficiencyStore on startup.

        Stored values are kept in ``_persisted_proficiency`` so that future
        ``register_entity`` calls automatically use DB values instead of
        payload defaults.  Any edges that already exist are updated immediately.
        """
        # Remember all persisted values for future register_entity calls
        for (entity_id, capability_id), prof in values.items():
            from app.capability.ontology import resolve_alias
            canonical = resolve_alias(capability_id)
            self._persisted_proficiency[(entity_id, canonical)] = max(0.0, min(1.0, prof))

        # Apply to edges that already exist
        applied = 0
        for (entity_id, canonical), prof in self._persisted_proficiency.items():
            edge_id = f"hc_{entity_id}_{canonical}"
            edge = self._graph.edges.get(edge_id)
            if edge and edge.kind == "has_capability":
                edge.weight = prof
                applied += 1
        logger.info(
            "[Registry] load_persisted_proficiency: %d persisted, %d applied to existing edges",
            len(self._persisted_proficiency), applied,
        )

    def update_proficiency(
        self, entity_id: str, capability_id: str, new_prof: float,
    ) -> None:
        """Modify the ``has_capability`` edge weight for a specific entity-capability pair.

        Called by ``BoundaryLearner`` after learning or by runtime rules.
        Maintains an audit trail in ``edge.attrs["proficiency_history"]``.
        Also updates ``_persisted_proficiency`` so that subsequent
        ``register_entity`` calls will use the learned value.
        """
        canonical = resolve_alias(capability_id)
        clamped = max(0.0, min(1.0, new_prof))
        self._persisted_proficiency[(entity_id, canonical)] = clamped

        edge_id = f"hc_{entity_id}_{canonical}"
        edge = self._graph.edges.get(edge_id)
        if edge:
            old = edge.weight
            edge.weight = clamped
            edge.attrs.setdefault("proficiency_history", []).append({
                "value": old, "updated_at": _time.time(),
            })
            logger.info(
                "[Registry] proficiency updated: %s/%s  %.3f → %.3f",
                entity_id, canonical, old, edge.weight,
            )
        else:
            logger.warning(
                "[Registry] update_proficiency: edge %s not found", edge_id,
            )

    def update_cognitive_profile(
        self, entity_id: str, updates: dict[str, float],
    ) -> None:
        """Update an operator's cognitive baseline values.

        *updates* example: ``{"decision_accuracy": 0.88, "avg_response_sec": 6.5}``

        Values are written both into the nested ``cognitive_profile`` dict
        and flattened into top-level attrs (for direct access by ``compute_human_utility``).
        """
        node = self._graph.nodes.get(entity_id)
        if node and node.kind == "entity":
            cog = node.attrs.get("cognitive_profile", {})
            cog.update(updates)
            node.attrs["cognitive_profile"] = cog
            for k, v in updates.items():
                if v is not None:
                    node.attrs[k] = v
            logger.info("[Registry] cognitive profile updated: %s  %s", entity_id, updates)
            self._invalidate_entity_subgraph()
        else:
            logger.warning("[Registry] update_cognitive_profile: entity %s not found", entity_id)

    # ------------------------------------------------------------------
    # Channel quality (Sprint 4: device degradation)
    # ------------------------------------------------------------------

    @staticmethod
    def _channel_quality(device_node: HNode) -> float:
        """Compute channel quality coefficient from device physical state.

        Returns a value in [0, 1] that modulates the ``provides`` edge weight.
        """
        status = device_node.attrs.get("status", "online")
        if status in ("offline", "dead", "DEAD"):
            return 0.0
        battery = device_node.attrs.get("constraints", {}).get("battery", 1.0)
        if isinstance(battery, str):
            try:
                battery = float(battery)
            except ValueError:
                battery = 1.0
        if battery < 0.15:
            return 0.3
        if battery < 0.30:
            return 0.7
        return 1.0

    # ------------------------------------------------------------------
    # Entity subgraph cache
    # ------------------------------------------------------------------

    def _invalidate_entity_subgraph(self) -> None:
        """Mark the entity subgraph cache as stale.

        Called after any mutation that changes entity/device/channel/capability
        nodes or has_capability/equips/provides/enables edges.
        """
        self._entity_subgraph = None

    def get_entity_subgraph(self) -> HyperGraph:
        """Return a lightweight copy of the entity/device/channel/capability subgraph.

        Cache hit  → O(N) dict copy (no node reconstruction).
        Cache miss → rebuild from _graph, then return a copy.

        The returned graph shares HNode/HEdge objects with the internal cache.
        Callers must not mutate entity node attrs; they may freely add new
        task/requires nodes and edges to the returned graph.
        """
        if self._entity_subgraph is None:
            self._entity_subgraph = self._rebuild_entity_subgraph()
            logger.debug(
                "[Registry] entity_subgraph rebuilt (nodes=%d, edges=%d)",
                len(self._entity_subgraph.nodes),
                len(self._entity_subgraph.edges),
            )
        # Shallow copy: new dicts, shared HNode/HEdge objects
        g = HyperGraph()
        g.nodes = dict(self._entity_subgraph.nodes)
        g.edges = dict(self._entity_subgraph.edges)
        return g

    def _rebuild_entity_subgraph(self) -> HyperGraph:
        """Extract entity-related nodes/edges from _graph into a fresh HyperGraph."""
        entity_kinds = {"entity", "device", "channel", "capability"}
        entity_edge_kinds = {"has_capability", "equips", "provides", "enables"}
        sub = HyperGraph()
        for nid, node in self._graph.nodes.items():
            if node.kind in entity_kinds:
                sub.add_node(node)
        node_ids = sub.nodes.keys()
        for eid, edge in self._graph.edges.items():
            if edge.kind in entity_edge_kinds and edge.nodes <= node_ids:
                sub.add_edge(edge)
        return sub

    # ------------------------------------------------------------------
    # Unregistration
    # ------------------------------------------------------------------

    def unregister_entity(self, entity_id: str) -> None:
        # Also remove attached devices
        for edge in list(self._graph.edges_of(entity_id, "equips")):
            device_id = next((n for n in edge.nodes if n != entity_id), None)
            if device_id:
                self._graph.remove_node(device_id)
        self._graph.remove_node(entity_id)
        self._invalidate_entity_subgraph()
        logger.debug("Unregistered entity %s", entity_id)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_graph_snapshot(self) -> HyperGraph:
        """Deep-copy (safe for concurrent reads)."""
        return copy.deepcopy(self._graph)

    def get_graph_ref(self) -> HyperGraph:
        """Direct reference (fast, NOT mutation-safe)."""
        return self._graph

    def query_entities_for_capability(
        self,
        cap_name: str,
        min_proficiency: float = 0.0,
    ) -> list[tuple[str, float]]:
        canonical = resolve_alias(cap_name)
        results = self._graph.entities_with_capability(canonical)
        return [(eid, prof) for eid, prof in results if prof >= min_proficiency]

    def all_entity_ids(self) -> list[str]:
        return [nid for nid, n in self._graph.nodes.items() if n.kind == "entity"]

    def get_entity_collaboration_mode(self, entity_id: str) -> str:
        """Return highest collaboration mode available to *entity_id*."""
        return EffectiveCapabilityResolver.collaboration_mode_for(self._graph, entity_id)

    def get_param_defs(self, capability_id: str) -> list[dict[str, Any]]:
        """Return param_defs for a capability, or [] if not declared."""
        canonical = resolve_alias(capability_id)
        node = self._graph.nodes.get(canonical)
        if not node:
            return []
        return node.attrs.get("param_defs", [])
