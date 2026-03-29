"""Capability ontology — hierarchical taxonomy, channel definitions,
enables rules, and collaboration mode configurations.

All data is loaded from ``configs/capability-ontology.yaml`` so the
system can be extended without touching Python code.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_ONTOLOGY_YAML = Path(__file__).resolve().parents[2] / "configs" / "capability-ontology.yaml"

# ── Taxonomy maps ─────────────────────────────────────────────────────────────
# alias → canonical capability name
_alias_map: dict[str, str] = {}
# canonical → parent canonical (None for roots)
_parent_map: dict[str, str | None] = {}
# canonical → category string
_category_map: dict[str, str] = {}

# ── Channel definitions ───────────────────────────────────────────────────────
# channel_id → {direction, device_types, description}
_channel_defs: dict[str, dict[str, Any]] = {}

# ── Enables rules ─────────────────────────────────────────────────────────────
# capability_id → {required_channels: [...], maps_to: str|None, description}
_enables_rules: dict[str, dict[str, Any]] = {}

# ── Collaboration modes ───────────────────────────────────────────────────────
# mode_key → {capability_mode, min_channels, attention_cost_base, bt_pattern_default, …}
_collab_modes: dict[str, dict[str, Any]] = {}

# ── BT patterns ───────────────────────────────────────────────────────────────
# pattern_key → {description, structure}
_bt_patterns: dict[str, dict[str, Any]] = {}

# ── Utility weights ───────────────────────────────────────────────────────────
_utility_weights: dict[str, dict[str, float]] = {}
_attention_budget: dict[str, float] = {}

# ── Param defs ────────────────────────────────────────────────────────────────
# canonical_capability → list of param_def dicts
_param_defs_map: dict[str, list[dict[str, Any]]] = {}

# ── Skill Model extensions ────────────────────────────────────────────────────
# canonical → {type, required, source, input_mode, default_value, description, enum}
_input_schema_map: dict[str, dict[str, dict[str, Any]]] = {}
# canonical → [precondition_key, ...]
_preconditions_map: dict[str, list[str]] = {}
# canonical → [effect_key, ...]
_effects_map: dict[str, list[str]] = {}
# canonical → UE actionType string
_ue_action_type_map: dict[str, str] = {}
# canonical → {top_level: [...], params: [...]}
_ue_param_layout_map: dict[str, dict[str, list[str]]] = {}
# canonical → {channel_combo: quality_float}
_channel_quality_map: dict[str, dict[str, float]] = {}


# ── Internal builders ─────────────────────────────────────────────────────────

def _build_taxonomy(taxonomy: dict[str, Any]) -> None:
    _alias_map.clear()
    _parent_map.clear()
    _category_map.clear()
    _param_defs_map.clear()
    _input_schema_map.clear()
    _preconditions_map.clear()
    _effects_map.clear()
    _ue_action_type_map.clear()
    _ue_param_layout_map.clear()
    _channel_quality_map.clear()

    for category, caps in taxonomy.items():
        if not isinstance(caps, dict):
            continue
        for cap_name, cap_meta in caps.items():
            meta = cap_meta if isinstance(cap_meta, dict) else {}
            canonical = cap_name.lower()
            _alias_map[canonical] = canonical
            _parent_map[canonical] = (meta.get("parent") or "").lower() or None
            _category_map[canonical] = category
            for alias in meta.get("aliases", []):
                _alias_map[alias.lower()] = canonical
            if "param_defs" in meta:
                _param_defs_map[canonical] = list(meta["param_defs"])
            if "input_schema" in meta and isinstance(meta["input_schema"], dict):
                _input_schema_map[canonical] = dict(meta["input_schema"])
            if "preconditions" in meta:
                _preconditions_map[canonical] = list(meta["preconditions"])
            if "effects" in meta:
                _effects_map[canonical] = list(meta["effects"])
            if "ue_action_type" in meta:
                _ue_action_type_map[canonical] = str(meta["ue_action_type"])
            if "ue_param_layout" in meta and isinstance(meta["ue_param_layout"], dict):
                _ue_param_layout_map[canonical] = {
                    "top_level": list(meta["ue_param_layout"].get("top_level", [])),
                    "params": list(meta["ue_param_layout"].get("params", [])),
                }
            if "channel_quality_map" in meta and isinstance(meta["channel_quality_map"], dict):
                _channel_quality_map[canonical] = {
                    str(k): float(v) for k, v in meta["channel_quality_map"].items()
                }


def _build_channels(channels: dict[str, Any]) -> None:
    _channel_defs.clear()
    for ch_id, ch_meta in channels.items():
        _channel_defs[ch_id] = dict(ch_meta) if isinstance(ch_meta, dict) else {}


def _build_enables_rules(rules: dict[str, Any]) -> None:
    _enables_rules.clear()
    for cap_id, rule_meta in rules.items():
        if not isinstance(rule_meta, dict):
            continue
        # Resolve maps_to aliases so the resolver always gets the canonical cap id
        maps_to = rule_meta.get("maps_to")
        canonical_cap = resolve_alias(maps_to) if maps_to else resolve_alias(cap_id)
        _enables_rules[cap_id] = {
            "capability_id": canonical_cap,
            "required_channels": rule_meta.get("required_channels", []),
            "description": rule_meta.get("description", ""),
        }


def _build_collab_modes(modes: dict[str, Any]) -> None:
    _collab_modes.clear()
    for mode_key, mode_meta in modes.items():
        if isinstance(mode_meta, dict):
            _collab_modes[mode_key] = dict(mode_meta)


def _build_bt_patterns(patterns: dict[str, Any]) -> None:
    _bt_patterns.clear()
    for key, meta in patterns.items():
        if isinstance(meta, dict):
            _bt_patterns[key] = dict(meta)


# ── Public loader ─────────────────────────────────────────────────────────────

def load_ontology(path: Path | str | None = None) -> None:
    """(Re-)load the full ontology from YAML."""
    p = Path(path) if path else _ONTOLOGY_YAML
    if not p.exists():
        logger.warning("Ontology file %s not found — using empty taxonomy", p)
        return
    with open(p, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    _build_taxonomy(data.get("taxonomy", {}))
    _build_channels(data.get("channels", {}))
    # enables_rules must be built AFTER taxonomy (resolve_alias depends on it)
    _build_enables_rules(data.get("enables_rules", {}))
    _build_collab_modes(data.get("collaboration_modes", {}))
    _build_bt_patterns(data.get("bt_patterns", {}))

    uw = data.get("utility_weights", {})
    _utility_weights.clear()
    _utility_weights.update({k: dict(v) for k, v in uw.items() if k != "attention_budget"})
    ab = uw.get("attention_budget", {})
    _attention_budget.clear()
    _attention_budget.update({k: float(v) for k, v in ab.items()})

    logger.info(
        "Loaded ontology: %d capabilities, %d channels, %d enables_rules, %d collab_modes",
        len(_parent_map), len(_channel_defs), len(_enables_rules), len(_collab_modes),
    )


# ── Taxonomy API ──────────────────────────────────────────────────────────────

def resolve_alias(name: str) -> str:
    """Map any name (including aliases) to its canonical capability name."""
    return _alias_map.get(name.lower(), name.lower())


def is_subcapability(child: str, parent: str) -> bool:
    """Return True if *child* is a (transitive) sub-capability of *parent*."""
    c = resolve_alias(child)
    p = resolve_alias(parent)
    visited: set[str] = set()
    cur: str | None = c
    while cur and cur not in visited:
        if cur == p:
            return True
        visited.add(cur)
        cur = _parent_map.get(cur)
    return False


def capability_similarity(cap_a: str, cap_b: str) -> float:
    """Heuristic similarity ∈ [0, 1] based on taxonomy distance."""
    a = resolve_alias(cap_a)
    b = resolve_alias(cap_b)
    if a == b:
        return 1.0
    if is_subcapability(a, b) or is_subcapability(b, a):
        return 0.8
    if _category_map.get(a) and _category_map.get(a) == _category_map.get(b):
        return 0.5
    return 0.0


def get_category(cap_name: str) -> str | None:
    return _category_map.get(resolve_alias(cap_name))


def all_capabilities() -> list[str]:
    return sorted(_parent_map.keys())


# ── Channel API ───────────────────────────────────────────────────────────────

def get_channel_def(channel_id: str) -> dict[str, Any]:
    return _channel_defs.get(channel_id, {})


def all_channels() -> list[str]:
    return sorted(_channel_defs.keys())


# ── Enables rules API ─────────────────────────────────────────────────────────

def get_enables_rules() -> dict[str, dict[str, Any]]:
    """Return the full enables-rules dict (rule_key → {capability_id, required_channels})."""
    return _enables_rules


def capabilities_from_channels(available: set[str]) -> set[str]:
    """Return canonical capability IDs that are fully unlocked by *available* channels."""
    unlocked: set[str] = set()
    for rule in _enables_rules.values():
        required: list[str] = rule.get("required_channels", [])
        if not required:
            continue
        if set(required) <= available:
            unlocked.add(resolve_alias(rule["capability_id"]))
    return unlocked


# ── Collaboration modes API ───────────────────────────────────────────────────

def get_collaboration_modes() -> dict[str, dict[str, Any]]:
    return _collab_modes


def mode_from_capability_mode(capability_mode: str) -> str:
    """Map CapabilityMode string to collaboration mode key.

    e.g. ``"MODE_AUTONOMOUS"`` → ``"task_based"``
    """
    mapping = {
        "MODE_AUTONOMOUS":     "task_based",
        "autonomous":          "task_based",
        "MODE_SUPERVISED":     "partner",
        "supervised":          "partner",
        "MODE_REMOTE_CONTROL": "proxy",
        "remote_control":      "proxy",
    }
    return mapping.get(capability_mode, "task_based")


def attention_cost(collab_mode_key: str) -> float:
    """Return the base attention cost for a collaboration mode key."""
    mode = _collab_modes.get(collab_mode_key, {})
    return float(mode.get("attention_cost_base", 0.0))


def default_bt_pattern(collab_mode_key: str) -> str:
    """Return the default bt_pattern for a collaboration mode key."""
    mode = _collab_modes.get(collab_mode_key, {})
    return mode.get("bt_pattern_default", "autonomous")


def get_bt_patterns() -> dict[str, dict[str, Any]]:
    return _bt_patterns


# ── Param defs API ────────────────────────────────────────────────────────────

def get_param_defs(cap_name: str) -> list[dict[str, Any]]:
    """Return the param_defs list for a capability (by canonical name or alias).

    Returns an empty list if the capability has no param_defs declared.
    """
    canonical = resolve_alias(cap_name)
    return _param_defs_map.get(canonical, [])


def needs_human_input(cap_name: str, provided_params: dict[str, Any] | None = None) -> bool:
    """Return True if *cap_name* has a required param whose source includes 'human'
    and that param is not already present in *provided_params*.
    """
    provided = provided_params or {}
    for pd in get_param_defs(cap_name):
        if not pd.get("required", False):
            continue
        sources = [s.strip() for s in str(pd.get("source", "")).split("|")]
        if "human" in sources and pd["name"] not in provided:
            return True
    return False


# ── Skill Model API ───────────────────────────────────────────────────────────

def get_skill_descriptor(skill_name: str) -> dict[str, Any]:
    """Return the full skill descriptor for a capability (by canonical name or alias).

    Includes input_schema, preconditions, effects, ue_action_type, ue_param_layout,
    and channel_quality_map.
    """
    canonical = resolve_alias(skill_name)
    return {
        "name": canonical,
        "category": _category_map.get(canonical),
        "input_schema": _input_schema_map.get(canonical, {}),
        "preconditions": _preconditions_map.get(canonical, []),
        "effects": _effects_map.get(canonical, []),
        "ue_action_type": _ue_action_type_map.get(canonical),
        "ue_param_layout": _ue_param_layout_map.get(canonical),
        "channel_quality_map": _channel_quality_map.get(canonical),
    }


def get_input_schema(skill_name: str) -> dict[str, dict[str, Any]]:
    """Return the input_schema dict for a capability.

    Each key is a parameter name, value is {type, required, source, input_mode,
    default_value, description, enum}.
    """
    return _input_schema_map.get(resolve_alias(skill_name), {})


def get_preconditions(skill_name: str) -> list[str]:
    """Return the list of precondition keys for a capability."""
    return _preconditions_map.get(resolve_alias(skill_name), [])


def get_effects(skill_name: str) -> list[str]:
    """Return the list of effect keys produced when a capability succeeds."""
    return _effects_map.get(resolve_alias(skill_name), [])


def get_ue_action_type(skill_name: str) -> str | None:
    """Return the UE actionType string for a capability, or None."""
    return _ue_action_type_map.get(resolve_alias(skill_name))


def get_ue_param_layout(skill_name: str) -> dict[str, list[str]]:
    """Return the UE param layout: {top_level: [...], params: [...]}."""
    return _ue_param_layout_map.get(resolve_alias(skill_name), {"top_level": [], "params": []})


def get_channel_quality_map(skill_name: str) -> dict[str, float] | None:
    """Return the channel_quality_map for a capability, or None."""
    return _channel_quality_map.get(resolve_alias(skill_name))


def check_preconditions(
    skill_name: str,
    agent_state: dict[str, Any] | None = None,
    blackboard_storage: dict[str, Any] | None = None,
) -> list[str]:
    """Check preconditions for a skill against agent state and blackboard.

    Returns a list of unsatisfied precondition keys (empty = all satisfied).
    """
    preconds = get_preconditions(skill_name)
    if not preconds:
        return []
    state = agent_state or {}
    bb = blackboard_storage or {}
    unsatisfied: list[str] = []
    for pc in preconds:
        if pc == "entity_idle":
            entity_state = state.get("state", "ELS_IDLE")
            if entity_state not in ("ELS_IDLE", "idle"):
                unsatisfied.append(pc)
        elif pc == "detection_service_available":
            if not bb.get("detection_service_available"):
                pass  # soft precondition — don't block
        else:
            bb_val = bb.get(pc)
            if not bb_val:
                unsatisfied.append(pc)
    return unsatisfied


# ── Utility weights API ───────────────────────────────────────────────────────

def get_utility_weights() -> dict[str, dict[str, float]]:
    return _utility_weights


def get_attention_budget() -> dict[str, float]:
    return _attention_budget


# ── Auto-load on import ───────────────────────────────────────────────────────
load_ontology()
