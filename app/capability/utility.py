"""Utility calculation — quantify how suitable an entity is for a task.

Robots and humans use different scoring dimensions.  Weights are loaded
from ``configs/capability-ontology.yaml`` and can be tuned without code
changes.  A Bayesian update entry-point is reserved for Phase C.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.capability.hypergraph import HNode, HyperGraph

_ONTOLOGY_YAML = Path(__file__).resolve().parents[2] / "configs" / "capability-ontology.yaml"

# Mode preference order (higher → more autonomous → better)
_MODE_SCORES: dict[str, float] = {
    "autonomous": 1.0,
    "supervised": 0.6,
    "remote_control": 0.3,
}


@dataclass
class AllocationScore:
    entity_id: str
    task_id: str
    total: float
    breakdown: dict[str, float] = field(default_factory=dict)


# ------------------------------------------------------------------
# Weight loading
# ------------------------------------------------------------------

def _load_weights() -> dict[str, dict[str, float]]:
    if _ONTOLOGY_YAML.exists():
        with open(_ONTOLOGY_YAML, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data.get("utility_weights", {})
    return {}


_weights_cache: dict[str, dict[str, float]] | None = None


def _get_weights() -> dict[str, dict[str, float]]:
    global _weights_cache
    if _weights_cache is None:
        _weights_cache = _load_weights()
    return _weights_cache


def reload_weights() -> None:
    global _weights_cache
    _weights_cache = None


# ------------------------------------------------------------------
# Robot utility
# ------------------------------------------------------------------

def compute_robot_utility(
    entity: HNode,
    task: HNode,
    graph: HyperGraph,
    context: dict[str, Any] | None = None,
) -> AllocationScore:
    ctx = context or {}
    w = _get_weights().get("robot", {})

    proficiency = _avg_proficiency(entity.id, task.id, graph)
    proximity = _proximity_score(entity, task, ctx)
    energy = min(entity.attrs.get("battery", 100) / 100.0, 1.0)
    availability = 1.0 if entity.attrs.get("status", "idle") == "idle" else 0.2
    mode_pref = _MODE_SCORES.get(entity.attrs.get("mode", "autonomous"), 0.5)
    risk = entity.attrs.get("risk", 0.0)
    collab = _collaboration_bonus(entity.id, task.id, graph)

    breakdown = {
        "proficiency": proficiency,
        "proximity": proximity,
        "energy": energy,
        "availability": availability,
        "mode_preference": mode_pref,
        "risk": risk,
        "collaboration": collab,
    }

    total = (
        w.get("proficiency", 0.3) * proficiency
        + w.get("proximity", 0.2) * proximity
        + w.get("energy", 0.15) * energy
        + w.get("availability", 0.15) * availability
        + w.get("mode_preference", 0.1) * mode_pref
        + w.get("risk", -0.05) * risk
        + w.get("collaboration", 0.05) * collab
    )

    return AllocationScore(
        entity_id=entity.id,
        task_id=task.id,
        total=total,
        breakdown=breakdown,
    )


# ------------------------------------------------------------------
# Human utility
# ------------------------------------------------------------------

def compute_human_utility(
    entity: HNode,
    task: HNode,
    graph: HyperGraph,
    context: dict[str, Any] | None = None,
) -> AllocationScore:
    w = _get_weights().get("human", {})

    raw_accuracy = entity.attrs.get("decision_accuracy", 0.8)
    fatigue = entity.attrs.get("fatigue_level", entity.attrs.get("fatigue", 0.0))

    # Fatigue modulates cognitive performance:
    #   accuracy degrades up to 30% at full fatigue
    #   response time inflates up to 50% at full fatigue
    accuracy = raw_accuracy * (1.0 - 0.3 * fatigue)

    raw_response = entity.attrs.get("avg_response_sec", 10.0)
    speed = _normalise_response_speed(raw_response * (1.0 + 0.5 * fatigue))

    authority = 1.0 if entity.attrs.get("authority_level") else 0.5
    max_conc = entity.attrs.get("max_concurrent_tasks", 3)
    current = entity.attrs.get("current_task_count", 0)
    cog_load = min(current / max(max_conc, 1), 1.0)

    breakdown = {
        "decision_accuracy": accuracy,
        "response_speed": speed,
        "authority_match": authority,
        "cognitive_load": cog_load,
        "fatigue": fatigue,
    }

    total = (
        w.get("decision_accuracy", 0.3) * accuracy
        + w.get("response_speed", 0.2) * speed
        + w.get("authority_match", 0.2) * authority
        + w.get("cognitive_load", -0.15) * cog_load
        + w.get("fatigue", -0.15) * fatigue
    )

    return AllocationScore(
        entity_id=entity.id,
        task_id=task.id,
        total=total,
        breakdown=breakdown,
    )


# ------------------------------------------------------------------
# Bayesian update — Phase C entry-point
# ------------------------------------------------------------------

def bayesian_update(
    weights: dict[str, float],
    performance_history: list[dict],
) -> dict[str, float]:
    """Update utility weights using Phase A piecewise linear learner.

    Returns the original weights if data is insufficient (<15 records)
    or if the model fit is too poor (R² < 0.3).
    """
    try:
        from app.experiment.learner import BoundaryLearner
        from app.experiment.store import ExperimentStore

        store = ExperimentStore()
        if store.count() < 15:
            return dict(weights)

        learner = BoundaryLearner(store)
        result = learner.fit_piecewise_linear()

        if result.overall_r_squared < 0.3:
            return dict(weights)

        learned = learner.export_weights()
        if learned:
            merged = dict(weights)
            merged.update(learned)
            return merged
        return dict(weights)
    except Exception:
        logger.exception("bayesian_update failed — returning original weights")
        return dict(weights)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _avg_proficiency(entity_id: str, task_id: str, graph: HyperGraph) -> float:
    required = graph.capabilities_for_task(task_id)
    if not required:
        return 0.5
    total = 0.0
    count = 0
    for cap_id, importance in required:
        for eid, prof in graph.entities_with_capability(cap_id):
            if eid == entity_id:
                total += prof * importance
                count += 1
                break
    return total / max(count, 1)


def _proximity_score(entity: HNode, task: HNode, context: dict) -> float:
    epos = entity.attrs.get("position")
    tpos = task.attrs.get("target_position") or context.get("target_position")
    if not epos or not tpos:
        return 0.5
    try:
        dist = math.sqrt(sum((a - b) ** 2 for a, b in zip(epos, tpos)))
    except (TypeError, ValueError):
        return 0.5
    return max(1.0 - dist / 10000.0, 0.0)


def _normalise_response_speed(avg_sec: float) -> float:
    """Fast response → high score.  30s → 0, 0s → 1."""
    return max(1.0 - avg_sec / 30.0, 0.0)


def _collaboration_bonus(entity_id: str, task_id: str, graph: HyperGraph) -> float:
    """Simple additive bonus if entity is part of a collaboration group."""
    collabs = graph.find_collaborations(task_id)
    for e in collabs:
        if entity_id in e.nodes:
            return e.weight
    return 0.0
