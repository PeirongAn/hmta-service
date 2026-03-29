"""T2 — Capability ontology tests."""

from __future__ import annotations

import pytest

from app.capability.ontology import (
    all_capabilities,
    capability_similarity,
    is_subcapability,
    load_ontology,
    resolve_alias,
)


@pytest.fixture(autouse=True)
def _reload_ontology():
    load_ontology()


class TestAliasResolution:
    def test_chinese_alias(self):
        assert resolve_alias("嗅探") == "detect"

    def test_identity(self):
        assert resolve_alias("move") == "move"

    def test_unknown_passthrough(self):
        assert resolve_alias("totally_unknown_xyz") == "totally_unknown_xyz"


class TestSubcapability:
    def test_patrol_is_sub_of_move(self):
        assert is_subcapability("patrol", "move") is True

    def test_move_is_not_sub_of_detect(self):
        assert is_subcapability("move", "detect") is False

    def test_self_is_not_sub(self):
        assert is_subcapability("move", "move") is True  # identity


class TestSimilarity:
    def test_same_capability(self):
        assert capability_similarity("move", "move") == 1.0

    def test_parent_child(self):
        assert capability_similarity("patrol", "move") > capability_similarity("patrol", "detect")

    def test_same_category(self):
        sim = capability_similarity("scan", "detect")
        assert sim >= 0.5

    def test_different_category(self):
        assert capability_similarity("move", "disarm") == 0.0


class TestAllCapabilities:
    def test_non_empty(self):
        caps = all_capabilities()
        assert len(caps) > 0
        assert "move" in caps
