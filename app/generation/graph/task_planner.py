"""Task Planner node — decomposes the objective into a structured plan."""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from app.config import settings
from app.generation.graph.json_utils import extract_json
from app.generation.graph.progress import publish_step
from app.generation.graph.state import GenerationState

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "task_planner.md"


# ── Output schema ─────────────────────────────────────────────────────────────

class SubTask(BaseModel):
    subtask_id: str
    description: str
    assigned_entity_ids: list[str] = []
    required_capabilities: list[str] = []
    dependencies: list[str] = []
    params: dict[str, Any] = Field(default_factory=dict)


class TaskPlan(BaseModel):
    phases: list[dict[str, Any]] = Field(default_factory=list)
    subtasks: list[SubTask] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    doctrine_notes: str = ""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _strip_extensions(entities: list[dict]) -> list[dict]:
    """Pass only base fields + capabilities to Task Planner (no device extensions)."""
    stripped = []
    for e in entities:
        stripped.append({
            "entity_id": e.get("entity_id"),
            "entity_type": e.get("entity_type") or e.get("type"),
            "display_name": e.get("display_name", ""),
            "status": e.get("status", "idle"),
            "position": e.get("position"),
            "capabilities": [c if isinstance(c, str) else c.get("name") for c in e.get("capabilities", [])],
        })
    return stripped


def _summarize_environment(env: dict) -> dict:
    """Produce a compact environment summary for the LLM.

    Includes zone list (id, name, center, floor, risk, navigable, type),
    topology graph, and landmarks — but strips bulky data like boundary_2d
    and obstacle arrays to stay within token limits.
    """
    zones_raw = env.get("zones", {})
    # Normalize array or dict
    zones_iter: list[tuple[str, dict]] = []
    if isinstance(zones_raw, list):
        for z in zones_raw:
            zones_iter.append((z.get("id", "?"), z))
    elif isinstance(zones_raw, dict):
        zones_iter = list(zones_raw.items())

    compact_zones = []
    for zid, z in zones_iter:
        center = z.get("center", {})
        z_range = z.get("z_range", {})
        zc = {
            "id": zid,
            "name": z.get("display_name") or z.get("name") or zid,
            "type": z.get("type", ""),
            "floor": z.get("floor_name") or z.get("floor", ""),
            "navigable": z.get("navigable", True),
            "center": {
                "x": center.get("x", 0),
                "y": center.get("y", 0),
                "z": center.get("z") or ((z_range.get("min", 0) + z_range.get("max", 0)) / 2 if z_range else 0),
            },
            "shape": z.get("shape", ""),
            "radius": z.get("radius"),
        }
        sem = z.get("semantics", {})
        if sem:
            zc["risk"] = sem.get("risk_level", "")
            zc["terrain"] = sem.get("terrain", "")
            zc["cover"] = sem.get("cover", "")
            zc["visibility"] = sem.get("visibility", "")
        tags = z.get("tags")
        if tags:
            zc["tags"] = tags
        cf = z.get("connects_floors")
        if cf:
            zc["connects_floors"] = cf
        compact_zones.append(zc)

    result: dict[str, Any] = {
        "scene_name": env.get("scene_name") or env.get("map_id", ""),
        "unit": "cm",
    }

    bounds = env.get("bounds")
    if bounds:
        result["bounds"] = bounds

    result["zones"] = compact_zones

    topology = env.get("topology", [])
    if topology:
        result["topology"] = topology

    landmarks = env.get("landmarks", [])
    if landmarks:
        result["landmarks"] = [
            {"id": lm.get("id"), "name": lm.get("name"), "position": lm.get("position"), "type": lm.get("type")}
            for lm in landmarks if lm.get("position")
        ]

    return result


def _load_prompt() -> str:
    if PROMPT_PATH.exists():
        return PROMPT_PATH.read_text(encoding="utf-8")
    # fallback inline prompt
    return """You are a military task planning AI.

Given the following mission context, decompose the objective into phases and subtasks.
Assign each subtask to the most suitable entity based on capabilities.
Return a valid JSON object conforming to TaskPlan schema.

Entities (base info + capabilities):
{entities}

Environment:
{environment}

Task Context:
{task_context}

Respond ONLY with a JSON object."""


# ── LangChain LCEL chain ──────────────────────────────────────────────────────

def _build_chain(model: str):
    llm = ChatOpenAI(
        model=model,
        api_key=settings.openai_api_key or None,
        base_url=settings.openai_base_url or None,
        temperature=0,
    )
    prompt = ChatPromptTemplate.from_template(_load_prompt())
    return prompt | llm | StrOutputParser()


# ── LangGraph node ────────────────────────────────────────────────────────────

async def task_planner_node(state: GenerationState) -> dict:
    """LangGraph node: Task Planner."""
    t0 = time.monotonic()
    model = state.get("planner_model", state.get("llm_model", settings.planner_model))
    task_id = state.get("task_id", "unknown")

    logger.info("[%s] task_planner started (model=%s)", task_id, model)
    publish_step("task_planner", "started", message=f"LLM 分解任务中 (model={model})…")

    env_summary = _summarize_environment(state.get("environment", {}))
    mission_goal = state.get("mission_goal") or {}
    context = {
        "entities": json.dumps(_strip_extensions(state.get("entities", [])), ensure_ascii=False, indent=2),
        "environment": json.dumps(env_summary, ensure_ascii=False, indent=2),
        "task_context": json.dumps(state.get("task_context", {}), ensure_ascii=False, indent=2),
        "mission_goal": json.dumps(mission_goal, ensure_ascii=False, indent=2),
    }

    logger.info("[%s] task_planner LLM request ──────────────────", task_id)
    logger.info("[%s]   model: %s", task_id, model)
    logger.info("[%s]   entities (%d):\n%s", task_id, len(state.get("entities", [])), context["entities"])
    logger.info("[%s]   environment:\n%s", task_id, context["environment"])
    logger.info("[%s]   task_context:\n%s", task_id, context["task_context"])

    chain = _build_chain(model)
    raw_output = await chain.ainvoke(context)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    logger.info("[%s] task_planner completed in %dms", task_id, elapsed_ms)
    logger.info("[%s] task_planner LLM response (%d chars) ──────", task_id, len(raw_output))
    logger.info("[%s]   %s", task_id, raw_output)

    task_plan_dict = extract_json(raw_output)
    n_subtasks = len(task_plan_dict.get("subtasks", []))
    n_phases = len(task_plan_dict.get("phases", []))
    publish_step(
        "task_planner", "completed",
        duration_ms=elapsed_ms,
        message=f"分解为 {n_subtasks} 个子任务, {n_phases} 个阶段",
    )

    trace_entry = {
        "step": "task_planner",
        "status": "completed",
        "elapsed_ms": elapsed_ms,
        "output_summary": {
            "phases": len(task_plan_dict.get("phases", [])),
            "subtasks": len(task_plan_dict.get("subtasks", [])),
        },
    }

    return {
        "task_plan": task_plan_dict,
        "generation_trace": [*state.get("generation_trace", []), trace_entry],
    }
