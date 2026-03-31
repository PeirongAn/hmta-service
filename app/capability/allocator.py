"""LangGraph node: quantitative entity-task allocator with joint optimisation.

Inserted between ``task_planner`` and ``bt_builder``.

Key improvements over the naive per-task greedy allocator:
1. Derives human effective capabilities via EffectiveCapabilityResolver
   (device → channel → capability chain) before scoring.
2. Outputs an ``interaction`` block per subtask:
   {mode, collaboration, human_supervisor, attention_cost, bt_pattern}
3. Respects a global human attention budget — high-priority subtasks
   lock down budget first; low-priority ones fall back to task_based.
4. Filters human supervisors by device compatibility with required mode.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.capability.effective_resolver import EffectiveCapabilityResolver
from app.generation.graph.progress import publish_step
from app.capability.hypergraph import HEdge, HNode, HyperGraph
from app.capability.ontology import (
    attention_cost,
    default_bt_pattern,
    get_attention_budget,
    get_collaboration_modes,
    mode_from_capability_mode,
    needs_human_input,
    resolve_alias,
)
from app.capability.utility import (
    AllocationScore,
    compute_human_utility,
    compute_robot_utility,
)
from app.capability.allocation_metrics import compute_allocation_quality

if TYPE_CHECKING:
    from app.experiment.controller import ExperimentController

logger = logging.getLogger(__name__)

_experiment_controller: ExperimentController | None = None


def set_experiment_controller(ctrl: ExperimentController | None) -> None:
    """Inject experiment controller for allocation overrides."""
    global _experiment_controller
    _experiment_controller = ctrl

_resolver = EffectiveCapabilityResolver()


# ------------------------------------------------------------------
# Hypergraph construction (enhanced: includes device/channel nodes)
# ------------------------------------------------------------------

def _build_hypergraph(
    entities: list[dict[str, Any]],
    task_plan: dict[str, Any],
) -> HyperGraph:
    """Build a task-scoped hypergraph from entities + task plan.

    Supports both robot entities (direct capabilities) and human entities
    (device-derived capabilities via EffectiveCapabilityResolver).
    """
    graph = HyperGraph()

    for ent in entities:
        eid = ent.get("entity_id", "")
        if not eid:
            continue

        is_human = ent.get("type") == "human" or ent.get("entity_type") == "human"
        graph.add_node(HNode(id=eid, kind="entity", attrs=dict(ent)))

        if is_human:
            # Build device/channel sub-graph for this human
            for device_data in ent.get("devices", []):
                did = device_data.get("device_id", "")
                if not did:
                    continue
                graph.add_node(HNode(
                    id=did,
                    kind="device",
                    attrs={
                        "type": device_data.get("type", "unknown"),
                        "status": device_data.get("status", "online"),
                    },
                ))
                graph.add_edge(HEdge(
                    id=f"eq_{eid}_{did}",
                    kind="equips",
                    nodes=frozenset([eid, did]),
                    weight=1.0,
                ))
                for ch_id in device_data.get("channels", []):
                    if ch_id not in graph.nodes:
                        graph.add_node(HNode(id=ch_id, kind="channel", attrs={}))
                    graph.add_edge(HEdge(
                        id=f"pv_{did}_{ch_id}",
                        kind="provides",
                        nodes=frozenset([did, ch_id]),
                        weight=1.0,
                    ))
            # Derive human's effective capabilities from channels
            _resolver.resolve_and_apply(graph, eid)

        else:
            # Robot: direct structured_capabilities + flat fallback
            caps: list[dict[str, Any]] = []
            for sc in ent.get("structured_capabilities", []):
                caps.append(sc)
            for c in ent.get("capabilities", []):
                cap_name = c if isinstance(c, str) else (c.get("name") or c.get("id", ""))
                if cap_name and not any(
                    (x.get("name") or x.get("id", "")) == cap_name for x in caps
                ):
                    caps.append({"name": cap_name})

            for cap_data in caps:
                raw_name = cap_data.get("name") or cap_data.get("id", "")
                if not raw_name:
                    continue
                canonical = resolve_alias(str(raw_name))
                mode_raw = cap_data.get("mode", "autonomous")
                if canonical not in graph.nodes:
                    graph.add_node(HNode(id=canonical, kind="capability", attrs=dict(cap_data)))
                edge_id = f"hc_{eid}_{canonical}"
                if edge_id not in graph.edges:
                    graph.add_edge(HEdge(
                        id=edge_id,
                        kind="has_capability",
                        nodes=frozenset([eid, canonical]),
                        weight=float(cap_data.get("proficiency", 1.0)),
                        attrs={
                            "mode": mode_raw,
                            "collab_mode": mode_from_capability_mode(mode_raw),
                        },
                    ))

    # Task + requires edges
    for subtask in task_plan.get("subtasks", task_plan.get("phases", [])):
        tid = subtask.get("task_id") or subtask.get("id", "")
        if not tid:
            continue
        graph.add_node(HNode(id=tid, kind="task", attrs=dict(subtask)))
        for rc in subtask.get("required_capabilities", []):
            cap_name = rc if isinstance(rc, str) else rc.get("name", "")
            canonical = resolve_alias(str(cap_name))
            if canonical not in graph.nodes:
                graph.add_node(HNode(id=canonical, kind="capability"))
            importance = 1.0 if isinstance(rc, str) else float(rc.get("importance", 1.0))
            graph.add_edge(HEdge(
                id=f"req_{tid}_{canonical}",
                kind="requires",
                nodes=frozenset([tid, canonical]),
                weight=importance,
            ))

    return graph


# ------------------------------------------------------------------
# Capability mode detection per subtask
# ------------------------------------------------------------------

def _required_collab_mode(subtask: dict, entities: list[dict], graph: HyperGraph) -> str:
    """Determine the required collaboration mode for a subtask.

    Walks the assigned/candidate robot's has_capability edges to find
    the most demanding CapabilityMode across required capabilities.
    Priority: proxy > partner > task_based.
    """
    required_cap_names = {
        resolve_alias(r if isinstance(r, str) else r.get("name", ""))
        for r in subtask.get("required_capabilities", [])
    }

    mode_priority = {"proxy": 2, "partner": 1, "task_based": 0}
    max_mode = "task_based"

    for e in graph.edges.values():
        if e.kind != "has_capability":
            continue
        cap_ids = {nid for nid in e.nodes if graph.nodes.get(nid, HNode("", "entity")).kind == "capability"}
        if not cap_ids & required_cap_names:
            continue
        # Only consider robot entities (humans derive mode from devices)
        entity_ids = {nid for nid in e.nodes if graph.nodes.get(nid, HNode("", "capability")).kind == "entity"}
        entity_nodes = [graph.nodes[eid] for eid in entity_ids if eid in graph.nodes]
        if all(n.attrs.get("entity_type") == "human" for n in entity_nodes):
            continue
        collab = e.attrs.get("collab_mode", "task_based")
        if mode_priority.get(collab, 0) > mode_priority.get(max_mode, 0):
            max_mode = collab

    return max_mode


# ------------------------------------------------------------------
# Hard-constraint filtering
# ------------------------------------------------------------------

def _filter_robot_candidates(
    subtask: dict,
    entities: list[dict],
    graph: HyperGraph,
) -> list[str]:
    """Return robot entity IDs that satisfy all capability constraints."""
    raw_caps = subtask.get("required_capabilities", [])
    required = {
        resolve_alias(r if isinstance(r, str) else r.get("name", ""))
        for r in raw_caps
    }
    stid = subtask.get("task_id") or subtask.get("id", "?")
    logger.debug(
        "robot_candidates(%s): raw_caps=%s → resolved=%s",
        stid, [r if isinstance(r, str) else r.get("name", "") for r in raw_caps], required,
    )
    candidates: list[str] = []
    for ent in entities:
        eid = ent.get("entity_id", "")
        if not eid or eid not in graph.nodes:
            continue
        if ent.get("type") == "human" or ent.get("entity_type") == "human":
            continue
        if ent.get("status") in ("offline", "dead", "DEAD"):
            continue
        if not required:
            candidates.append(eid)
            continue
        entity_caps = {
            nid
            for e in graph.edges_of(eid, "has_capability")
            for nid in e.nodes
            if nid != eid
        }
        if required <= entity_caps:
            candidates.append(eid)
        else:
            logger.debug(
                "robot_candidates(%s): %s REJECTED — has %s, needs %s, missing %s",
                stid, eid, entity_caps, required, required - entity_caps,
            )
    if not candidates:
        logger.warning(
            "robot_candidates(%s): NO candidates — falling back to all online robots. required=%s",
            stid, required,
        )
        for ent in entities:
            eid = ent.get("entity_id", "")
            if not eid or eid not in graph.nodes:
                continue
            if ent.get("type") == "human" or ent.get("entity_type") == "human":
                continue
            if ent.get("status") in ("offline", "dead", "DEAD"):
                continue
            candidates.append(eid)
    return candidates


def _filter_human_supervisors(
    entities: list[dict],
    graph: HyperGraph,
    required_collab_mode: str,
    attention_spent: dict[str, float],
    attention_budget: float,
) -> list[str]:
    """Return human entity IDs that can supervise given the required collaboration mode.

    Checks:
    1. Entity is a human and online.
    2. Available devices support the required collaboration mode.
    3. Remaining attention budget >= mode's base attention cost.
    """
    collab_modes = get_collaboration_modes()
    mode_meta = collab_modes.get(required_collab_mode, {})
    min_channels = set(mode_meta.get("min_channels", []))
    cost = float(mode_meta.get("attention_cost_base", 0.0))

    candidates: list[str] = []
    for ent in entities:
        eid = ent.get("entity_id", "")
        if not eid or eid not in graph.nodes:
            continue
        if ent.get("type") != "human" and ent.get("entity_type") != "human":
            continue
        if ent.get("status") in ("offline", "dead", "DEAD"):
            continue

        available_channels = graph.available_channels(eid)
        if not min_channels <= available_channels:
            continue

        spent = attention_spent.get(eid, 0.0)
        reserve = get_attention_budget().get("reserve", 0.1)
        if spent + cost > attention_budget * (1.0 - reserve):
            logger.debug(
                "[Allocator] %s attention budget exhausted (spent=%.2f, cost=%.2f, budget=%.2f)",
                eid, spent, cost, attention_budget,
            )
            continue

        candidates.append(eid)
    return candidates


# ------------------------------------------------------------------
# Scoring
# ------------------------------------------------------------------

def _score_robot(
    eid: str,
    subtask: dict,
    graph: HyperGraph,
    context: dict,
) -> AllocationScore | None:
    e_node = graph.nodes.get(eid)
    if not e_node:
        return None
    tid = subtask.get("task_id") or subtask.get("id", "")
    task_node = graph.nodes.get(tid) or HNode(id=tid, kind="task", attrs=dict(subtask))
    return compute_robot_utility(e_node, task_node, graph, context)


def _score_human(
    eid: str,
    subtask: dict,
    graph: HyperGraph,
    context: dict,
) -> AllocationScore | None:
    e_node = graph.nodes.get(eid)
    if not e_node:
        return None
    tid = subtask.get("task_id") or subtask.get("id", "")
    task_node = graph.nodes.get(tid) or HNode(id=tid, kind="task", attrs=dict(subtask))
    return compute_human_utility(e_node, task_node, graph, context)


def _x_to_collaboration_mode(x: float) -> tuple[str, str]:
    """Map continuous involvement degree x to (collaboration, bt_pattern)."""
    if x < 0.1:
        return "task_based", "autonomous"
    elif x < 0.5:
        return "partner", "human_plan_execute"
    else:
        return "proxy", "human_plan_execute"


# ------------------------------------------------------------------
# LangGraph node function
# ------------------------------------------------------------------

def allocator_node(state: dict) -> dict:
    """Quantitative allocator with joint optimisation.

    Outputs per-subtask ``interaction`` block in addition to ``assigned_entity_ids``.
    """
    task_plan = state.get("task_plan")
    entities: list[dict] = state.get("entities", [])
    context: dict = state.get("environment", {})
    task_id = state.get("task_id", "?")

    if not task_plan:
        logger.warning("[%s] allocator: no task_plan — skipping", task_id)
        return {}

    publish_step("allocator", "started",
                 message=f"量化分配 {len(entities)} 个实体…")

    graph = _build_hypergraph(entities, task_plan)

    attention_budget = get_attention_budget().get("total", 1.0)
    attention_spent: dict[str, float] = {}   # human_entity_id → cumulative cost

    subtasks = task_plan.get("subtasks", task_plan.get("phases", []))

    # Sort by priority: critical > urgent > normal (preserve order within same tier)
    priority_order = {"critical": 0, "urgent": 1, "normal": 2}
    subtasks_sorted = sorted(
        subtasks,
        key=lambda s: priority_order.get(s.get("priority", "normal"), 2),
    )

    allocation_trace: list[dict] = []
    robot_task_count: dict[str, int] = {}

    for subtask in subtasks_sorted:
        stid = subtask.get("task_id") or subtask.get("id", "")

        # --- Step 0: experiment override ----------------------------------
        experiment_override: dict[str, Any] | None = None
        if _experiment_controller:
            experiment_override = _experiment_controller.get_override_for_subtask(subtask)

        # --- Step 1: find robot candidates --------------------------------
        robot_candidates = _filter_robot_candidates(subtask, entities, graph)
        if not robot_candidates:
            logger.warning("[%s] allocator: no robot candidates for %s", task_id, stid)
            subtask["assigned_entity_ids"] = []
            subtask["interaction"] = {"mode": "unassigned", "collaboration": "none"}
            allocation_trace.append({"subtask_id": stid, "assigned": [], "reason": "no_candidates"})
            continue

        # --- Step 2: score robots, pick best (with load balancing) --------
        robot_scores = [s for eid in robot_candidates if (s := _score_robot(eid, subtask, graph, context))]
        # Apply LLM pre-assignment hint bonus: treat planner's assignment as soft constraint
        # (capture before we overwrite it at Step 5)
        llm_hint_ids: list[str] = list(subtask.get("assigned_entity_ids") or [])
        if llm_hint_ids:
            for score in robot_scores:
                if score.entity_id in llm_hint_ids:
                    score.total = round(score.total + 0.15, 4)
                    score.breakdown["llm_hint_bonus"] = 0.15
        # Apply load-balancing penalty: each already-assigned task reduces score by 0.05
        for score in robot_scores:
            load_penalty = robot_task_count.get(score.entity_id, 0) * 0.05
            score.total = round(score.total - load_penalty, 4)
            if load_penalty > 0:
                score.breakdown["load_penalty"] = -round(load_penalty, 4)
        robot_scores.sort(key=lambda s: s.total, reverse=True)
        best_robot = robot_scores[0] if robot_scores else None

        # --- Step 2.5: apply forced_robot from experiment override --------
        if experiment_override and experiment_override.get("forced_robot"):
            forced_rid = experiment_override["forced_robot"]
            forced_score = next((s for s in robot_scores if s.entity_id == forced_rid), None)
            if forced_score:
                best_robot = forced_score
            elif forced_rid in [n.id for n in graph.nodes.values() if n.kind == "entity"]:
                best_robot = AllocationScore(entity_id=forced_rid, total=0.0, breakdown={})
            logger.info("[%s] experiment forced robot=%s for %s", task_id, forced_rid, stid)

        # --- Step 3: determine required collaboration mode ----------------
        required_collab = _required_collab_mode(subtask, entities, graph)

        # --- Step 3.5: upgrade bt_pattern when capability params need human input
        required_cap_names = {
            resolve_alias(r if isinstance(r, str) else r.get("name", ""))
            for r in subtask.get("required_capabilities", [])
        }
        subtask_params = subtask.get("params") or {}
        _needs_human = any(
            needs_human_input(cap, subtask_params) for cap in required_cap_names
        )

        # --- Step 3.7: use learned optimal_x if available on has_capability edge
        if best_robot and not experiment_override:
            for cap_name in required_cap_names:
                edge = graph.edges.get(f"hc_{best_robot.entity_id}_{cap_name}")
                if edge and "optimal_x" in edge.attrs:
                    learned_x = edge.attrs["optimal_x"]
                    confidence = edge.attrs.get("optimal_x_confidence", 0.0)
                    if confidence > 0.5:
                        learned_collab, learned_bt = _x_to_collaboration_mode(learned_x)
                        required_collab = learned_collab
                        logger.info(
                            "[%s] allocator: %s using learned optimal_x=%.3f → %s (confidence=%.2f)",
                            task_id, stid, learned_x, learned_collab, confidence,
                        )
                        break

        # --- Step 4: find human supervisor (if mode requires one) ---------
        human_supervisor: str | None = None
        actual_collab = required_collab
        bt_pattern = default_bt_pattern(required_collab)
        acost = attention_cost(required_collab)

        # --- Step 4.0: apply experiment override for collaboration/pattern
        if experiment_override:
            if "collaboration" in experiment_override:
                actual_collab = experiment_override["collaboration"]
                bt_pattern = experiment_override.get("bt_pattern", default_bt_pattern(actual_collab))
                acost = attention_cost(actual_collab)
                _needs_human = actual_collab != "task_based"
                logger.info(
                    "[%s] experiment forced collaboration=%s bt_pattern=%s for %s",
                    task_id, actual_collab, bt_pattern, stid,
                )

        if _needs_human and bt_pattern == "autonomous":
            bt_pattern = "human_plan_execute"
            if actual_collab == "task_based":
                actual_collab = "partner"
                acost = attention_cost("partner")
            logger.info(
                "[%s] allocator: %s upgraded to %s (capability params require human input)",
                task_id, stid, bt_pattern,
            )

        # Always try to assign a human supervisor for failure fallback (HMTA pattern).
        # For task_based/supervised_fallback, the attention cost of fallback-only
        # supervision is negligible, so we use the mode's base cost (0 for task_based).
        human_candidates = _filter_human_supervisors(
            entities, graph, required_collab, attention_spent, attention_budget,
        )
        if not human_candidates and required_collab == "task_based":
            # Relax channel constraints for fallback-only supervision
            human_candidates = [
                ent.get("entity_id", "")
                for ent in entities
                if (ent.get("type") == "human" or ent.get("entity_type") == "human")
                and ent.get("status") not in ("offline", "dead", "DEAD")
                and ent.get("entity_id")
            ]

        human_scores = [
            s for eid in human_candidates
            if (s := _score_human(eid, subtask, graph, context))
        ]
        human_scores.sort(key=lambda s: s.total, reverse=True)

        if human_scores:
            best_human = human_scores[0]
            human_supervisor = best_human.entity_id
            attention_spent[human_supervisor] = (
                attention_spent.get(human_supervisor, 0.0) + acost
            )
        elif required_collab != "task_based":
            logger.warning(
                "[%s] allocator: no eligible human supervisor for %s (%s) — degrading to task_based",
                task_id, stid, required_collab,
            )
            actual_collab = "task_based"
            bt_pattern = "supervised_fallback"
            acost = 0.0

        # --- Step 4.5: apply forced_human from experiment override --------
        if experiment_override and experiment_override.get("forced_human"):
            human_supervisor = experiment_override["forced_human"]
            logger.info("[%s] experiment forced human=%s for %s", task_id, human_supervisor, stid)

        # --- Step 5: write allocation results back into subtask -----------
        assigned = [best_robot.entity_id] if best_robot else []
        subtask["assigned_entity_ids"] = assigned
        if best_robot:
            robot_task_count[best_robot.entity_id] = robot_task_count.get(best_robot.entity_id, 0) + 1
        if best_robot:
            subtask["allocation_score"] = round(best_robot.total, 4)
            subtask["allocation_breakdown"] = {
                k: round(v, 4) for k, v in best_robot.breakdown.items()
            }

        collab_modes = get_collaboration_modes()
        capability_mode = collab_modes.get(actual_collab, {}).get("capability_mode", "MODE_AUTONOMOUS")
        subtask["interaction"] = {
            "collaboration": actual_collab,       # task_based | partner | proxy
            "capability_mode": capability_mode,   # MODE_AUTONOMOUS | MODE_SUPERVISED | MODE_REMOTE_CONTROL
            "human_supervisor": human_supervisor, # entity_id or null
            "attention_cost": round(acost, 3),
            "bt_pattern": bt_pattern,             # consumed by bt_builder
        }

        trace_entry: dict[str, Any] = {
            "subtask_id": stid,
            "robot_candidates": robot_candidates,
            "human_candidates_checked": len(
                _filter_human_supervisors(entities, graph, required_collab, {}, attention_budget)
            ),
            "assigned": assigned,
            "human_supervisor": human_supervisor,
            "collaboration": actual_collab,
            "bt_pattern": bt_pattern,
            "attention_cost": round(acost, 3),
            "robot_scores": [
                {"entity_id": s.entity_id, "total": round(s.total, 4)}
                for s in robot_scores[:3]
            ],
        }
        if experiment_override:
            trace_entry["experiment_trial_id"] = experiment_override.get("trial_id")
            trace_entry["experiment_override"] = experiment_override
        allocation_trace.append(trace_entry)

        logger.info(
            "[%s] allocator: %s → robot=%s supervisor=%s mode=%s pattern=%s",
            task_id, stid, assigned, human_supervisor, actual_collab, bt_pattern,
        )

    # Attention budget summary
    attention_summary = {eid: round(spent, 3) for eid, spent in attention_spent.items()}
    logger.info("[%s] allocator: attention budget used: %s", task_id, attention_summary)
    assigned_count = sum(1 for st in subtasks_sorted if st.get("assigned_entity_ids"))
    publish_step("allocator", "completed",
                 message=f"已分配 {assigned_count}/{len(subtasks)} 个子任务, 注意力预算: {attention_summary}")

    # Compute allocation quality metrics
    quality = compute_allocation_quality(
        allocation_trace,
        attention_summary=attention_summary,
        attention_budget=attention_budget,
    )

    # Derive task dependency edges from ontology preconditions/effects
    derive_task_dependencies(graph, task_plan)

    # Issue #4: write priority-sorted order back so downstream sees consistent ordering
    if "subtasks" in task_plan:
        task_plan["subtasks"] = subtasks_sorted
    elif "phases" in task_plan:
        task_plan["phases"] = subtasks_sorted

    return {
        "task_plan": task_plan,
        "capability_graph": graph.to_dict(),
        "attention_summary": attention_summary,
        "allocation_quality": quality.to_dict(),
        "allocation_trace": [
            *state.get("allocation_trace", []),
            *allocation_trace,
        ],
        "generation_trace": [
            *state.get("generation_trace", []),
            {
                "node": "allocator",
                "subtask_count": len(subtasks),
                "entity_count": len(entities),
                "attention_budget_remaining": round(
                    attention_budget - sum(attention_spent.values()), 3
                ),
            },
        ],
    }


# ------------------------------------------------------------------
# Hypergraph-based task dependency derivation
# ------------------------------------------------------------------

def derive_task_dependencies(graph: HyperGraph, task_plan: dict[str, Any]) -> None:
    """Populate precondition/effect/depends_on HEdges on *graph* in-place.

    Algorithm:
    1. For every capability node in the graph, add precondition and effect edges
       sourced from the ontology.
    2. For tasks assigned to the same entity (ordered by subtask list position),
       find the earliest preceding task whose capability.effects satisfy a
       precondition of the current task's capability — and create a depends_on edge.
    3. Also honour explicit ``depends_on`` attrs already present in the task_plan.

    Idempotent: calling multiple times only adds edges that don't already exist.
    """
    from app.capability.ontology import get_preconditions, get_effects, resolve_alias

    subtasks: list[dict[str, Any]] = task_plan.get("subtasks", task_plan.get("phases", []))

    # Step 1 — populate precondition/effect edges for all known capabilities
    for nid, node in list(graph.nodes.items()):
        if node.kind != "capability":
            continue
        for token in get_preconditions(nid):
            eid = f"pre_{nid}_{token}"
            if eid not in graph.edges:
                graph.add_edge(HEdge(
                    id=eid, kind="precondition",
                    nodes=frozenset([nid, token]),
                    attrs={"token": token},
                ))
        for token in get_effects(nid):
            eid = f"eff_{nid}_{token}"
            if eid not in graph.edges:
                graph.add_edge(HEdge(
                    id=eid, kind="effect",
                    nodes=frozenset([nid, token]),
                    attrs={"token": token},
                ))

    # Step 2 — auto-derive depends_on edges
    # Group tasks by assigned entity (preserving declaration order)
    from collections import defaultdict
    entity_task_order: dict[str, list[dict]] = defaultdict(list)
    for st in subtasks:
        tid = st.get("task_id") or st.get("id", "")
        if not tid:
            continue
        assigned = st.get("assigned_entity_ids") or st.get("interaction", {}).get("assigned", [])
        if not assigned and st.get("assigned"):
            assigned = [st["assigned"]] if isinstance(st["assigned"], str) else st["assigned"]
        for entity_id in (assigned or []):
            entity_task_order[entity_id].append(st)

    for entity_id, ordered_tasks in entity_task_order.items():
        # Track which effect tokens are provided by which task_id (in order)
        available_effects: dict[str, str] = {}  # effect_token → provider_task_id

        for st in ordered_tasks:
            tid = st.get("task_id") or st.get("id", "")
            cap_raw = st.get("intent") or st.get("capability") or ""
            cap = resolve_alias(str(cap_raw)) if cap_raw else ""

            # Check preconditions of this capability
            preconds = get_preconditions(cap) if cap else []
            for token in preconds:
                if token in available_effects:
                    provider_tid = available_effects[token]
                    edge_id = f"dep_{tid}_{provider_tid}"
                    if edge_id not in graph.edges:
                        graph.add_edge(HEdge(
                            id=edge_id,
                            kind="depends_on",
                            nodes=frozenset([tid, provider_tid]),
                            attrs={
                                "dependent": tid,
                                "provider": provider_tid,
                                "condition": token,
                                "auto_derived": True,
                            },
                        ))
                        logger.debug(
                            "[deps] %s depends_on %s (condition: %s, entity: %s)",
                            tid, provider_tid, token, entity_id,
                        )

            # Register effects produced by this task
            for token in get_effects(cap) if cap else []:
                available_effects[token] = tid

    # Step 3 — honour explicit depends_on declared in task_plan subtasks
    for st in subtasks:
        tid = st.get("task_id") or st.get("id", "")
        explicit_dep = st.get("depends_on")
        if explicit_dep and tid:
            edge_id = f"dep_{tid}_{explicit_dep}_explicit"
            if edge_id not in graph.edges:
                graph.add_edge(HEdge(
                    id=edge_id,
                    kind="depends_on",
                    nodes=frozenset([tid, explicit_dep]),
                    attrs={
                        "dependent": tid,
                        "provider": explicit_dep,
                        "condition": "explicit",
                        "auto_derived": False,
                    },
                ))

    dep_count = sum(1 for e in graph.edges.values() if e.kind == "depends_on")
    logger.info("[deps] derive_task_dependencies: %d depends_on edges created", dep_count)


# ------------------------------------------------------------------
# Single-subtask reallocation (Sprint 2: dynamic reallocation)
# ------------------------------------------------------------------

def reallocate_subtask(
    subtask: dict,
    entities: list[dict],
    graph: HyperGraph,
    context: dict,
    exclude_entities: list[str] | None = None,
) -> dict[str, Any] | None:
    """Re-score and reassign a single subtask, optionally excluding specific entities.

    Returns the new allocation result dict or None if no candidate found.
    """
    stid = subtask.get("task_id") or subtask.get("id", "")
    excluded = set(exclude_entities or [])

    robot_candidates = [
        eid for eid in _filter_robot_candidates(subtask, entities, graph)
        if eid not in excluded
    ]
    if not robot_candidates:
        logger.warning("[realloc] no robot candidates for %s after exclusion", stid)
        return None

    robot_scores = [s for eid in robot_candidates if (s := _score_robot(eid, subtask, graph, context))]
    robot_scores.sort(key=lambda s: s.total, reverse=True)
    best = robot_scores[0] if robot_scores else None
    if not best:
        return None

    required_collab = _required_collab_mode(subtask, entities, graph)
    bt_pattern = default_bt_pattern(required_collab)

    result = {
        "subtask_id": stid,
        "assigned_entity_ids": [best.entity_id],
        "allocation_score": round(best.total, 4),
        "collaboration": required_collab,
        "bt_pattern": bt_pattern,
        "reallocated": True,
    }
    logger.info("[realloc] %s → %s (score=%.3f)", stid, best.entity_id, best.total)
    return result
