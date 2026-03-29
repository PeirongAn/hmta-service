"""Ground truth store — manages known target positions from UE simulation.

Targets are registered via Zenoh ``zho/sim/ground_truth`` and used by
:class:`OracleService` to validate robot detection events against reality.
"""

from __future__ import annotations

import logging
import math
from typing import Any

from app.oracle.types import GroundTruthTarget

logger = logging.getLogger(__name__)


class GroundTruthStore:
    """In-memory store for UE ground truth targets (bomb positions, etc.)."""

    def __init__(self) -> None:
        self._targets: dict[str, GroundTruthTarget] = {}

    # ── Registration ──────────────────────────────────────────────────────

    def register_targets(self, targets: list[dict[str, Any]]) -> None:
        """Bulk-register targets from a ``zho/sim/ground_truth`` payload."""
        for t in targets:
            tid = t.get("target_id") or t.get("id", "")
            if not tid:
                continue
            gt = GroundTruthTarget(
                target_id=tid,
                target_type=t.get("type", "unknown"),
                position=t.get("position", {}),
                zone_id=t.get("zone_id", ""),
            )
            self._targets[tid] = gt
        logger.info(
            "[GroundTruthStore] Registered %d targets (total now %d)",
            len(targets), len(self._targets),
        )

    def register_single(self, target: GroundTruthTarget) -> None:
        self._targets[target.target_id] = target

    # ── Spatial matching ──────────────────────────────────────────────────

    def match_detection(
        self,
        position: dict[str, float],
        threshold_cm: float = 200.0,
    ) -> GroundTruthTarget | None:
        """Find the closest ground-truth target within *threshold_cm*.

        Returns ``None`` if no target is close enough — indicating a likely
        false positive.
        """
        if not self._targets:
            return None

        dx = position.get("x", 0.0)
        dy = position.get("y", 0.0)
        dz = position.get("z", 0.0)

        best: GroundTruthTarget | None = None
        best_dist = float("inf")

        for gt in self._targets.values():
            gx = gt.position.get("x", 0.0)
            gy = gt.position.get("y", 0.0)
            gz = gt.position.get("z", 0.0)
            dist = math.sqrt((dx - gx) ** 2 + (dy - gy) ** 2 + (dz - gz) ** 2)
            if dist < best_dist:
                best_dist = dist
                best = gt

        if best is not None and best_dist <= threshold_cm:
            return best
        return None

    # ── Housekeeping ──────────────────────────────────────────────────────

    def clear(self) -> None:
        """Reset all targets (call on new mission)."""
        self._targets.clear()

    @property
    def target_count(self) -> int:
        return len(self._targets)

    def all_targets(self) -> list[GroundTruthTarget]:
        return list(self._targets.values())
