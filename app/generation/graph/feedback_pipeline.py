"""Post-mission feedback / proficiency-learning pipeline.

Three-node LangGraph that runs after mission conclusion:
  metrics_aggregator   → compute per-(entity, capability) metrics from task queue
  bottleneck_analyzer  → identify bottlenecks, publish BottleneckData
  proficiency_proposer → EMA update, publish ProficiencyProposal list
"""

from __future__ import annotations

import logging
import time as _time
from typing import TYPE_CHECKING, Any

from langgraph.graph import END, StateGraph

from app.generation.graph.feedback_state import FeedbackState

if TYPE_CHECKING:
    from app.capability.registry import CapabilityRegistry
    from app.experiment.proficiency_store import ProficiencyStore
    from app.zenoh_bridge import ZenohBridge

logger = logging.getLogger(__name__)


# ── Helper ────────────────────────────────────────────────────────────────────

def _safe_resolve(intent: str) -> str:
    """Resolve capability alias without crashing if ontology is not loaded."""
    try:
        from app.capability.ontology import resolve_alias
        return resolve_alias(intent)
    except Exception:
        return intent


# ── Node 1: metrics_aggregator ────────────────────────────────────────────────

def _metrics_aggregator_node(
    zenoh: "ZenohBridge",
    config: dict,
) -> Any:
    """Build the actual LangGraph node callable."""

    def _node(state: FeedbackState) -> dict:
        task_id = state.get("task_id", "unknown")
        task_queue: list[dict] = state.get("task_queue") or []

        zenoh.publish_progress(task_id, "metrics_aggregator", "started",
                               message="开始聚合任务指标…")

        # Group tasks by (entity_id, capability_id)
        groups: dict[tuple[str, str], list[dict]] = {}
        for task in task_queue:
            entity_id = task.get("entity") or task.get("entity_id", "")
            intent = task.get("intent", "")
            cap_id = _safe_resolve(intent) if intent else intent
            key = (entity_id, cap_id)
            groups.setdefault(key, []).append(task)

        # Terminal statuses that mean the task actually ran (and we can learn from).
        # "cancelled" / "pending" / "skipped" tasks may have been aborted when
        # the mission ended -- they are NOT failures, so we exclude them.
        _EVALUATED_STATUSES = {"completed", "failed"}
        _MISSION_ABORT_STATUSES = {"cancelled", "pending", "skipped", "aborted"}

        entity_metrics: dict[str, dict] = {}
        for (entity_id, cap_id), tasks in groups.items():
            if not entity_id:
                continue

            total = len(tasks)

            # Only tasks that actually ran contribute to performance signals.
            evaluated = [t for t in tasks if t.get("status") in _EVALUATED_STATUSES]
            mission_cancelled = [
                t for t in tasks if t.get("status") in _MISSION_ABORT_STATUSES
            ]
            evaluated_total = len(evaluated)

            if evaluated_total == 0:
                # None of this entity's tasks ran — cannot infer anything.
                # Skip so we don't pollute proficiency with zero-run "failures".
                logger.debug(
                    "[FeedbackPipeline] metrics_aggregator: skipping %s/%s "
                    "— all %d tasks were mission-cancelled",
                    entity_id, cap_id, total,
                )
                continue

            completed = sum(1 for t in evaluated if t.get("status") == "completed")
            failed = sum(1 for t in evaluated if t.get("status") == "failed")

            # elapsed_ms only from tasks that actually ran
            elapsed_vals = [
                t["elapsed_ms"] for t in evaluated
                if isinstance(t.get("elapsed_ms"), (int, float)) and t["elapsed_ms"] > 0
            ]
            avg_elapsed = sum(elapsed_vals) / len(elapsed_vals) if elapsed_vals else 0.0

            # Interventions only from evaluated tasks
            total_interventions = sum(
                t.get("human_intervention_count", 0) for t in evaluated
            )

            baseline_ms = float(
                config.get("feedback", {}).get("duration_baseline_ms", 30_000)
            )

            # All ratios are over *evaluated* tasks, not total queue length
            success_rate = completed / evaluated_total
            duration_eff = min(baseline_ms / avg_elapsed, 1.0) if avg_elapsed > 0 else 1.0
            intervention_rate = total_interventions / evaluated_total

            # Confidence: fraction of queued tasks that actually ran.
            # Used downstream to dampen the learning rate when only a few
            # tasks were evaluated (e.g., mission aborted early).
            confidence = evaluated_total / total if total > 0 else 1.0

            group_key = f"{entity_id}::{cap_id}"
            entity_metrics[group_key] = {
                "entity_id": entity_id,
                "capability_id": cap_id,
                "total": total,
                "evaluated_total": evaluated_total,
                "cancelled_total": len(mission_cancelled),
                "completed": completed,
                "failed": failed,
                "avg_elapsed_ms": avg_elapsed,
                "total_interventions": total_interventions,
                "success_rate": success_rate,
                "duration_eff": duration_eff,
                "intervention_rate": intervention_rate,
                "confidence": confidence,
            }

        # ── Human operator metrics ────────────────────────────────────────
        # Group by (human_supervisor_id, capability_id) for tasks that
        # actually escalated to human fallback.  Two distinct failure modes:
        #   - timeout  → human did NOT respond (human_timeout_count)
        #   - retry succeeded → human guided the robot, task completed
        #   - retry failed → human guided the robot but it still failed
        human_groups: dict[tuple[str, str], dict] = {}
        for task in task_queue:
            total_escalations = task.get("human_intervention_count", 0)
            if total_escalations == 0:
                continue
            human_id = (
                task.get("human_supervisor")
                or task.get("human_supervisor_id")
                or ""
            )
            if not human_id:
                continue
            intent = task.get("intent", "")
            cap_id = _safe_resolve(intent) if intent else intent
            key = (human_id, cap_id)
            if key not in human_groups:
                human_groups[key] = {
                    "human_id": human_id,
                    "cap_id": cap_id,
                    "total_escalations": 0,
                    "timeouts": 0,
                    "responses": 0,
                    # task completed after human responded (effective intervention)
                    "effective_responses": 0,
                    # task failed even after human responded (robot still broken)
                    "ineffective_responses": 0,
                    "total_response_ms": 0,
                }
            g = human_groups[key]
            g["total_escalations"] += total_escalations
            g["timeouts"] += task.get("human_timeout_count", 0)
            g["responses"] += task.get("human_response_count", 0)
            if task.get("human_response_count", 0) > 0:
                if task.get("status") == "completed":
                    g["effective_responses"] += 1
                else:
                    g["ineffective_responses"] += 1
            # Only count response-side ms (exclude pure-timeout waiting time)
            # human_intervention_ms includes both; we use it as a rough proxy
            # for response speed since that's all we have without sub-event logging.
            g["total_response_ms"] += task.get("human_intervention_ms", 0)

        baseline_response_ms = float(
            config.get("feedback", {}).get("human_response_baseline_ms", 30_000)
        )

        for (human_id, cap_id), g in human_groups.items():
            total_esc = g["total_escalations"]
            if total_esc == 0:
                continue
            responses = g["responses"]
            response_rate = responses / total_esc  # 0 if always times out
            effective = g["effective_responses"]
            ineffective = g["ineffective_responses"]
            effectiveness = effective / responses if responses > 0 else 0.0
            avg_response_ms = (
                g["total_response_ms"] / responses if responses > 0 else baseline_response_ms * 2
            )
            speed_eff = min(baseline_response_ms / avg_response_ms, 1.0) if avg_response_ms > 0 else 1.0

            group_key = f"{human_id}::{cap_id}"
            entity_metrics[group_key] = {
                "entity_id": human_id,
                "capability_id": cap_id,
                "is_human": True,
                # Keep interface compatible with robot metrics
                "total": total_esc,
                "evaluated_total": total_esc,
                "cancelled_total": 0,
                "completed": effective,
                "failed": g["timeouts"] + ineffective,
                "avg_elapsed_ms": avg_response_ms,
                "total_interventions": 0,  # not applicable for humans
                # Human-specific
                "response_rate": response_rate,
                "effectiveness": effectiveness,
                "speed_eff": speed_eff,
                "timeouts": g["timeouts"],
                "responses": responses,
                "effective_responses": effective,
                "ineffective_responses": ineffective,
                # Generic ratios used by bottleneck analyzer
                "success_rate": response_rate * effectiveness if responses > 0 else 0.0,
                "duration_eff": speed_eff,
                "intervention_rate": 0.0,
                "confidence": 1.0,
            }

        evaluated_count = sum(
            m["evaluated_total"] for m in entity_metrics.values() if not m.get("is_human")
        )
        cancelled_count = sum(
            m["cancelled_total"] for m in entity_metrics.values() if not m.get("is_human")
        )
        human_group_count = sum(1 for m in entity_metrics.values() if m.get("is_human"))
        logger.info(
            "[FeedbackPipeline] metrics_aggregator: %d robot groups (%d evaluated, %d cancelled), "
            "%d human operator groups",
            len(entity_metrics) - human_group_count, evaluated_count, cancelled_count, human_group_count,
        )
        zenoh.publish_progress(task_id, "metrics_aggregator", "completed",
                               message=(
                                   f"指标聚合完成：{len(entity_metrics) - human_group_count} 机器组"
                                   f"（{cancelled_count} 任务因任务结束取消）"
                                   + (f"，{human_group_count} 人员组" if human_group_count else "")
                               ))
        return {"entity_metrics": entity_metrics}

    return _node


