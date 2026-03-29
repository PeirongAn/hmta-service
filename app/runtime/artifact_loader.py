"""Load and validate a compiled scenario package for Runtime execution."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


class ArtifactLoader:
    """Loads all JSON artifacts from a compiled scenario directory."""

    def __init__(self, scenario_dir: Path | str) -> None:
        self.root = Path(scenario_dir)
        self.manifest: dict[str, Any] = {}
        self.behavior_tree: dict = {}
        self.fsm_definitions: list[dict] = []
        self.blackboard_init: dict = {}
        self.allocation_rules: dict = {}
        self.command_mappings: dict = {}
        self.contingency_plans: dict = {}
        self.capability_snapshot: dict = {}
        self.runtime_config: dict = {}
        self.reallocation_rules: dict = {}

    def load(self) -> None:
        """Load all artifacts.  Raises on missing required files."""
        errors = self._validate()
        if errors:
            raise FileNotFoundError(
                f"Invalid scenario package at {self.root}: " + "; ".join(errors)
            )

        self.manifest = self._read("manifest.json")
        self.behavior_tree = self._read("artifacts/behavior_tree.json")
        self.fsm_definitions = self._read("artifacts/fsm_definitions.json")
        self.blackboard_init = self._read("artifacts/blackboard_init.json")
        self.allocation_rules = self._read("artifacts/allocation_rules.json")
        self.command_mappings = self._read("artifacts/command_mappings.json")
        self.contingency_plans = self._read("artifacts/contingency_plans.json")
        self.capability_snapshot = self._read("artifacts/capability_snapshot.json")

        config_dir = self.root / "config"
        if (config_dir / "runtime.yaml").exists():
            self.runtime_config = self._read("config/runtime.yaml")
        if (config_dir / "reallocation_rules.yaml").exists():
            self.reallocation_rules = self._read("config/reallocation_rules.yaml")

        logger.info(
            "Loaded scenario %s v%s (built %s)",
            self.manifest.get("scenario_name"),
            self.manifest.get("version"),
            self.manifest.get("build_time"),
        )

    def _read(self, rel_path: str) -> Any:
        p = self.root / rel_path
        with open(p, encoding="utf-8") as f:
            if p.suffix in (".yaml", ".yml"):
                return yaml.safe_load(f) or {}
            return json.load(f)

    def _validate(self) -> list[str]:
        errors: list[str] = []
        required = [
            "manifest.json",
            "artifacts/behavior_tree.json",
            "artifacts/fsm_definitions.json",
            "artifacts/blackboard_init.json",
            "artifacts/allocation_rules.json",
            "artifacts/command_mappings.json",
            "artifacts/contingency_plans.json",
            "artifacts/capability_snapshot.json",
        ]
        for rel in required:
            if not (self.root / rel).exists():
                errors.append(f"Missing: {rel}")
        return errors
