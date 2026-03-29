"""Standalone Runtime entry point — loads compiled artifacts and runs.

Usage::

    python -m app.runtime.runtime_main --scenario ./build/scenario-xxx-1.0.0/

No LLM required.  Connects to real devices (or UE5) via Zenoh.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
from pathlib import Path

logger = logging.getLogger("runtime")


def main() -> None:
    parser = argparse.ArgumentParser(description="HMTA Runtime")
    parser.add_argument("--scenario", required=True, help="Path to compiled scenario package")
    parser.add_argument("--zenoh", default="tcp/localhost:7447", help="Zenoh router address")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))

    scenario_dir = Path(args.scenario)
    if not scenario_dir.exists():
        logger.error("Scenario directory not found: %s", scenario_dir)
        sys.exit(1)

    # Lazy imports so startup is fast even if some deps are optional
    from app.runtime.artifact_loader import ArtifactLoader
    from app.runtime.health_monitor import HealthMonitor
    from app.runtime.ide_callback import IDECallback
    from app.runtime.rule_reallocator import RuleReallocator
    from app.zenoh_bridge import ZenohBridge

    # 1. Load artifacts
    loader = ArtifactLoader(scenario_dir)
    loader.load()
    logger.info("Artifacts loaded for scenario: %s", loader.manifest.get("scenario_name"))

    # 1b. Open Zenoh session for runtime communication
    zenoh_bridge = ZenohBridge(router_url=args.zenoh)
    zenoh_bridge.open()

    # 2. Initialise runtime components
    reallocator = RuleReallocator(
        allocation_rules=loader.allocation_rules,
        contingency_plans=loader.contingency_plans,
    )

    ide_callback = IDECallback(
        zenoh_bridge=zenoh_bridge,
        ide_url=loader.runtime_config.get("ide_callback_url", ""),
    )

    health_monitor = HealthMonitor(
        timeout_sec=loader.runtime_config.get("heartbeat_timeout_sec", 5.0),
        on_offline=lambda eid: _handle_offline(eid, reallocator, ide_callback),
        on_online=lambda eid: reallocator.on_entity_online(eid),
    )

    logger.info("Runtime initialised — entering main loop")
    logger.info("Press Ctrl+C to stop")

    # 3. Main tick loop
    running = True

    def _stop(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    tick_rate = loader.runtime_config.get("tick_rate_hz", 10)
    tick_interval = 1.0 / tick_rate

    while running:
        t0 = time.monotonic()
        health_monitor.check()
        elapsed = time.monotonic() - t0
        sleep_time = max(tick_interval - elapsed, 0)
        time.sleep(sleep_time)

    zenoh_bridge.close()
    logger.info("Runtime stopped")


def _handle_offline(
    entity_id: str,
    reallocator,
    ide_callback,
) -> None:
    result = reallocator.on_entity_offline(entity_id)
    if result:
        logger.info("Reallocated: %s", result)
    elif reallocator.needs_ide_callback():
        logger.warning("All candidates exhausted — requesting IDE replan")
        ide_callback.request_replan(
            reason="all_candidates_offline",
            current_state={},
            failed_entities=[entity_id],
        )


if __name__ == "__main__":
    main()
