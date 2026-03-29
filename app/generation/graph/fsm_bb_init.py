"""FSM & Blackboard Initializer node — template-based, no LLM."""

from __future__ import annotations

import logging
import math
from typing import Any

from app.generation.graph.progress import publish_step
from app.generation.graph.state import GenerationState

logger = logging.getLogger(__name__)

# ── FSM templates ─────────────────────────────────────────────────────────────

ROBOT_FSM_TEMPLATE = {
    "entity_type": "robot",
    "initial_state": "idle",
}

HUMAN_FSM_TEMPLATE = {
    "entity_type": "human",
    "initial_state": "idle",
}


def _generate_circle_waypoints(
    center: dict, radius: float, n_points: int = 8, z_range: dict | None = None,
) -> list[dict]:
    """Generate evenly-spaced patrol waypoints around a circle (at 80% radius for safety margin)."""
    z = 0.0
    if z_range:
        z = (z_range.get("min", 0) + z_range.get("max", 0)) / 2
    elif "z" in center:
        z = center["z"]

    r = radius * 0.8
    cx, cy = center["x"], center["y"]
    return [
        {
            "x": round(cx + r * math.cos(2 * math.pi * i / n_points), 1),
            "y": round(cy + r * math.sin(2 * math.pi * i / n_points), 1),
            "z": z,
        }
        for i in range(n_points)
    ]


def _tailor_fsm(template: dict, bt: dict, entity: dict) -> dict:
    """Produce a FSMDefinition dict for one entity."""
    return {
        "entity_id": entity["entity_id"],
        "entity_type": entity.get("entity_type") or entity.get("type", "robot"),
        "initial_state": template["initial_state"],
        "extra_transitions": [],
    }


def _collect_blackboard_keys(
    bt: dict,
    entities: list[dict],
    environment: dict,
    task_context: dict | None = None,
    task_plan: dict | None = None,
) -> dict:
    """
    Derive initial blackboard entries from:
    - entity states (position, status, comm_status)
    - environment zones
    - condition node expected values
    - task_context (mission-level info)
    - subtask params (structured parameters extracted by Task Planner)
    """
    entries: dict = {}

    # Entity baseline
    for e in entities:
        eid = e.get("entity_id", "")
        entries[f"entities/{eid}/status"] = e.get("status", "idle")
        entries[f"entities/{eid}/comm_status"] = e.get("comm_status", "online")
        entries[f"entities/{eid}/position"] = e.get("position", {})
        entries[f"entities/{eid}/fsm_state"] = "idle"

    # Zone baseline — seed zone center (with z), patrol waypoints, semantics
    raw_zones = environment.get("zones", {})
    # Normalize: SSSG sends array, HMTA expects dict keyed by id
    zones: dict[str, Any] = {}
    if isinstance(raw_zones, list):
        for z in raw_zones:
            zid = z.get("id", f"zone_{len(zones)}")
            zones[zid] = z
    elif isinstance(raw_zones, dict):
        zones = raw_zones

    from app.execution.param_resolver import ParamResolver
    ParamResolver.set_zone_registry(zones)

    for zone_id, zone_data in zones.items():
        entries[f"zones/{zone_id}/explored"] = False
        entries[f"zones/{zone_id}/cleared"] = False
        entries[f"zones/{zone_id}/coverage_status"] = "unchecked"
        entries[f"zones/{zone_id}/data"] = zone_data

        # Center with z derived from z_range midpoint
        center = zone_data.get("center")
        if center:
            z_range = zone_data.get("z_range", {})
            z_val = center.get("z")
            if z_val is None and z_range:
                z_val = (z_range.get("min", 0) + z_range.get("max", 0)) / 2
            full_center = {"x": center["x"], "y": center["y"], "z": z_val or 0}
            entries[f"zones/{zone_id}/center"] = full_center

        # Patrol waypoints: use provided ones, or auto-generate for circle zones
        zone_pts = zone_data.get("patrolPoints", [])
        if zone_pts:
            entries[f"zones/{zone_id}/waypoints"] = [
                p.get("position", p) for p in zone_pts
            ]
        elif zone_data.get("shape") == "circle" and center and zone_data.get("radius"):
            waypoints = _generate_circle_waypoints(center, zone_data["radius"], z_range=zone_data.get("z_range"))
            entries[f"zones/{zone_id}/waypoints"] = waypoints
        elif zone_data.get("boundary_2d"):
            entries[f"zones/{zone_id}/waypoints"] = [
                {"x": p["x"], "y": p["y"], "z": (zone_data.get("z_range", {}).get("min", 0) + zone_data.get("z_range", {}).get("max", 0)) / 2}
                for p in zone_data["boundary_2d"]
            ]

        # Scan grid (Roomba-style cell map) + coverage path
        from app.execution.param_resolver import generate_scan_grid
        grid_cells, scan_path = generate_scan_grid(zone_data)
        if grid_cells:
            entries[f"zones/{zone_id}/grid_cells"] = grid_cells
            entries[f"zones/{zone_id}/scan_waypoints"] = scan_path

    # Mission-level goal keys
    entries["bomb_detected"] = False
    entries["all_zones_cleared"] = False
    entries["bomb_location"] = {}

    # Structured mission_goal from Goal Extractor (used by GoalConfirmationGate
    # for detection confirmation, EntityWorker for early termination, and
    # MissionMonitor for completion semantics).
    # Prefer state-level mission_goal (from goal_extractor); fall back to task_plan.
    mission_goal_val = None
    if task_plan:
        mission_goal_val = task_plan.get("mission_goal")
    if mission_goal_val:
        entries["mission_goal"] = mission_goal_val

    # Topology: zone adjacency graph
    topology = environment.get("topology", [])
    if topology:
        entries["topology"] = topology
        adj: dict[str, list[str]] = {}
        for edge in topology:
            f, t = edge.get("from_zone", ""), edge.get("to_zone", "")
            adj.setdefault(f, []).append(t)
            adj.setdefault(t, []).append(f)
        entries["zone_adjacency"] = adj

    # Global patrol points → preset_waypoints (fallback for patrol/follow_by_path)
    patrol_points = environment.get("patrol_points", [])
    if patrol_points:
        all_waypoints = [p.get("position", p) for p in patrol_points if p.get("position")]
        if all_waypoints:
            entries["preset_waypoints"] = all_waypoints
            entries["waypoints"] = all_waypoints

    # Landmarks & POIs → named locations for navigation targets
    for lm in environment.get("landmarks", []):
        lm_id = lm.get("id") or lm.get("name", "")
        if lm_id and lm.get("position"):
            entries[f"landmarks/{lm_id}/position"] = lm["position"]
            entries[f"landmarks/{lm_id}/data"] = lm

    # Map bounds
    bounds = environment.get("bounds")
    if bounds:
        entries["map_bounds"] = bounds

    # Task context → flat keys under "task_context/"
    if task_context:
        entries["task_context"] = task_context
        for k, v in task_context.items():
            entries[f"task_context/{k}"] = v

    # Subtask params → flat preset keys so ParamResolver "preset" source can find them.
    # Each subtask's params are merged; later subtasks overwrite earlier ones for same key.
    if task_plan:
        all_subtask_params: dict = {}
        for st in task_plan.get("subtasks", []):
            st_params = st.get("params") or {}
            all_subtask_params.update(st_params)
        for k, v in all_subtask_params.items():
            entries[f"preset_{k}"] = v
            if k not in entries:
                entries[k] = v

    # Scan condition nodes for expected values
    for node in (bt.get("nodes") or {}).values():
        if node.get("type") == "condition" and node.get("key") and node.get("expected") is not None:
            key = node["key"]
            if not isinstance(key, str):
                key = str(key)
            if key not in entries:
                entries[key] = None   # placeholder; real value set at runtime

    return entries


