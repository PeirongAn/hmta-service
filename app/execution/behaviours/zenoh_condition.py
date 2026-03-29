"""ZenohCondition — reads a blackboard key written by BlackboardSync and compares."""

from __future__ import annotations

import logging
import operator as op
from typing import Any

import py_trees

logger = logging.getLogger(__name__)

_OPS = {
    "eq": op.eq,
    "ne": op.ne,
    "gt": op.gt,
    "lt": op.lt,
    "ge": op.ge,
    "le": op.le,
}


_CONDITION_COUNTER = 0


class ZenohCondition(py_trees.behaviour.Behaviour):
    """
    ConditionNode that checks a py_trees blackboard key (kept in sync with
    Zenoh entity state by BlackboardSync).

    Parameters
    ----------
    key      : blackboard key, e.g. "entities/robot_1/status"
    expected : expected value
    operator : "eq" | "ne" | "gt" | "lt" | "ge" | "le"  (default "eq")
    """

    def __init__(
        self,
        name: str,
        key: str,
        expected: Any,
        operator: str = "eq",
    ):
        super().__init__(name=name)
        self.key = key
        self.expected = expected
        self._op = _OPS.get(operator, op.eq)
        global _CONDITION_COUNTER
        _CONDITION_COUNTER += 1
        client_name = f"cond_{_CONDITION_COUNTER}_{name[:20]}"
        self._bb = py_trees.blackboard.Client(name=client_name)
        self._bb.register_key(key=key, access=py_trees.common.Access.READ)

    def update(self) -> py_trees.common.Status:
        try:
            value = self._bb.get(self.key)
        except KeyError:
            self.feedback_message = f"key '{self.key}' not in blackboard"
            logger.debug("[%s] key '%s' not found → FAILURE", self.name, self.key)
            return py_trees.common.Status.FAILURE

        result = self._op(value, self.expected)
        if result:
            logger.debug("[%s] %s=%r == %r → SUCCESS", self.name, self.key, value, self.expected)
            return py_trees.common.Status.SUCCESS
        self.feedback_message = f"{self.key}={value!r} (expected {self.expected!r})"
        return py_trees.common.Status.FAILURE
