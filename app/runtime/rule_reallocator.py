"""Rule-based reallocator for Runtime anomaly handling.

Uses the distilled allocation_rules.json lookup table — zero LLM calls.
Handles L2/L3 level exceptions (entity offline, new entity joins).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class RuleReallocator:
    """Deterministic reallocation driven by pre-computed lookup tables."""

    def __init__(
        self,
        allocation_rules: dict[str, Any],
        contingency_plans: dict[str, Any],
    ) -> None:
        self._rules = allocation_rules
        self._contingency = contingency_plans
        self._offline_entities: set[str] = set()

    def on_entity_offline(self, entity_id: str) -> dict | None:
        """Find a replacement for *entity_id* from ranked candidates.

        Returns ``{"original": entity_id, "replacement": new_id}`` or
        ``None`` if no candidate is available.
        """
        self._offline_entities.add(entity_id)
        plan = self._contingency.get(entity_id, {})
        replacements = plan.get("replacement_order", [])

        for candidate in replacements:
            if candidate not in self._offline_entities:
                logger.info(
                    "Reallocating %s -> %s (rule-based)", entity_id, candidate,
                )
                return {"original": entity_id, "replacement": candidate}

        logger.warning("No replacement candidate for %s", entity_id)
        return None

    def on_entity_online(self, entity_id: str) -> None:
        """Mark entity as available again."""
        self._offline_entities.discard(entity_id)

    def on_new_entity(self, entity_id: str) -> None:
        """A previously unknown entity joined the network."""
        self._offline_entities.discard(entity_id)
        logger.info("New entity %s joined — added to candidate pool", entity_id)

    def needs_ide_callback(self) -> bool:
        """True when all candidates for any task are offline."""
        ranked = self._rules.get("ranked_candidates", {})
        for task_type, candidates in ranked.items():
            available = [
                c for c in candidates
                if c.get("entity_id") not in self._offline_entities
            ]
            if not available:
                return True
        return False