# ── Task-level preconditions / effects (business rules) ──────────────────────

_NAV_INTENTS = {"navigation", "navigate", "move", "move_to"}
_SCAN_INTENTS = {"scan", "detect", "reconnaissance", "area_scan"}


def _derive_task_conditions(
    intent: str, zone_id: str,
) -> tuple[list[dict], list[dict]]:
    """Generate declarative preconditions and effects for a task.

    Business rules live HERE, not in EntityWorker.  The worker evaluates
    these generically via BB key lookups without understanding their
    semantics.
    """
    pc: list[dict] = []
    eff: list[dict] = []

    if not zone_id:
        return pc, eff

    intent_lower = intent.lower()

    if intent_lower in _SCAN_INTENTS or intent_lower in _NAV_INTENTS:
        pc.append({"key": f"zones/{zone_id}/cleared", "expect": False})

    if intent_lower in _SCAN_INTENTS:
        eff.append({"key": f"zones/{zone_id}/cleared", "value": True})
        eff.append({"key": f"zones/{zone_id}/coverage_status",
                     "value": {"status": "scanned"}})

    return pc, eff


# ── Task Queue builder ────────────────────────────────────────────────────────

def _build_task_queue(
    task_plan: dict,
    capability_graph_dict: dict | None,
) -> list[dict]:
    """Convert allocator subtasks + hypergraph depends_on edges into a flat task queue.

    Each task queue item:
        id          : str  — unique task id (subtask_id / task_id from plan)
        intent      : str  — capability id (navigation, scan, disarm …)
        entity      : str  — assigned entity_id
        params      : dict — forwarded from subtask params
        status      : str  — "pending"
        depends_on  : list[str]  — task ids this task must wait for
        bt_pattern  : str  — interaction pattern (autonomous | supervised_fallback | …)
        human_supervisor : str | None
        description : str  — human-readable label
    """
    from app.capability.hypergraph import HyperGraph

    subtasks: list[dict] = task_plan.get("subtasks", task_plan.get("phases", []))
    if not subtasks:
        return []

    # Reconstruct hypergraph to query depends_on edges
    graph: HyperGraph | None = None
    if capability_graph_dict:
        try:
            graph = HyperGraph.from_dict(capability_graph_dict)
        except Exception as exc:
            logger.warning("[fsm_bb_init] could not rebuild HyperGraph: %s", exc)

    # Build depends_on lookup from graph edges
    # Edge attrs carry 'dependent' and 'provider'; fall back to node set
    dep_map: dict[str, list[str]] = {}  # task_id → [provider_task_id, ...]
    if graph:
        for edge in graph.edges.values():
            if edge.kind != "depends_on":
                continue
            dependent = edge.attrs.get("dependent")
            provider = edge.attrs.get("provider")
            if dependent and provider:
                dep_map.setdefault(dependent, []).append(provider)
            else:
                # Infer from node set (legacy)
                nodes_list = list(edge.nodes)
                if len(nodes_list) == 2:
                    dep_map.setdefault(nodes_list[0], []).append(nodes_list[1])

    queue: list[dict] = []
    for i, st in enumerate(subtasks):
        tid = st.get("task_id") or st.get("id") or f"t{i+1}"
        intent = st.get("intent") or st.get("capability") or ""
        if not intent:
            req_caps = st.get("required_capabilities") or []
            if req_caps:
                # Use the last required capability as the primary intent
                # (e.g. ["navigation", "scan"] → "scan"; navigation is
                # the means, scan is the goal).
                intent = req_caps[-1] if isinstance(req_caps[-1], str) else ""
        assigned_ids = (
            st.get("assigned_entity_ids")
            or ([st["assigned"]] if isinstance(st.get("assigned"), str) else st.get("assigned"))
            or []
        )
        interaction = st.get("interaction") or {}
        bt_pattern = interaction.get("bt_pattern") or st.get("bt_pattern") or "supervised_fallback"
        human_supervisor = interaction.get("human_supervisor") or st.get("human_supervisor")
        params = dict(st.get("params") or {})
        description = st.get("description") or st.get("name") or f"{intent} ({tid})"

        # One task queue item per assigned entity
        entity = assigned_ids[0] if assigned_ids else "unknown"
        depends_on = dep_map.get(tid, [])
        # Also honour explicit depends_on in subtask
        explicit_dep = st.get("depends_on")
        if explicit_dep and explicit_dep not in depends_on:
            depends_on = [*depends_on, explicit_dep]

        req_caps = st.get("required_capabilities") or []
        zone_id = st.get("zone_id") or params.get("zone_id") or ""

        preconditions, effects = _derive_task_conditions(intent, zone_id)

        queue.append({
            "id": tid,
            "intent": intent,
            "entity": entity,
            "params": params,
            "required_capabilities": req_caps,
            "zone_id": zone_id,
            "status": "pending",
            "depends_on": depends_on,
            "preconditions": preconditions,
            "effects": effects,
            "bt_pattern": bt_pattern,
            "human_supervisor": human_supervisor,
            "description": description,
            "started_at": None,
            "completed_at": None,
            "failure_reason": None,
        })

    logger.info(
        "[fsm_bb_init] task_queue: %d tasks built (%d with dependencies)",
        len(queue), sum(1 for t in queue if t["depends_on"]),
    )
    return queue


