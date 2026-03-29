"""Unit tests for CommandResolver."""

from unittest.mock import MagicMock

import pytest

from app.execution.command.command_resolver import CommandResolver
from app.schemas.command import AbstractCommand


def _make_resolver():
    zenoh = MagicMock()
    schema_reg = {
        "robot_1": {
            "entity_id": "robot_1",
            "entity_type": "robot",
            "status": "idle",
            "comm_status": "online",
            "capabilities": [
                {"name": "navigation.move_to"},
                {"name": "perception.scan_zone"},
            ],
            "extensions": {},
        },
        "human_1": {
            "entity_id": "human_1",
            "entity_type": "human",
            "status": "idle",
            "comm_status": "online",
            "capabilities": [{"name": "command.approve"}],
            "extensions": {"authority_level": "operator"},
        },
    }
    cap_reg = {
        "robot_1": {"navigation.move_to", "perception.scan_zone"},
        "human_1": {"command.approve"},
    }
    return CommandResolver(schema_reg, cap_reg, zenoh), zenoh


def test_resolve_robot_command():
    resolver, zenoh = _make_resolver()
    cmd = AbstractCommand(intent="navigation.move_to", entity_id="robot_1", params={"target_zone": "zone_a"})
    result = resolver.resolve(cmd)
    assert result.ok
    assert result.command_id == cmd.command_id
    zenoh.publish_robot_command.assert_called_once()


def test_resolve_human_directive():
    resolver, zenoh = _make_resolver()
    cmd = AbstractCommand(intent="command.approve", entity_id="human_1", params={})
    result = resolver.resolve(cmd)
    assert result.ok
    zenoh.publish_human_directive.assert_called_once()


def test_entity_not_found():
    resolver, _ = _make_resolver()
    cmd = AbstractCommand(intent="navigation.move_to", entity_id="ghost")
    result = resolver.resolve(cmd)
    assert result.error == "ENTITY_NOT_FOUND"


def test_capability_mismatch():
    resolver, _ = _make_resolver()
    cmd = AbstractCommand(intent="manipulation.disarm", entity_id="robot_1")
    result = resolver.resolve(cmd)
    assert result.error == "CAPABILITY_MISMATCH"


def test_entity_offline():
    resolver, zenoh = _make_resolver()
    resolver._schema["robot_1"]["status"] = "offline"
    cmd = AbstractCommand(intent="navigation.move_to", entity_id="robot_1")
    result = resolver.resolve(cmd)
    assert result.error == "ENTITY_OFFLINE"


def test_status_tracking():
    resolver, _ = _make_resolver()
    cmd = AbstractCommand(intent="navigation.move_to", entity_id="robot_1", params={"target_zone": "a"})
    result = resolver.resolve(cmd)
    status = resolver.get_status(result.command_id)
    assert status.state == "dispatched"

    resolver.update_status(result.command_id, "completed")
    assert resolver.get_status(result.command_id).state == "completed"


def test_cancel():
    resolver, _ = _make_resolver()
    cmd = AbstractCommand(intent="navigation.move_to", entity_id="robot_1", params={"target_zone": "a"})
    result = resolver.resolve(cmd)
    resolver.cancel(result.command_id)
    assert resolver.get_status(result.command_id).state == "cancelled"
