"""BT Builder node — generates or repairs the Behavior Tree JSON."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from app.capability.ontology import get_input_schema, get_preconditions, get_effects
from app.config import settings
from app.generation.graph.json_utils import extract_behavior_tree
from app.generation.graph.progress import publish_step
from app.generation.graph.state import GenerationState

logger = logging.getLogger(__name__)

PROMPT_BUILD_PATH = Path(__file__).parent.parent / "prompts" / "bt_builder.md"
PROMPT_FIX_PATH = Path(__file__).parent.parent / "prompts" / "bt_fix.md"

_INLINE_BUILD_PROMPT = """You are a behavior tree architect for robot/human task allocation.

Given a task plan and entity capabilities, generate a behavior tree as a JSON object.
The tree must use node types: sequence, selector, parallel, condition, action, humanGate, timeout, retry.
CRITICAL: The "intent" field of action nodes must be copied verbatim from the capability id listed below. If listed as "some_id (中文名)", use "some_id".
CRITICAL: For each action node, populate "params" with values from the subtask's "params" field and context. NEVER leave params empty when the Task Plan provides values.

Entity Capabilities:
{capabilities}

Task Plan:
{task_plan}

Return ONLY a JSON object matching the BehaviorTree schema:
{{
  "tree_id": "bt_<task_id>",
  "root_id": "<node_id>",
  "nodes": {{
    "<node_id>": {{
      "node_id": "<id>",
      "type": "<type>",
      "name": "<name>",
      "children": [],
      "intent": "<capability_id from above>",
      "entity": "<entity_id>",
      "params": {{"<copy from subtask params>"}}
    }}
  }},
  "metadata": {{}}
}}"""

_INLINE_FIX_PROMPT = """You are a behavior tree repair specialist.

The following behavior tree failed validation. Fix ONLY the violated nodes.
Do NOT restructure unrelated parts of the tree.

Current behavior tree:
{behavior_tree}

Violations to fix:
{violations}

