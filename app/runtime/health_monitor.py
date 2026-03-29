"""Entity health monitoring via heartbeat timeout detection."""

from __future__ import annotations

import logging
import time
from typing import Callable

logger = logging.getLogger(__name__)


class HealthMonitor:
    """Track entity heartbeats and fire callbacks on timeout."""

    def __init__(
        self,
        timeout_sec: float = 5.0,
        on_offline: Callable[[str], None] | None = None,
        on_online: Callable[[str], None] | None = None,
    ) -> None:
        self._timeout = timeout_sec
        self._on_offline = on_offline
        self._on_online = on_online
        self._last_seen: dict[str, float] = {}
        self._alive: set[str] = set()

    def heartbeat(self, entity_id: str) -> None:
        """Record a heartbeat for *entity_id*."""
        self._last_seen[entity_id] = time.monotonic()
        if entity_id not in self._alive:
            self._alive.add(entity_id)
            if self._on_online:
                self._on_online(entity_id)

    def check(self) -> list[str]:
        """Check all entities and return newly-offline IDs."""
        now = time.monotonic()
        newly_offline: list[str] = []
        for eid, ts in list(self._last_seen.items()):
            if now - ts > self._timeout and eid in self._alive:
                self._alive.discard(eid)
                newly_offline.append(eid)
                logger.warning("Entity %s heartbeat timeout", eid)
                if self._on_offline:
                    self._on_offline(eid)
        return newly_offline

    @property
    def alive_entities(self) -> set[str]:
        return set(self._alive)