# ── Node 2: bottleneck_analyzer ───────────────────────────────────────────────

def _bottleneck_analyzer_node(
    zenoh: "ZenohBridge",
    config: dict,
    store: "ProficiencyStore | None" = None,
) -> Any:

    def _node(state: FeedbackState) -> dict:
        task_id = state.get("task_id", "unknown")
        entity_metrics: dict[str, dict] = state.get("entity_metrics") or {}

        zenoh.publish_progress(task_id, "bottleneck_analyzer", "started",
                               message="开始分析瓶颈…")

        thresholds = config.get("feedback", {}).get("bottleneck_thresholds", {})
        dur_mult = float(thresholds.get("duration_multiplier", 2.0))
        intv_count_thresh = int(thresholds.get("intervention_count", 2))
        intv_pct_thresh = float(thresholds.get("intervention_pct", 0.3))

        bottlenecks = []
        health_scores: list[float] = []

        for group_key, m in entity_metrics.items():
            entity_id = m["entity_id"]
            cap_id = m["capability_id"]
            baseline_ms = float(config.get("feedback", {}).get("duration_baseline_ms", 30_000))

            detected_types: list[str] = []
            suggestion_parts: list[str] = []

            # Long-running detection
            if m["avg_elapsed_ms"] > baseline_ms * dur_mult:
                detected_types.append("long_duration")
                suggestion_parts.append(
                    f"平均耗时 {m['avg_elapsed_ms']:.0f}ms 超过基准 {dur_mult}x"
                )

            # Repeated failure detection
            if m["failed"] >= 1 and m["success_rate"] < 0.5:
                detected_types.append("repeated_failure")
                suggestion_parts.append(
                    f"成功率 {m['success_rate']:.0%}，失败 {m['failed']} 次"
                )

            # Human intervention detection
            if m["total_interventions"] >= intv_count_thresh or m["intervention_rate"] >= intv_pct_thresh:
                detected_types.append("human_intervention")
                suggestion_parts.append(
                    f"人工干预 {m['total_interventions']} 次 ({m['intervention_rate']:.0%})"
                )

            # Health score: weighted combination
            health = (
                0.40 * m["success_rate"]
                + 0.35 * m["duration_eff"]
                + 0.25 * (1.0 - m["intervention_rate"])
            )
            health_scores.append(health)

            if detected_types:
                bottlenecks.append({
                    "node_id": group_key,
                    "node_name": f"{entity_id}/{cap_id}",
                    "entity_id": entity_id,
                    "severity": "high" if health < 0.4 else "medium" if health < 0.7 else "low",
                    "duration_ms": int(m["avg_elapsed_ms"]),
                    "total_pct": round(1.0 - m["duration_eff"], 3),
                    "bottleneck_type": detected_types[0],
                    "suggestion": "；".join(suggestion_parts) if suggestion_parts else None,
                })

        overall_health = sum(health_scores) / len(health_scores) if health_scores else 1.0

        bottleneck_data: dict = {
            "task_id": task_id,
            "health_score": round(overall_health, 3),
            "total_duration_ms": int(
                # Use evaluated_total (tasks that ran), not total queue length
                sum(
                    m["avg_elapsed_ms"] * m.get("evaluated_total", m["total"])
                    for m in entity_metrics.values()
                )
            ),
            "bottlenecks": bottlenecks,
            "summary": (
                f"发现 {len(bottlenecks)} 个瓶颈，综合健康度 {overall_health:.0%}"
                if bottlenecks else "未检测到明显瓶颈"
            ),
        }

        zenoh.publish_profiler_bottlenecks(bottleneck_data)

        if store is not None:
            try:
                store.save_bottleneck(
                    task_id=task_id,
                    health_score=bottleneck_data["health_score"],
                    total_duration_ms=bottleneck_data["total_duration_ms"],
                    bottlenecks=bottlenecks,
                    summary=bottleneck_data.get("summary"),
                )
                # Push updated history to frontend
                history = store.get_bottleneck_history(limit=20)
                zenoh.publish_bottleneck_history(history)
            except Exception:
                logger.exception("[FeedbackPipeline] bottleneck_analyzer: save_bottleneck failed")

        logger.info(
            "[FeedbackPipeline] bottleneck_analyzer: health=%.2f bottlenecks=%d",
            overall_health, len(bottlenecks),
        )
        zenoh.publish_progress(task_id, "bottleneck_analyzer", "completed",
                               message=f"瓶颈分析完成，健康度 {overall_health:.0%}")
        return {"bottleneck_data": bottleneck_data}

    return _node