Return ONLY the corrected JSON object (same schema as input)."""


def _format_preconditions_effects(entities: list[dict]) -> str:
    """Collect preconditions/effects for all skills referenced by entities."""
    seen: set[str] = set()
    lines: list[str] = []
    for e in entities:
        for c in e.get("capabilities", []):
            cap_id = c.get("id") or c.get("name") or str(c) if isinstance(c, dict) else str(c)
            if cap_id in seen:
                continue
            seen.add(cap_id)
            preconds = get_preconditions(cap_id)
            effects = get_effects(cap_id)
            if preconds or effects:
                lines.append(f"- **{cap_id}**: preconditions={preconds}, effects={effects}")
    return "\n".join(lines) if lines else "(no preconditions/effects defined)"


def _format_input_schemas(entities: list[dict]) -> str:
    """Collect input schemas for all skills referenced by entities."""
    seen: set[str] = set()
    lines: list[str] = []
    for e in entities:
        for c in e.get("capabilities", []):
            cap_id = c.get("id") or c.get("name") or str(c) if isinstance(c, dict) else str(c)
            if cap_id in seen:
                continue
            seen.add(cap_id)
            schema = get_input_schema(cap_id)
            if schema:
                params_summary = []
                for pname, pdef in schema.items():
                    req = "REQUIRED" if pdef.get("required") else "optional"
                    src = pdef.get("source", "config")
                    params_summary.append(f"    {pname}: {pdef.get('type','?')} ({req}, source={src})")
                lines.append(f"- **{cap_id}**:\n" + "\n".join(params_summary))
    return "\n".join(lines) if lines else "(no input schemas defined)"


def _extract_capabilities(entities: list[dict]) -> list[dict]:
    result = []
    for e in entities:
        caps = []
        for c in e.get("capabilities", []):
            if isinstance(c, str):
                caps.append(c)
            elif isinstance(c, dict):
                caps.append(c.get("id") or c.get("name") or str(c))
            else:
                caps.append(str(c))
        result.append({
            "entity_id": e.get("entity_id"),
            "entity_type": e.get("entity_type") or e.get("type"),
            "capabilities": caps,
        })
    return result


def _load_prompt(path: Path, fallback: str) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else fallback


def _build_chain(model: str, template: str):
    llm = ChatOpenAI(
        model=model,
        api_key=settings.openai_api_key or None,
        base_url=settings.openai_base_url or None,
        temperature=0,
    )
    prompt = ChatPromptTemplate.from_template(template)
    return prompt | llm | StrOutputParser()


async def bt_builder_node(state: GenerationState) -> dict:
    """LangGraph node: BT Builder (initial build or repair)."""
    t0 = time.monotonic()
    task_id = state.get("task_id", "unknown")
    violations = state.get("violations")
    is_repair = bool(violations)
    model = state.get("fixer_model", settings.fixer_model) if is_repair else state.get(
        "builder_model", settings.builder_model
    )

    logger.info("[%s] bt_builder started (repair=%s, model=%s)", task_id, is_repair, model)
    iteration = state.get("iteration_count", 0)
    max_iters = state.get("max_iterations", 3)
    if is_repair:
        publish_step(
            "bt_builder", "started",
            message=f"LLM 修复行为树 (repair, model={model})…",
            iteration=iteration + 1, max_iterations=max_iters,
        )
    else:
        publish_step(
            "bt_builder", "started",
            message=f"LLM 构建行为树 (model={model})…",
            iteration=iteration + 1, max_iterations=max_iters,
        )

    task_plan = dict(state.get("task_plan") or {})
    current_phase_id = state.get("current_phase_id")
    phases = task_plan.get("phases", [])

    if is_repair:
        template = _load_prompt(PROMPT_FIX_PATH, _INLINE_FIX_PROMPT)
        context = {
            "behavior_tree": json.dumps(state.get("behavior_tree", {}), ensure_ascii=False, indent=2),
            "violations": json.dumps(violations, ensure_ascii=False, indent=2),
        }
    else:
        entities = state.get("entities", [])
        template = _load_prompt(PROMPT_BUILD_PATH, _INLINE_BUILD_PROMPT)
        if not current_phase_id and phases:
            current_phase_id = phases[0].get("phase_id")
            logger.info("[%s] bt_builder: no current_phase_id specified, auto-selecting first phase: %s", task_id, current_phase_id)
        if current_phase_id:
            all_subtasks = task_plan.get("subtasks", [])
            phase_subtasks = [
                st for st in all_subtasks
                if st.get("phase_id") == current_phase_id
            ]
            if phase_subtasks:
                task_plan = {**task_plan, "subtasks": phase_subtasks}
                logger.info(
                    "[%s] bt_builder: filtering to phase '%s' → %d/%d subtasks",
                    task_id, current_phase_id, len(phase_subtasks), len(all_subtasks),
                )
            else:
                logger.warning(
                    "[%s] bt_builder: phase '%s' has no subtasks, using all %d",
                    task_id, current_phase_id, len(all_subtasks),
                )

        context = {
            "capabilities": json.dumps(
                _extract_capabilities(entities), ensure_ascii=False, indent=2
            ),
            "task_plan": json.dumps(task_plan, ensure_ascii=False, indent=2),
            "skill_preconditions_effects": _format_preconditions_effects(entities),
            "skill_input_schemas": _format_input_schemas(entities),
        }

    logger.info("[%s] bt_builder LLM request ──────────────────", task_id)
    logger.info("[%s]   model: %s, repair: %s", task_id, model, is_repair)
    for k, v in context.items():
        logger.info("[%s]   %s:\n%s", task_id, k, v)

    chain = _build_chain(model, template)
    raw_output = await chain.ainvoke(context)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    logger.info("[%s] bt_builder completed in %dms (repair=%s)", task_id, elapsed_ms, is_repair)
    logger.info("[%s] bt_builder LLM response (%d chars) ──────", task_id, len(raw_output))
    logger.info("[%s]   %s", task_id, raw_output)

    behavior_tree_dict = extract_behavior_tree(raw_output)

    if behavior_tree_dict and current_phase_id:
        metadata = behavior_tree_dict.get("metadata") or {}
        metadata["phase_id"] = current_phase_id
        metadata["total_phases"] = len(phases)
        behavior_tree_dict["metadata"] = metadata
        logger.info("[%s] bt_builder: wrote phase_id=%s total_phases=%d into BT metadata", task_id, current_phase_id, len(phases))

    n_nodes = len((behavior_tree_dict or {}).get("nodes", {}))
    publish_step(
        "bt_builder", "completed",
        duration_ms=elapsed_ms,
        message=f"行为树已{'修复' if is_repair else '构建'}, {n_nodes} 个节点",
        iteration=iteration + 1, max_iterations=max_iters,
    )

    trace_entry = {
        "step": "bt_builder",
        "status": "completed",
        "repair": is_repair,
        "elapsed_ms": elapsed_ms,
        "node_count": len((behavior_tree_dict or {}).get("nodes", {})),
    }

    return {
        "behavior_tree": behavior_tree_dict,
        "current_phase_id": current_phase_id,
        "total_phases": len(phases),
        "violations": None,                            # clear previous violations
        "iteration_count": state.get("iteration_count", 0) + 1,
        "generation_trace": [*state.get("generation_trace", []), trace_entry],
    }
