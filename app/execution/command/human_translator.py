"""L1 → L2: Human Directive Translator."""

from __future__ import annotations

import logging
from typing import Any

import py_trees

from app.schemas.command import AbstractCommand, DecisionOption, HumanDirective

logger = logging.getLogger(__name__)

_INTENT_TO_DIRECTIVE_TYPE = {
    # Approval / decision intents
    "command.approve": "approval",
    "approve": "approval",
    "approval": "approval",
    # Path planning intents
    "plan_path": "path_planning",
    "plan_patrol": "path_planning",
    "plan_route": "path_planning",
    "path_planning": "path_planning",
    # Observation / reconnaissance intents
    "observe": "observation",
    "reconnaissance": "observation",
    "survey": "observation",
    "observation": "observation",
    # Path planning intents (additional aliases)
    "remote_control": "path_planning",
    "remote_operate": "path_planning",
    # Manual override / takeover intents
    "command.override": "override",
    "override": "override",
    "manual_control": "override",
    # Reporting intents
    "system.report": "report",
    "report": "report",
    # Task assist — EntityWorker human fallback
    "task.assist": "task_assist",
    "task_assist": "task_assist",
}

_AUTHORITY_ALLOWED: dict[str, list[str]] = {
    "operator": ["approve", "reject", "request_recheck", "submit_waypoints", "submit_observation", "zone_specified", "skip", "retry_with_params"],
    "supervisor": ["approve", "reject", "approve_with_conditions", "request_recheck", "override", "submit_waypoints", "submit_observation", "zone_specified", "skip", "retry_with_params"],
    "commander": ["approve", "reject", "approve_with_conditions", "override", "abort_mission", "submit_waypoints", "submit_observation", "zone_specified", "skip", "retry_with_params"],
}

_DIRECTIVE_TYPE_OPTIONS: dict[str, list[tuple[str, str, str, bool]]] = {
    # directive_type → [(option_id, label, risk_level, requires_input)]
    "path_planning": [
        ("submit_waypoints", "提交路径点", "low", False),
        ("reject", "拒绝执行", "none", False),
        ("approve_with_conditions", "修改参数后提交", "medium", True),
    ],
    "observation": [
        ("submit_observation", "提交观察报告", "low", True),
        ("reject", "无法执行", "none", False),
    ],
    "override": [
        ("approve", "切换人工控制", "medium", False),
        ("reject", "保持自动模式", "none", False),
    ],
    "report": [
        ("approve", "确认接收", "low", False),
    ],
    "task_assist": [
        ("approve", "提供参数并重试", "low", True),
        ("zone_specified", "指定执行区域", "low", True),
        ("skip", "跳过此任务", "none", False),
        ("reject", "拒绝执行", "none", False),
    ],
}