# ── LangGraph node ────────────────────────────────────────────────────────────

def fsm_bb_init_node(state: GenerationState) -> dict:
    """LangGraph node: FSM & Blackboard Initializer."""
    task_id = state.get("task_id", "unknown")
    bt = state.get("behavior_tree", {})
    entities = state.get("entities", [])
    environment = state.get("environment", {})
    task_context = state.get("task_context") or {}
    task_plan = state.get("task_plan") or {}
    capability_graph_dict = state.get("capability_graph") or None

    logger.info("[%s] fsm_bb_init started", task_id)
    publish_step("fsm_bb_init", "started", message="初始化 FSM 与黑板…")

    fsm_defs = []
    for entity in entities:
        etype = entity.get("entity_type") or entity.get("type", "robot")
        template = ROBOT_FSM_TEMPLATE if etype == "robot" else HUMAN_FSM_TEMPLATE
        fsm_defs.append(_tailor_fsm(template, bt, entity))

    bb_entries = _collect_blackboard_keys(
        bt, entities, environment,
        task_context=task_context,
        task_plan=task_plan,
    )

    # Inject state-level mission_goal (from goal_extractor) — overrides task_plan
    state_goal = state.get("mission_goal")
    if state_goal:
        bb_entries["mission_goal"] = state_goal

    # Build task queue from allocator output + hypergraph deps
    task_queue = _build_task_queue(task_plan, capability_graph_dict)
    bb_entries["task_queue"] = task_queue

    trace_entry = {
        "step": "fsm_bb_init",
        "status": "completed",
        "fsm_count": len(fsm_defs),
        "bb_key_count": len(bb_entries),
        "task_queue_count": len(task_queue),
    }

    logger.info(
        "[%s] fsm_bb_init done — %d FSMs, %d BB keys, %d tasks in queue",
        task_id, len(fsm_defs), len(bb_entries), len(task_queue),
    )
    publish_step("fsm_bb_init", "completed",
                 message=f"{len(fsm_defs)} 个 FSM, {len(bb_entries)} 个黑板键, {len(task_queue)} 个任务")

    return {
        "fsm_definitions": fsm_defs,
        "blackboard_init": {"entries": bb_entries},
        "generation_trace": [*state.get("generation_trace", []), trace_entry],
    }
