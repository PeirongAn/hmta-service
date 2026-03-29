"""Unit tests for FSM Manager."""

import pytest

from app.execution.fsm.fsm_manager import FSMManager


@pytest.fixture
def manager():
    m = FSMManager()
    m.create_instance("robot_1", "robot")
    m.create_instance("human_1", "human")
    return m


def test_initial_state_is_idle(manager: FSMManager):
    assert manager.get_state("robot_1") == "idle"
    assert manager.get_state("human_1") == "idle"


def test_robot_normal_transition(manager: FSMManager):
    ok = manager.trigger("robot_1", "move_command")
    assert ok
    assert manager.get_state("robot_1") == "moving"

    manager.trigger("robot_1", "waypoint_reached")
    assert manager.get_state("robot_1") == "idle"


def test_robot_scan_to_target_marked(manager: FSMManager):
    manager.trigger("robot_1", "scan_command")
    assert manager.get_state("robot_1") == "scanning"
    manager.trigger("robot_1", "explosive_detected")
    assert manager.get_state("robot_1") == "target_marked"


def test_robot_disarm_flow(manager: FSMManager):
    manager.trigger("robot_1", "scan_command")
    manager.trigger("robot_1", "explosive_detected")
    manager.trigger("robot_1", "disarm_command")
    assert manager.get_state("robot_1") == "disarming"
    manager.trigger("robot_1", "disarm_complete")
    assert manager.get_state("robot_1") == "idle"


def test_robot_comm_lost_from_any_state(manager: FSMManager):
    for event in ["move_command", "comm_lost"]:
        manager.trigger("robot_1", event)
    assert manager.get_state("robot_1") == "offline"
    manager.trigger("robot_1", "comm_restored")
    assert manager.get_state("robot_1") == "idle"


def test_invalid_transition_silently_ignored(manager: FSMManager):
    ok = manager.trigger("robot_1", "nonexistent_event")
    assert not ok
    assert manager.get_state("robot_1") == "idle"   # unchanged


def test_unknown_entity_returns_false(manager: FSMManager):
    ok = manager.trigger("ghost_entity", "move_command")
    assert not ok


def test_human_approval_shortcut(manager: FSMManager):
    manager.trigger("human_1", "approval_requested")
    assert manager.get_state("human_1") == "deciding"
    manager.trigger("human_1", "approval_given")
    assert manager.get_state("human_1") == "idle"


def test_transition_history_recorded(manager: FSMManager):
    manager.trigger("robot_1", "move_command")
    history = manager.get_transition_history("robot_1")
    assert len(history) >= 1
    assert history[-1]["to"] == "moving"
    assert history[-1]["trigger"] == "move_command"


def test_load_definitions(manager: FSMManager):
    fsm_defs = [
        {"entity_id": "robot_2", "entity_type": "robot"},
        {"entity_id": "human_2", "entity_type": "human"},
    ]
    manager.load_definitions(fsm_defs)
    assert "robot_2" in manager.instances
    assert "human_2" in manager.instances
