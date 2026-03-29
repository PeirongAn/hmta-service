"""Coverage Ensurer — LLM-driven zone coverage gap filler.

Reads the ``coverage_policy`` and ``mission_goal`` from the Task Planner's
output and cross-references with the environment zones.  Any zones that
fall within the declared scope but are NOT covered by existing subtasks
are automatically filled with scan subtasks.

Pipeline position: **task_planner → coverage_ensurer → allocator**
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from typing import Any

from app.generation.graph.progress import publish_step
from app.generation.graph.state import GenerationState

logger = logging.getLogger(__name__)


# ── Zone extraction helpers ──────────────────────────────────────────────────

def _extract_zones(env: dict) -> list[dict]:
    """Normalize environment zones into a flat list of dicts with 'id'."""
    zones_raw = env.get("zones", {})
    zones: list[dict] = []
    if isinstance(zones_raw, list):
        for z in zones_raw:
            zones.append({**z, "id": z.get("id", z.get("zone_id", ""))})
    elif isinstance(zones_raw, dict):
        for zid, z in zones_raw.items():
            zones.append({**z, "id": zid})
    return zones


def _zone_floor(z: dict) -> str:
    return str(z.get("floor_name") or z.get("floor") or "").strip()


def _zone_risk(z: dict) -> str:
    sem = z.get("semantics", {})
    return str(sem.get("risk_level") or z.get("risk") or "").strip().lower()


def _is_navigable(z: dict) -> bool:
    return z.get("navigable", True) is not False


# ── Scope resolution ─────────────────────────────────────────────────────────

def _apply_scope(
    all_zones: list[dict],
    zone_scope: str,
    explicit_ids: list[str] | None,
) -> list[dict]:
    """Filter zones according to the LLM's ``zone_scope`` declaration."""

    scope = zone_scope.strip().lower() if zone_scope else "all_navigable"

    if scope == "all_navigable" or scope == "all":
        return [z for z in all_zones if _is_navigable(z)]

    if scope == "zone_ids" and explicit_ids:
        id_set = set(explicit_ids)
        return [z for z in all_zones if z["id"] in id_set]

    # "floor:1楼" / "floor:2F" patterns
    m = re.match(r"floor[:\s](.+)", scope, re.IGNORECASE)
    if m:
        floor_val = m.group(1).strip()
        return [
            z for z in all_zones
            if _is_navigable(z) and floor_val in _zone_floor(z)
        ]

    # "risk:high" / "risk:critical" patterns
    m = re.match(r"risk[:\s](.+)", scope, re.IGNORECASE)
    if m:
        risk_val = m.group(1).strip().lower()
        targets = {risk_val}
        if risk_val == "high":
            targets.add("critical")
        return [
            z for z in all_zones
            if _is_navigable(z) and _zone_risk(z) in targets
        ]

    # Unknown scope — fall back to all navigable
    logger.warning(
        "[coverage_ensurer] Unknown zone_scope '%s', falling back to all_navigable",
        zone_scope,
    )
    return [z for z in all_zones if _is_navigable(z)]


# ── Inference helpers ────────────────────────────────────────────────────────

def _infer_capabilities(subtasks: list[dict]) -> list[str]:
    """Pick the most common ``required_capabilities`` pattern from existing subtasks."""
    counts: Counter[tuple[str, ...]] = Counter()
    for st in subtasks:
        caps = st.get("required_capabilities") or []
        if caps:
            counts[tuple(caps)] += 1
    if counts:
        return list(counts.most_common(1)[0][0])
    return ["navigation", "scan"]


def _infer_shared_params(subtasks: list[dict]) -> dict[str, Any]:
    """Extract params that appear in ALL existing subtasks (minus zone_id)."""
    shared: dict[str, Any] = {}
    if not subtasks:
        return shared
    first_params = dict((subtasks[0].get("params") or {}))
    first_params.pop("zone_id", None)
    first_params.pop("end", None)
    for k, v in first_params.items():
        if all(
            (st.get("params") or {}).get(k) == v
            for st in subtasks[1:]
        ):
            shared[k] = v
    return shared


# ── LangGraph node ───────────────────────────────────────────────────────────

def coverage_ensurer_node(state: GenerationState) -> dict:
    """LangGraph node: Coverage Ensurer.

    Reads LLM-produced ``coverage_policy`` and ``mission_goal``, compares
    against environment zones, and auto-generates subtasks for any zones
    that fall within scope but were not explicitly planned by the LLM.
    """
    task_plan = dict(state.get("task_plan") or {})
    env = state.get("environment", {})
    task_id = state.get("task_id", "unknown")

    publish_step("coverage_ensurer", "started", message="覆盖策略校验中…")

    # 1. Read LLM's coverage policy
    policy = task_plan.get("coverage_policy") or {}
    zone_scope = policy.get("zone_scope", "all_navigable")

    # 2. Resolve target zones from environment
    all_zones = _extract_zones(env)
    target_zones = _apply_scope(all_zones, zone_scope, policy.get("zone_ids"))

    # 3. Collect zones already covered by LLM subtasks
    subtasks = list(task_plan.get("subtasks", []))
    planned_zone_ids: set[str] = set()
    for st in subtasks:
        zid = (st.get("params") or {}).get("zone_id") or st.get("zone_id") or ""
        if zid:
            planned_zone_ids.add(zid)

    # 4. Determine missing zones
    missing = [z for z in target_zones if z["id"] not in planned_zone_ids]

    if not missing:
        logger.info(
            "[%s] coverage_ensurer: all %d target zones already planned (scope=%s)",
            task_id, len(target_zones), zone_scope,
        )
        publish_step(
            "coverage_ensurer", "completed",
            message=f"所有 {len(target_zones)} 个目标区域已规划，无需补全",
        )
        return {"task_plan": task_plan}

    # 5. Infer template from existing subtasks
    template_caps = _infer_capabilities(subtasks)
    template_params = _infer_shared_params(subtasks)

    # 6. Generate fill subtasks
    for z in missing:
        fill_params = {**template_params, "zone_id": z["id"]}
        subtasks.append({
            "subtask_id": f"auto_{z['id']}",
            "description": f"扫描 {z.get('name') or z.get('display_name') or z['id']}",
            "required_capabilities": list(template_caps),
            "params": fill_params,
            "dependencies": [],
            "assigned_entity_ids": [],
            "termination": {"type": "natural"},
            "is_concurrent": True,
            "_auto_generated": True,
        })

    task_plan["subtasks"] = subtasks

    logger.info(
        "[%s] coverage_ensurer: scope=%s, target=%d zones, planned=%d, "
        "auto-filled=%d (caps=%s)",
        task_id, zone_scope, len(target_zones),
        len(planned_zone_ids), len(missing), template_caps,
    )
    publish_step(
        "coverage_ensurer", "completed",
        message=f"已补全 {len(missing)} 个遗漏区域（共 {len(target_zones)} 个目标区域）",
    )

    return {"task_plan": task_plan}
