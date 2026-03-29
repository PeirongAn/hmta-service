"""Goal Extractor node — lightweight LLM call to extract mission_goal from task context.

Runs BEFORE task_planner in the pipeline. Produces a structured mission_goal
dict that downstream nodes (task_planner, bt_template_builder, fsm_bb_init)
can consume.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI

from app.config import settings
from app.generation.graph.json_utils import extract_json
from app.generation.graph.progress import publish_step
from app.generation.graph.state import GenerationState

logger = logging.getLogger(__name__)

PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "goal_extractor.md"


def _load_prompt() -> str:
    if PROMPT_PATH.exists():
        return PROMPT_PATH.read_text(encoding="utf-8")
    return """Analyze the mission objective and return a JSON object with:
- success_condition: {key, expected} or null
- failure_fallback: "human_intervention" or "complete"
- parallel_policy: "success_on_one" or "success_on_all"
- requires_confirmation: true or false

Task Context:
{task_context}

Respond ONLY with JSON."""


def _build_chain(model: str):
    llm = ChatOpenAI(
        model=model,
        api_key=settings.openai_api_key or None,
        base_url=settings.openai_base_url or None,
        temperature=0,
    )
    prompt = ChatPromptTemplate.from_template(_load_prompt())
    return prompt | llm | StrOutputParser()


async def goal_extractor_node(state: GenerationState) -> dict:
    """LangGraph node: Goal Extractor — extract mission_goal before task planning."""
    t0 = time.monotonic()
    model = state.get("planner_model", state.get("llm_model", settings.planner_model))
    task_id = state.get("task_id", "unknown")

    logger.info("[%s] goal_extractor started (model=%s)", task_id, model)
    publish_step("goal_extractor", "started", message="提取任务目标…")

    context = {
        "task_context": json.dumps(
            state.get("task_context", {}), ensure_ascii=False, indent=2,
        ),
    }

    chain = _build_chain(model)
    raw_output = await chain.ainvoke(context)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    logger.info("[%s] goal_extractor completed in %dms", task_id, elapsed_ms)
    logger.info("[%s] goal_extractor response: %s", task_id, raw_output)

    mission_goal = extract_json(raw_output)

    if mission_goal.get("success_condition") is None:
        mission_goal["success_condition"] = None
    if "requires_confirmation" not in mission_goal:
        has_condition = mission_goal.get("success_condition") is not None
        mission_goal["requires_confirmation"] = has_condition

    publish_step(
        "goal_extractor", "completed",
        duration_ms=elapsed_ms,
        message=f"任务目标: {mission_goal.get('success_condition') or '无明确目标'}",
    )

    trace_entry = {
        "step": "goal_extractor",
        "status": "completed",
        "elapsed_ms": elapsed_ms,
        "output_summary": {
            "has_goal": mission_goal.get("success_condition") is not None,
            "requires_confirmation": mission_goal.get("requires_confirmation", False),
        },
    }

    return {
        "mission_goal": mission_goal,
        "generation_trace": [*state.get("generation_trace", []), trace_entry],
    }