class HumanTranslator:
    """Translates an AbstractCommand into a HumanDirective (L2) using Blackboard context."""

    def translate(self, command: AbstractCommand, entity: dict) -> HumanDirective:
        extensions = entity.get("extensions", {})
        bb = py_trees.blackboard.Client(name="human_translator")

        directive_type = _INTENT_TO_DIRECTIVE_TYPE.get(command.intent, "approval")
        situation = self._build_situation_briefing(command, bb)
        options = self._build_decision_options(command, directive_type)
        authority = extensions.get("authority_level", "operator")
        options = self._filter_by_authority(options, authority)

        expected_response = self._build_expected_response(directive_type)

        return HumanDirective(
            directive_id=command.command_id,
            entity_id=command.entity_id,
            directive_type=directive_type,
            task_description=self._generate_description(command),
            situation_briefing=situation,
            recommended_action=self._generate_recommendation(command, situation, directive_type),
            decision_options=options,
            constraints_reminder=command.context.get("constraints", []) if command.context else [],
            urgency=command.priority,
            expected_response=expected_response,
            timeout_sec=command.timeout_sec or 120.0,
        )

    def _build_situation_briefing(self, command: AbstractCommand, bb: py_trees.blackboard.Client) -> dict[str, Any]:
        briefing: dict[str, Any] = {}

        # For task.assist directives, pass through the full task context
        directive_type = _INTENT_TO_DIRECTIVE_TYPE.get(command.intent, "approval")
        if directive_type == "task_assist":
            briefing["entity"] = command.params.get("entity", "")
            briefing["task_id"] = command.params.get("task_id", "")
            briefing["task_intent"] = command.params.get("task_intent", "")
            briefing["failing_step"] = command.params.get("failing_step", "")
            briefing["step_progress"] = command.params.get("step_progress", "")
            briefing["task_params"] = command.params.get("task_params", {})
            briefing["required_capabilities"] = command.params.get("required_capabilities", [])
            briefing["reason"] = command.params.get("reason", "")
            return briefing

        zone_id = command.params.get("zone_id") or command.params.get("target_zone")
        if zone_id:
            try:
                bb.register_key(key=f"/zones/{zone_id}", access=py_trees.common.Access.READ)
                briefing["zone_status"] = bb.get(f"zones/{zone_id}")
            except (KeyError, AttributeError):
                briefing["zone_status"] = {}
        try:
            bb.register_key(key=f"/entities/{command.entity_id}/status", access=py_trees.common.Access.READ)
            briefing["entity_status"] = bb.get(f"entities/{command.entity_id}/status")
        except (KeyError, AttributeError):
            pass
        return briefing

    def _build_decision_options(self, command: AbstractCommand, directive_type: str) -> list[DecisionOption]:
        # Use type-specific options if available
        type_options = _DIRECTIVE_TYPE_OPTIONS.get(directive_type)
        if type_options:
            return [
                DecisionOption(
                    option_id=opt_id,
                    label=label,
                    risk_level=risk,
                    description="" if not requires_input else "需要填写附加信息",
                )
                for opt_id, label, risk, requires_input in type_options
            ]

        # Default approval options
        return [
            DecisionOption(option_id="approve", label="批准执行", risk_level="low"),
            DecisionOption(option_id="reject", label="拒绝", description="取消此动作", risk_level="none"),
            DecisionOption(
                option_id="approve_with_conditions",
                label="附条件批准",
                description="附加额外限制后执行",
                risk_level="medium",
            ),
            DecisionOption(
                option_id="request_recheck",
                label="请求重新验证",
                description="执行前再次验证",
                risk_level="low",
            ),
        ]

    def _build_expected_response(self, directive_type: str) -> dict[str, Any]:
        if directive_type == "path_planning":
            return {
                "type": "waypoints_submission",
                "required_fields": ["selected_option", "waypoints"],
                "description": "在地图上点选路径点后提交",
            }
        if directive_type == "observation":
            return {
                "type": "observation_report",
                "required_fields": ["selected_option", "additional_input"],
                "description": "填写观察报告后提交",
            }
        return {"type": "decision_selection", "required_fields": ["selected_option"]}

    def _filter_by_authority(self, options: list[DecisionOption], authority: str) -> list[DecisionOption]:
        allowed = set(_AUTHORITY_ALLOWED.get(authority, _AUTHORITY_ALLOWED["operator"]))
        return [o for o in options if o.option_id in allowed]

    def _generate_description(self, command: AbstractCommand) -> str:
        zone = command.params.get("zone_id") or command.params.get("target_zone", "")
        target = command.params.get("target") or command.params.get("area", "")
        location = zone or target or "未指定区域"
        directive_type = _INTENT_TO_DIRECTIVE_TYPE.get(command.intent, "approval")
        if directive_type == "path_planning":
            reason = command.params.get("reason", "自动导航路径受阻")
            return (
                f"导航受阻: {reason} | "
                f"机器人 {command.entity_id} 无法自动导航至 {location} | "
                f"请在地图上规划绕行路线后提交"
            )
        if directive_type == "task_assist":
            entity = command.params.get("entity", command.entity_id)
            task_intent = command.params.get("task_intent", "unknown")
            failing_step = command.params.get("failing_step", "")
            step_progress = command.params.get("step_progress", "")
            reason = command.params.get("reason", "执行失败")
            task_params = command.params.get("task_params", {})
            zone_id = task_params.get("zone_id", "")
            zone_str = f" → {zone_id}" if zone_id else ""
            step_info = ""
            if failing_step and failing_step != task_intent:
                step_info = f" (步骤 {step_progress}: {failing_step} 失败)"
            return (
                f"任务协助: {entity} 的 [{task_intent}{zone_str}] 任务{step_info} | "
                f"原因: {reason} | 请选择操作"
            )
        return (
            f"指令类型: {command.intent} | "
            f"执行实体: {command.entity_id} | "
            f"目标位置: {location}"
        )

    def _generate_recommendation(self, command: AbstractCommand, situation: dict, directive_type: str) -> str:
        if directive_type == "path_planning":
            zone = command.params.get("zone_id") or command.params.get("target_zone", "")
            return f"请在地图上点选路径点为 {command.entity_id} 规划绕行路线到「{zone or '目标区域'}」，确保路线安全可行。"
        if directive_type == "observation":
            return "请仔细观察目标区域并填写详细报告。"
        if directive_type == "override":
            return "切换到手动控制模式，确保操作安全。"
        return "建议根据当前态势评估批准执行。"
