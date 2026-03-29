"""IDE callback for extreme L5-level exceptions.

When the RuleReallocator cannot handle an anomaly (all candidates
offline, mission impossible), the Runtime calls back to the IDE
for full re-planning via LLM.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


class IDECallback:
    """Communicate with a remote IDE instance for re-planning."""

    def __init__(
        self,
        zenoh_bridge: Any = None,
        ide_url: str = "",
    ) -> None:
        self._zenoh = zenoh_bridge
        self._ide_url = ide_url

    def request_replan(
        self,
        reason: str,
        current_state: dict,
        failed_entities: list[str],
    ) -> dict | None:
        """Request re-planning from the IDE.

        Tries Zenoh first, falls back to HTTP if configured.
        Returns the new BT dict or None on failure.
        """
        payload = {
            "type": "replan_request",
            "reason": reason,
            "current_state": current_state,
            "failed_entities": failed_entities,
        }

        if self._zenoh:
            return self._try_zenoh(payload)

        if self._ide_url:
            return self._try_http(payload)

        logger.error("IDECallback: no transport configured")
        return None

    def _try_zenoh(self, payload: dict) -> dict | None:
        try:
            self._zenoh.publish("zho/runtime/replan/request", payload)
            logger.info("Replan request sent via Zenoh")
            return None  # async — response comes via subscription
        except Exception:
            logger.exception("Zenoh replan request failed")
            return None

    def _try_http(self, payload: dict) -> dict | None:
        try:
            import httpx
            resp = httpx.post(
                f"{self._ide_url}/api/replan",
                json=payload,
                timeout=60.0,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception:
            logger.exception("HTTP replan request failed")
            return None
