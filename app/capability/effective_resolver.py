"""EffectiveCapabilityResolver — device → channel → capability inference chain.

For human entities, capabilities are NOT intrinsic (unlike robots).
They are derived from the interaction channels available via online devices:

    entity --[equips]--> device --[provides]--> channel
    {channels} --[enables]--> capability
    → write has_capability edge into the graph

This module walks that chain and (re-)writes ``has_capability`` edges
in the HyperGraph whenever a human's device set changes.

Typical call sites:
- CapabilityRegistry.register_human_with_devices()   (initial registration)
- CapabilityRegistry.update_device_status()          (device goes online/offline)
"""

from __future__ import annotations

import logging
from typing import Callable

from app.capability.hypergraph import HEdge, HNode, HyperGraph
from app.capability.ontology import capabilities_from_channels, get_channel_quality_map, resolve_alias

logger = logging.getLogger(__name__)


class EffectiveCapabilityResolver:
    """Infer and maintain ``has_capability`` edges for human entities."""

    # Proficiency weight for human capabilities derived from devices.
    # The actual value is modulated by the number of required channels
    # satisfied vs. the total available (all-or-nothing per rule, but
    # richer device sets are implicitly higher quality).
    _BASE_PROFICIENCY: float = 0.9

    def __init__(self, on_capabilities_changed: Callable[[str, set[str], set[str]], None] | None = None) -> None:
        """
        Args:
            on_capabilities_changed: optional callback fired when a human's
                effective capability set changes.  Signature:
                ``(entity_id, added_caps, removed_caps) -> None``
        """
        self._on_changed = on_capabilities_changed

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve_and_apply(self, graph: HyperGraph, entity_id: str) -> set[str]:
        """Re-compute effective capabilities for *entity_id* and update the graph.

        Returns the new set of canonical capability IDs now held by the entity.
        """
        entity_node = graph.nodes.get(entity_id)
        if not entity_node or entity_node.kind != "entity":
            logger.warning("[EffectiveResolver] entity not found: %s", entity_id)
            return set()

        # 1. Collect available channels from online devices
        available_channels = graph.available_channels(entity_id)
        logger.debug(
            "[EffectiveResolver] %s — available channels: %s",
            entity_id, sorted(available_channels),
        )

        # 2. Determine new effective capabilities from ontology enables_rules
        new_caps = capabilities_from_channels(available_channels)
        logger.debug(
            "[EffectiveResolver] %s — effective capabilities: %s",
            entity_id, sorted(new_caps),
        )

        # 3. Determine previously held capabilities (via has_capability edges)
        old_caps: set[str] = {
            nid
            for e in graph.edges_of(entity_id, "has_capability")
            for nid in e.nodes
            if nid != entity_id
        }

        # 4. Remove stale has_capability edges
        removed = old_caps - new_caps
        for cap_id in removed:
            edge_id = f"hc_{entity_id}_{cap_id}"
            if edge_id in graph.edges:
                graph.remove_edge(edge_id)
                logger.info("[EffectiveResolver] %s lost capability: %s", entity_id, cap_id)

        # 5. Add new has_capability edges
        added = new_caps - old_caps
        for cap_id in added:
            canonical = resolve_alias(cap_id)
            if canonical not in graph.nodes:
                graph.add_node(HNode(id=canonical, kind="capability", attrs={"source": "device_derived"}))
            edge_id = f"hc_{entity_id}_{canonical}"
            prof = self._channel_weighted_proficiency(entity_id, canonical, graph)
            graph.add_edge(HEdge(
                id=edge_id,
                kind="has_capability",
                nodes=frozenset([entity_id, canonical]),
                weight=prof,
                attrs={
                    "mode": "supervised",
                    "source": "device_derived",
                },
            ))
            logger.info("[EffectiveResolver] %s gained capability: %s (prof=%.2f)", entity_id, cap_id, prof)

        # 6. Fire change callback
        if self._on_changed and (added or removed):
            self._on_changed(entity_id, added, removed)

        return new_caps

    def resolve_all_humans(self, graph: HyperGraph) -> dict[str, set[str]]:
        """Re-compute effective capabilities for ALL human entities in the graph."""
        result: dict[str, set[str]] = {}
        for nid, node in list(graph.nodes.items()):
            if node.kind == "entity" and node.attrs.get("entity_type") == "human":
                result[nid] = self.resolve_and_apply(graph, nid)
        return result

    # ------------------------------------------------------------------
    # Proficiency resolution
    # ------------------------------------------------------------------

    def _resolve_proficiency(
        self, entity_id: str, capability_id: str, graph: HyperGraph,
    ) -> float:
        """Look up a per-operator capability proficiency override.

        Falls back to ``_BASE_PROFICIENCY`` when no override is found.
        Overrides come from ``human-profiles.yaml → proficiency_overrides``
        which are stored on the entity node attrs during registration.
        """
        entity_node = graph.nodes.get(entity_id)
        if not entity_node:
            return self._BASE_PROFICIENCY
        overrides = entity_node.attrs.get("proficiency_overrides", {})
        return float(overrides.get(capability_id, self._BASE_PROFICIENCY))

    def _channel_weighted_proficiency(
        self, entity_id: str, capability_id: str, graph: HyperGraph,
    ) -> float:
        """Proficiency modulated by channel quality.

        If the capability has a ``channel_quality_map`` defined in the ontology,
        the best matching channel combination determines quality.  Otherwise
        falls back to the barrel principle (minimum channel weight).
        """
        base = self._resolve_proficiency(entity_id, capability_id, graph)
        available_channels = graph.available_channels(entity_id)

        quality_map = get_channel_quality_map(capability_id)
        if quality_map:
            best_quality = 0.0
            for combo_str, quality in quality_map.items():
                required = {ch.strip() for ch in combo_str.split("+")}
                if required <= available_channels:
                    best_quality = max(best_quality, quality)
            if best_quality > 0:
                return round(base * best_quality, 4)

        channel_weights: list[float] = []
        for equip_edge in graph.edges_of(entity_id, "equips"):
            device_id = next((n for n in equip_edge.nodes if n != entity_id), None)
            if not device_id:
                continue
            for pv_edge in graph.edges_of(device_id, "provides"):
                channel_weights.append(pv_edge.weight)
        if not channel_weights:
            return base
        min_quality = min(channel_weights)
        return round(base * min_quality, 4)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def available_channels_for(graph: HyperGraph, entity_id: str) -> set[str]:
        """Convenience: return available channel IDs without modifying the graph."""
        return graph.available_channels(entity_id)

    @staticmethod
    def collaboration_mode_for(graph: HyperGraph, entity_id: str) -> str:
        """Derive the highest collaboration mode available to *entity_id*.

        Returns one of: ``"proxy"`` | ``"partner"`` | ``"task_based"``.
        """
        from app.capability.ontology import get_collaboration_modes
        channels = graph.available_channels(entity_id)
        modes = get_collaboration_modes()

        # Check from most demanding to least
        for mode_key in ("proxy", "partner", "task_based"):
            mode_meta = modes.get(mode_key, {})
            required = set(mode_meta.get("min_channels", []))
            if required <= channels:
                return mode_key
        return "task_based"
