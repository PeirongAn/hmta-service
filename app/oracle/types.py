"""Oracle data types for ground-truth capability judgment."""

from __future__ import annotations

import time as _time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class GroundTruthTarget:
    """A known target registered by UE simulation as ground truth."""

    target_id: str
    target_type: str  # "bomb", "person", "threat", ...
    position: dict[str, float]  # {"x": float, "y": float, "z": float}
    zone_id: str = ""
    registered_at: float = field(default_factory=_time.time)


@dataclass
class OracleJudgment:
    """A ground-truth judgment on an entity's capability performance."""

    entity_id: str
    capability_id: str  # "detect", "move", "path_planning", ...
    entity_type: str  # "robot" | "human"
    judgment_type: str  # "detection_accuracy" | "navigation_reachability"
    outcome: str  # "true_positive" | "false_positive" | "reachable" | "unreachable"
    source: str  # "human_operator" | "ue_ground_truth" | "navmesh"
    confidence: float = 1.0
    details: dict[str, Any] = field(default_factory=dict)
    task_id: str = ""
    judgment_id: str = field(default_factory=lambda: f"oj-{uuid.uuid4().hex[:12]}")
    timestamp: float = field(default_factory=_time.time)
