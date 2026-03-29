"""T10 — RobotTranslator data-driven mapping tests."""

from __future__ import annotations

import pytest

from app.execution.command.robot_translator import RobotTranslator
from app.schemas.command import AbstractCommand


@pytest.fixture
def translator() -> RobotTranslator:
    t = RobotTranslator()
    t.load_mappings_from_graph({
        "nodes": {
            "robot_A": {"kind": "entity", "attrs": {}},
            "custom_cap": {"kind": "capability", "attrs": {"command_type": "CUSTOM_CMD"}},
        },
        "edges": {
            "hc_1": {
                "kind": "has_capability",
                "nodes": ["robot_A", "custom_cap"],
                "weight": 1.0,
            }
        },
    })
    return t


class TestDynamicMappings:
    def test_graph_mapping_loaded(self, translator: RobotTranslator):
        cmd = AbstractCommand(
            command_id="c1",
            entity_id="robot_A",
            intent="custom_cap",
            params={},
        )
        result = translator.translate(cmd, {"extensions": {}})
        assert result.command_type == "CUSTOM_CMD"

    def test_unknown_intent_generic(self, translator: RobotTranslator):
        cmd = AbstractCommand(
            command_id="c2",
            entity_id="robot_A",
            intent="totally_unknown_intent",
            params={},
        )
        result = translator.translate(cmd, {"extensions": {}})
        assert result.command_type == "TOTALLY_UNKNOWN_INTENT"


class TestBuiltinMappings:
    def test_move_intent(self):
        t = RobotTranslator()
        cmd = AbstractCommand(
            command_id="c3",
            entity_id="robot_A",
            intent="move",
            params={"target_position": [1, 2, 3]},
        )
        result = t.translate(cmd, {"extensions": {}})
        assert result.command_type == "NAVIGATE"

    def test_detect_intent(self):
        t = RobotTranslator()
        cmd = AbstractCommand(
            command_id="c4",
            entity_id="robot_A",
            intent="detect",
            params={},
        )
        result = t.translate(cmd, {"extensions": {}})
        assert result.command_type == "SCAN"


class TestCommandMappingsFile:
    def test_load_from_file_format(self):
        t = RobotTranslator()
        t.load_mappings_from_command_mappings({
            "robot_A": {"patrol": "PATROL_CMD"},
        })
        cmd = AbstractCommand(
            command_id="c5",
            entity_id="robot_A",
            intent="patrol",
            params={},
        )
        result = t.translate(cmd, {"extensions": {}})
        # Dynamic mappings take priority over built-in defaults
        assert result.command_type == "PATROL_CMD"

    def test_register_mapping(self):
        t = RobotTranslator()
        t.register_mapping("fly", "FLY_CMD")
        cmd = AbstractCommand(
            command_id="c6",
            entity_id="uav_1",
            intent="fly",
            params={},
        )
        result = t.translate(cmd, {"extensions": {}})
        assert result.command_type == "FLY_CMD"
