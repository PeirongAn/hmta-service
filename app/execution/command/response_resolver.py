"""Response Resolver — maps Human responses to CommandStatus + Blackboard updates.

Two routing mechanisms for human responses:

1. **Legacy path**: Writes global blackboard keys (``approval_result``, etc.)
   for CommandAction / HumanGate behaviours in the old LLM-generated BT.
2. **Generic HMTA path**: Writes ``human_response`` directly onto the matching
   task in ``blackboard["task_queue"]`` so EntityWorker can pick it up in
   ``_handle_human_fallback``.
"""

from __future__ import annotations

import logging

import py_trees

from app.schemas.command import HumanResponse

logger = logging.getLogger(__name__)

_BB_KEYS = (
    "approval_result",
    "rejection_reason",
    "approval_conditions",
    "planned_waypoints",
    "recheck_requested",
)


def _bb_set(key: str, value: object) -> None:
    """Write directly to the Blackboard storage, bypassing client access checks.

    ResponseResolver runs outside the tick-loop thread (from a Zenoh callback),
    so it cannot own keys that may already be registered by engine_init or
    behaviour nodes.  Direct storage access is the sanctioned escape hatch in
    py_trees for cross-cutting writers.

    py_trees BlackboardClient stores keys with a leading ``/`` prefix,
    so we write to both ``/key`` and ``key`` for maximum compatibility.
    """
    storage = py_trees.blackboard.Blackboard.storage
    storage[f"/{key}"] = value
    storage[key] = value


class ResponseResolver:
    def __init__(self, command_resolver):
        self._cr = command_resolver

    def handle_response(self, response: HumanResponse) -> None:
        option = response.selected_option

        if option in ("approve", "proceed"):
            _bb_set("approval_result", "approved")
            self._cr.update_status(response.response_id, "completed")
        elif option in ("reject", "abort"):
            _bb_set("approval_result", "rejected")
            _bb_set("rejection_reason", response.reason or "")
            self._cr.update_status(response.response_id, "failed")
        elif option == "approve_with_conditions":
            _bb_set("approval_result", "approved")
            _bb_set("approval_conditions", response.conditions or "")
            self._cr.update_status(response.response_id, "completed")
        elif option == "submit_waypoints":
            _bb_set("approval_result", "approved")
            if response.waypoints:
                _bb_set("planned_waypoints", response.waypoints)
                logger.info(
                    "Waypoints received for directive %s: %d point(s)",
                    response.response_id, len(response.waypoints),
                )
                status = self._cr.get_status(response.response_id)
                node_id = status.node_id or ""
                if node_id.startswith("py-"):
                    suffix = node_id[3:]
                    _bb_set(f"param_response/{suffix}", {"waypoints": response.waypoints})
                    logger.info(
                        "Wrote waypoints to param_response/%s for HumanGate pickup", suffix,
                    )
            self._cr.update_status(response.response_id, "completed")
        elif option == "request_recheck":
            _bb_set("recheck_requested", True)
            self._cr.update_status(response.response_id, "failed")
        elif option in ("skip", "skip_task"):
            self._cr.update_status(response.response_id, "completed")
        elif option == "zone_specified":
            self._cr.update_status(response.response_id, "completed")
        elif option == "retry_with_params":
            self._cr.update_status(response.response_id, "completed")
        else:
            logger.warning("Unknown response option '%s' for directive %s", option, response.response_id)
            self._cr.update_status(response.response_id, "failed")

        # ── Generic HMTA: route response into task_queue ──────────────────────
        self._apply_to_task_queue(response)

        logger.info("Human response processed: directive=%s option=%s", response.response_id, option)

    # ── Task queue integration ────────────────────────────────────────────────

    def _apply_to_task_queue(self, response: HumanResponse) -> None:
        """Write ``human_response`` onto the matching task in ``task_queue``.

        This is the primary mechanism for EntityWorker to receive human
        decisions (provided params, skip, zone assignment, etc.).
        """
        task_id = self._resolve_task_id(response)
        if not task_id:
            logger.warning(
                "Cannot route human response to task_queue: "
                "response_id=%s — _resolve_task_id returned empty. "
                "directive_task_map keys=%s, commands keys(first 10)=%s",
                response.response_id,
                list(self._cr._directive_task_map.keys())[:10] if hasattr(self._cr, "_directive_task_map") else "N/A",
                list(self._cr._commands.keys())[:10] if hasattr(self._cr, "_commands") else "N/A",
            )
            return

        storage = py_trees.blackboard.Blackboard.storage
        queue: list[dict] | None = storage.get("/task_queue") or storage.get("task_queue")
        if not queue:
            logger.warning(
                "Cannot route human response: task_queue not found on Blackboard "
                "(storage keys sample: %s)",
                [k for k in list(storage.keys())[:20] if "task" in str(k).lower()],
            )
            return

        option = response.selected_option
        for task in queue:
            if task.get("id") != task_id:
                continue

            human_resp: dict = {
                "decision": option,
                "params": {},
            }

            if option in ("skip", "skip_task", "reject", "abort"):
                human_resp["decision"] = "skip"
            elif option == "zone_specified":
                zone_id = (response.response_data or {}).get("zone_id", "")
                human_resp["decision"] = "retry"
                human_resp["params"] = {"zone_id": zone_id}
            elif option in ("approve", "proceed", "approve_with_conditions"):
                human_resp["decision"] = "retry"
                if response.waypoints:
                    human_resp["params"]["waypoints"] = response.waypoints
                if response.response_data:
                    human_resp["params"].update(response.response_data)
                if response.conditions:
                    human_resp["params"]["conditions"] = response.conditions
            elif option == "submit_waypoints":
                human_resp["decision"] = "retry"
                if response.waypoints:
                    human_resp["params"]["waypoints"] = response.waypoints
            elif option == "retry_with_params":
                human_resp["decision"] = "retry"
                rd = response.response_data or {}
                scan_params = rd.get("scan_params") or {}
                if scan_params:
                    human_resp["params"].update(scan_params)
                human_resp["params"] = {
                    **human_resp["params"],
                    **{k: v for k, v in rd.items() if k not in ("scan_params", "task_id")},
                }
            else:
                human_resp["decision"] = "retry"

            task["human_response"] = human_resp
            logger.info(
                "Wrote human_response to task %s: decision=%s params=%s",
                task_id, human_resp["decision"], list(human_resp["params"].keys()),
            )
            break

    def _resolve_task_id(self, response: HumanResponse) -> str:
        """Derive the task_id from the directive's metadata."""
        # Method 1: CommandResolver directive→task mapping
        if hasattr(self._cr, "_directive_task_map"):
            task_id = self._cr._directive_task_map.get(response.response_id, "")
            if task_id:
                logger.debug("_resolve_task_id: method1 hit response_id=%s → task_id=%s", response.response_id, task_id)
                return task_id

        # Method 2: Derive from node_id (format: "fallback_{task_id}")
        status = self._cr.get_status(response.response_id)
        node_id = status.node_id or ""
        if node_id.startswith("fallback_"):
            tid = node_id[len("fallback_"):]
            logger.debug("_resolve_task_id: method2 hit node_id=%s → task_id=%s", node_id, tid)
            return tid

        # Method 3: response_data may carry task_id from the frontend
        rd = response.response_data or {}
        if rd.get("task_id"):
            logger.debug("_resolve_task_id: method3 (response_data) → task_id=%s", rd["task_id"])
            return rd["task_id"]

        logger.warning(
            "_resolve_task_id: FAILED for response_id=%s node_id=%s response_data_keys=%s",
            response.response_id, node_id, list(rd.keys()),
        )
        return ""