# ── Node 3: proficiency_proposer ──────────────────────────────────────────────

def _proficiency_proposer_node(
    zenoh: "ZenohBridge",
    registry: "CapabilityRegistry",
    config: dict,
    store: "ProficiencyStore | None" = None,
) -> Any:

    def _node(state: FeedbackState) -> dict:
        task_id = state.get("task_id", "unknown")
        entity_metrics: dict[str, dict] = state.get("entity_metrics") or {}

        zenoh.publish_progress(task_id, "proficiency_proposer", "started",
                               message="开始计算能力评估提案…")

        fb_cfg = config.get("feedback", {})
        alpha = float(fb_cfg.get("learning_rate", 0.2))
        min_prof = float(fb_cfg.get("min_proficiency", 0.1))
        max_prof = float(fb_cfg.get("max_proficiency", 1.0))

        proposals: list[dict] = []

        def _entity_display_name(entity_id: str, is_human: bool) -> str:
            """Try to get a human-readable name; fall back to entity_id."""
            try:
                node = registry._graph.nodes.get(entity_id)
                if node and node.attrs.get("display_name"):
                    return node.attrs["display_name"]
            except Exception:
                pass
            return entity_id

        def _cap_display_name(cap_id: str) -> str:
            """Return a readable capability label."""
            _CAP_LABELS: dict[str, str] = {
                "navigation": "导航",
                "scan": "扫描",
                "manipulation": "操控",
                "inspection": "检测",
                "patrol": "巡逻",
                "communication": "通信",
                "coverage": "覆盖",
                "mapping": "建图",
            }
            return _CAP_LABELS.get(cap_id, cap_id)

        for group_key, m in entity_metrics.items():
            entity_id = m["entity_id"]
            cap_id = m["capability_id"]
            if not entity_id or not cap_id:
                continue
            is_human = bool(m.get("is_human"))

            if is_human:
                # Human proficiency formula:
                #   response_rate  (45%) — did they respond at all?
                #   effectiveness  (40%) — did their guidance work?
                #   speed_eff      (15%) — how fast did they respond?
                measured_perf = (
                    0.45 * m.get("response_rate", 0.0)
                    + 0.40 * m.get("effectiveness", 0.0)
                    + 0.15 * m.get("speed_eff", 1.0)
                )
            else:
                # Robot proficiency formula (same as health score)
                measured_perf = (
                    0.40 * m["success_rate"]
                    + 0.35 * m["duration_eff"]
                    + 0.25 * (1.0 - m["intervention_rate"])
                )

            current_prof = registry.get_proficiency(entity_id, cap_id)

            # Confidence-scaled learning rate: when only a few tasks actually ran
            # (mission aborted early), we trust the signal less and make a smaller
            # update. Full alpha is applied only when confidence == 1.0.
            confidence = m.get("confidence", 1.0)
            effective_alpha = alpha * confidence
            proposed_prof = effective_alpha * measured_perf + (1 - effective_alpha) * current_prof
            proposed_prof = max(min_prof, min(max_prof, proposed_prof))

            delta = proposed_prof - current_prof
            if abs(delta) < 1e-4:
                continue  # no meaningful change

            reason_parts: list[str] = []
            if is_human:
                timeouts = m.get("timeouts", 0)
                responses = m.get("responses", 0)
                total_esc = m.get("total", 0)
                ineffective = m.get("ineffective_responses", 0)
                if timeouts > 0:
                    reason_parts.append(f"{timeouts}/{total_esc} 次超时未响应")
                if ineffective > 0:
                    reason_parts.append(f"{ineffective} 次介入后机器仍失败")
                if m.get("effective_responses", 0) > 0:
                    reason_parts.append(f"{m['effective_responses']} 次有效引导")
            else:
                evaluated_total = m.get("evaluated_total", m.get("total", 0))
                cancelled_total = m.get("cancelled_total", 0)
                if cancelled_total > 0:
                    reason_parts.append(
                        f"{evaluated_total}/{evaluated_total + cancelled_total} 任务执行，"
                        f"{cancelled_total} 因任务结束取消"
                    )
                if m["failed"] > 0:
                    reason_parts.append(f"{m['failed']} 次失败")
                if m["total_interventions"] > 0:
                    reason_parts.append(f"{m['total_interventions']} 次干预")
                if m["avg_elapsed_ms"] > 0:
                    baseline_ms = float(fb_cfg.get("duration_baseline_ms", 30_000))
                    if m["avg_elapsed_ms"] > baseline_ms * 1.2:
                        reason_parts.append(f"平均耗时 {m['avg_elapsed_ms']:.0f}ms")

            if is_human:
                metrics_payload = {
                    "response_rate": round(m.get("response_rate", 0.0), 4),
                    "effectiveness": round(m.get("effectiveness", 0.0), 4),
                    "speed_eff": round(m.get("speed_eff", 1.0), 4),
                    "total_escalations": m.get("total", 0),
                    "timeouts": m.get("timeouts", 0),
                    "responses": m.get("responses", 0),
                    "effective_responses": m.get("effective_responses", 0),
                    "ineffective_responses": m.get("ineffective_responses", 0),
                    # aliases so ProficiencyProposalCard can render them generically
                    "success_rate": round(m.get("response_rate", 0.0), 4),
                    "duration_eff": round(m.get("speed_eff", 1.0), 4),
                    "intervention_rate": 0.0,
                    "total_tasks": m.get("total", 0),
                    "evaluated_tasks": m.get("total", 0),
                    "cancelled_tasks": 0,
                    "confidence": 1.0,
                }
            else:
                metrics_payload = {
                    "success_rate": round(m["success_rate"], 4),
                    "duration_eff": round(m["duration_eff"], 4),
                    "intervention_rate": round(m["intervention_rate"], 4),
                    "total_tasks": m["total"],
                    "evaluated_tasks": m.get("evaluated_total", m["total"]),
                    "cancelled_tasks": m.get("cancelled_total", 0),
                    "confidence": round(confidence, 4),
                }

            proposals.append({
                "entity_id": entity_id,
                "entity_name": _entity_display_name(entity_id, is_human),
                "capability_id": cap_id,
                "capability_name": _cap_display_name(cap_id),
                "is_human": is_human,
                "current_proficiency": round(current_prof, 4),
                "proposed_proficiency": round(proposed_prof, 4),
                "delta": round(delta, 4),
                "measured_performance": round(measured_perf, 4),
                "effective_alpha": round(effective_alpha, 4),
                "reason": "；".join(reason_parts) if reason_parts else "基于执行表现自动评估",
                "metrics": metrics_payload,
            })

        if store is not None:
            for p in proposals:
                try:
                    store.log_proposal(
                        task_id=task_id,
                        entity_id=p["entity_id"],
                        capability_id=p["capability_id"],
                        previous_value=p["current_proficiency"],
                        proposed_value=p["proposed_proficiency"],
                        reason=p.get("reason", ""),
                        metrics=p.get("metrics", {}),
                        source="feedback_pipeline",
                    )
                except Exception:
                    logger.exception(
                        "[FeedbackPipeline] proficiency_proposer: log_proposal failed for %s/%s",
                        p.get("entity_id"), p.get("capability_id"),
                    )
            # Push updated proficiency history to frontend (all entities/capabilities in this task)
            try:
                entity_cap_pairs = {(p["entity_id"], p["capability_id"]) for p in proposals}
                history_rows: list[dict] = []
                for entity_id, cap_id in entity_cap_pairs:
                    history_rows.extend(store.get_history(entity_id, cap_id, limit=10))
                if history_rows:
                    zenoh.publish_proficiency_history(history_rows)
            except Exception:
                logger.exception("[FeedbackPipeline] proficiency_proposer: publish_proficiency_history failed")

        if proposals:
            payload = {"task_id": task_id, "proposals": proposals}
            zenoh.publish_proficiency_proposals(payload)
            logger.info(
                "[FeedbackPipeline] proficiency_proposer: %d proposals published",
                len(proposals),
            )
        else:
            logger.info("[FeedbackPipeline] proficiency_proposer: no significant changes")

        zenoh.publish_progress(task_id, "proficiency_proposer", "completed",
                               message=f"能力评估完成，{len(proposals)} 项提案")
        return {"proficiency_proposals": proposals}

    return _node


