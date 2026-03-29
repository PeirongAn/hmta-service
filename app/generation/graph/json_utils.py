"""Utilities for robustly extracting JSON from LLM text output."""

from __future__ import annotations

import json
import re
import logging

logger = logging.getLogger(__name__)


def _close_truncated_json(text: str) -> str:
    """
    Attempt to close unclosed braces/brackets in a truncated JSON string.
    Handles strings, escape sequences, nested structures.
    """
    stack: list[str] = []
    in_string = False
    escape_next = False

    for ch in text:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in ("}", "]") and stack:
            stack.pop()

    suffix = "".join(reversed(stack))
    return text + suffix


def extract_json(text: str) -> dict:
    """
    Extract the first valid JSON object from LLM output.

    Handles:
    - Pure JSON strings
    - JSON wrapped in ```json ... ``` or ``` ... ``` code blocks
    - JSON preceded or followed by explanatory text
    - Truncated JSON (unclosed braces/brackets)
    """
    # Already a dict — nothing to parse
    if isinstance(text, dict):
        return text

    text = str(text)

    def _try_parse(candidate: str) -> dict | None:
        """Try to parse candidate JSON; if truncated, attempt bracket repair."""
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        try:
            return json.loads(_close_truncated_json(candidate))
        except json.JSONDecodeError:
            return None

    # 1. Try code fences first: ```json ... ``` or ``` ... ```
    fence_match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text)
    if fence_match:
        result = _try_parse(fence_match.group(1))
        if result is not None:
            return result

    # 2. Find the outermost { ... } span in the text
    start = text.find("{")
    if start != -1:
        depth = 0
        end_idx = start
        for i, ch in enumerate(text[start:], start=start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_idx = i
                    break

        # Complete or truncated span
        candidate = text[start : end_idx + 1]
        result = _try_parse(candidate)
        if result is not None:
            return result

        # Also try the full tail from { (handles truncation without any closing })
        result = _try_parse(text[start:])
        if result is not None:
            return result

    # 3. Last resort: try parsing the whole text
    result = _try_parse(text)
    if result is not None:
        return result

    raise ValueError(f"No valid JSON object found in LLM output (first 200 chars): {text[:200]!r}")


def is_behavior_tree(data: dict) -> bool:
    """Return True if data looks like a BehaviorTree (has tree_id / root_id / nodes)."""
    return (
        isinstance(data, dict)
        and "nodes" in data
        and ("root_id" in data or "tree_id" in data)
    )


def normalize_behavior_tree(data: dict) -> dict:
    """
    Normalize a BehaviorTree dict to ensure `nodes` is always a dict keyed by node_id.

    LLMs sometimes return nodes as a list [{node_id: ..., ...}, ...] instead of
    the expected {node_id: {node_id: ..., ...}} format. This function converts
    the list form to the dict form so downstream validators work correctly.
    """
    nodes = data.get("nodes")
    if isinstance(nodes, list):
        nodes_dict: dict = {}
        for item in nodes:
            if isinstance(item, dict):
                nid = item.get("node_id") or item.get("id")
                if nid:
                    nodes_dict[nid] = {**item, "node_id": nid}
        data = {**data, "nodes": nodes_dict}

    # Ensure root_id exists (fall back to tree_id or first node key)
    if not data.get("root_id"):
        nodes_dict = data.get("nodes", {})
        data = {**data, "root_id": data.get("tree_id") or (next(iter(nodes_dict), ""))}

    return data


_TASK_PLAN_KEYS = {"phases", "subtasks", "doctrine_notes"}


def extract_behavior_tree(text: str | dict) -> dict:
    """
    Extract and normalize a BehaviorTree JSON from LLM output.

    Raises ValueError if the extracted JSON does not look like a BehaviorTree.
    """
    data = extract_json(text)

    # Reject if it looks like a TaskPlan (wrong schema)
    if _TASK_PLAN_KEYS & set(data.keys()):
        raise ValueError(
            f"Extracted JSON looks like a TaskPlan, not a BehaviorTree "
            f"(found keys: {sorted(_TASK_PLAN_KEYS & set(data.keys()))}). "
            "The LLM returned the wrong schema — check the bt_builder prompt."
        )

    if not is_behavior_tree(data):
        raise ValueError(
            f"Extracted JSON is not a BehaviorTree schema "
            f"(missing 'nodes'/'root_id'). Keys found: {list(data.keys())}"
        )

    normalized = normalize_behavior_tree(data)

    # Reject silently-empty behavior trees
    if not normalized.get("nodes"):
        raise ValueError(
            "BehaviorTree has no nodes — the LLM generated an empty tree. "
            "Check the bt_builder prompt for a concrete node schema example."
        )

    return normalized
