"""Safety constraint checks (no LLM, pure rule engine)."""

from __future__ import annotations


def check_safety_constraints(bt: dict, entities: list[dict], task_ctx: dict) -> list[dict]:
    """
    Basic safety rules:
    - Offline entities must not receive action nodes.
    - Critical tasks must have at least one humanGate before final action.
    """
    violations = []
    offline = {e["entity_id"] for e in entities if e.get("status") == "offline"}
    priority = task_ctx.get("priority", "normal")

    has_human_gate = any(n.get("type") == "humanGate" for n in (bt.get("nodes") or {}).values())

    for node_id, node in (bt.get("nodes") or {}).items():
        if node.get("type") == "action" and node.get("entity") in offline:
            violations.append({
                "rule": "offline_entity_assigned",
                "node_id": node_id,
                "entity_id": node.get("entity"),
                "message": f"Entity '{node.get('entity')}' is offline but assigned an action",
            })

    if priority == "critical" and not has_human_gate:
        violations.append({
            "rule": "missing_human_gate_for_critical",
            "message": "Critical priority task must include at least one humanGate approval node",
        })

    return violations
