"""Rule: every action/humanGate node's intent must be in the entity's capability list."""

from __future__ import annotations


def check_capability_match(bt: dict, entities: list[dict]) -> list[dict]:
    """Return violation dicts for action/humanGate nodes with mismatched capabilities.

    Matching logic (in order):
      1. Exact match on capability id or name
      2. Case-insensitive match
      3. Prefix/containment match (e.g. "navigate" matches "navigation")

    Entities with an empty capabilities list are considered "unrestricted" and
    are skipped in capability checking.
    """
    capability_map: dict[str, list[dict]] = {}
    for e in entities:
        eid = e.get("entity_id", "")
        raw_caps = e.get("capabilities", [])
        caps: list[dict] = []
        for c in raw_caps:
            if isinstance(c, str):
                caps.append({"id": c, "name": c})
            elif isinstance(c, dict):
                caps.append({"id": c.get("id", ""), "name": c.get("name", "")})
        if caps:
            capability_map[eid] = caps

    violations = []
    for node_id, node in (bt.get("nodes") or {}).items():
        if node.get("type") not in ("action", "humanGate"):
            continue
        entity_id = node.get("entity")
        intent = node.get("intent")
        if not entity_id or not intent:
            continue
        if entity_id not in capability_map:
            continue
        if not _matches_any_capability(intent, capability_map[entity_id]):
            violations.append({
                "rule": "capability_mismatch",
                "node_id": node_id,
                "node_name": node.get("name"),
                "entity_id": entity_id,
                "intent": intent,
                "available": [f"{c['id']}({c['name']})" for c in capability_map[entity_id]],
                "message": f"Entity '{entity_id}' does not have capability '{intent}'",
            })
    return violations


def _matches_any_capability(intent: str, caps: list[dict]) -> bool:
    """Check if intent matches any capability via exact, case-insensitive, or fuzzy match."""
    intent_lower = intent.lower().strip()

    for cap in caps:
        cap_id = (cap.get("id") or "").strip()
        cap_name = (cap.get("name") or "").strip()

        if intent == cap_id or intent == cap_name:
            return True

        cap_id_lower = cap_id.lower()
        cap_name_lower = cap_name.lower()
        if intent_lower == cap_id_lower or intent_lower == cap_name_lower:
            return True

        if (intent_lower in cap_id_lower or cap_id_lower in intent_lower or
                intent_lower in cap_name_lower or cap_name_lower in intent_lower):
            return True

    return False
