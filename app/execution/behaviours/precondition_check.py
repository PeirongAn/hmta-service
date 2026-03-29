"""PreconditionCheck — py_trees Behaviour that checks skill preconditions.

Unlike ZenohCondition (which checks arbitrary blackboard keys), this
behaviour checks the formal preconditions defined in the capability
ontology for a specific skill.  Each precondition is evaluated against
the current agent state and the Blackboard (where upstream effects are
written by CommandAction on SUCCESS).

Returns SUCCESS if all preconditions are satisfied, FAILURE otherwise.
"""

from __future__ import annotations

import logging

import py_trees

from app.capability.ontology import check_preconditions

logger = logging.getLogger(__name__)


class PreconditionCheck(py_trees.behaviour.Behaviour):
    """
    Leaf node: checks that all preconditions for ``skill_name`` are
    satisfied before the corresponding action node runs.

    Parameters
    ----------
    name       : display name for the BT visualizer
    skill_name : canonical skill id (e.g. ``"detect"``, ``"visual_confirm"``)
    entity_id  : entity that will execute the skill (used for state lookup)
    """

    def __init__(self, name: str, skill_name: str, entity_id: str):
        super().__init__(name=name)
        self.skill_name = skill_name
        self.entity_id = entity_id

    def update(self) -> py_trees.common.Status:
        bb_storage = py_trees.blackboard.Blackboard.storage
        agent_state = bb_storage.get(f"entity_state/{self.entity_id}", {})

        unsatisfied = check_preconditions(
            self.skill_name,
            agent_state=agent_state,
            blackboard_storage=dict(bb_storage),
        )

        if not unsatisfied:
            return py_trees.common.Status.SUCCESS

        self.feedback_message = f"unsatisfied preconditions: {unsatisfied}"
        logger.debug(
            "[%s] precondition check FAILED for skill '%s' entity '%s': %s",
            self.name, self.skill_name, self.entity_id, unsatisfied,
        )
        return py_trees.common.Status.FAILURE