# ── Factory & runner ──────────────────────────────────────────────────────────

def build_feedback_graph(
    zenoh: "ZenohBridge",
    registry: "CapabilityRegistry",
    config: dict,
    store: "ProficiencyStore | None" = None,
):
    """Compile and return the feedback LangGraph.

    Call once at startup; invoke repeatedly with :func:`run_feedback_pipeline`.
    Pass *store* to enable persistent proficiency logging and bottleneck archiving.
    """
    graph = StateGraph(FeedbackState)

    graph.add_node("metrics_aggregator", _metrics_aggregator_node(zenoh, config))
    graph.add_node("bottleneck_analyzer", _bottleneck_analyzer_node(zenoh, config, store=store))
    graph.add_node("proficiency_proposer", _proficiency_proposer_node(zenoh, registry, config, store=store))

    graph.set_entry_point("metrics_aggregator")
    graph.add_edge("metrics_aggregator", "bottleneck_analyzer")
    graph.add_edge("bottleneck_analyzer", "proficiency_proposer")
    graph.add_edge("proficiency_proposer", END)

    compiled = graph.compile()
    logger.info("[FeedbackPipeline] compiled: metrics_aggregator → bottleneck_analyzer → proficiency_proposer")
    return compiled


def run_feedback_pipeline(
    graph,
    task_id: str,
    task_queue: list[dict],
    profiler_summary: dict,
) -> None:
    """Invoke the compiled feedback graph synchronously in a background thread.

    Errors are caught and logged; they must never propagate to the caller
    (engine post-tick handler).
    """
    try:
        initial_state: FeedbackState = {
            "task_id": task_id,
            "task_queue": task_queue,
            "profiler_summary": profiler_summary,
        }
        graph.invoke(initial_state)
        logger.info("[FeedbackPipeline] run completed for task_id=%s", task_id)
    except Exception:
        logger.exception("[FeedbackPipeline] run failed for task_id=%s", task_id)
