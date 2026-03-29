"""Human Adapter — subscribes to Zenoh human responses and routes to ResponseResolver."""

from __future__ import annotations

import logging

from app.schemas.command import HumanResponse

logger = logging.getLogger(__name__)


class HumanAdapter:
    def __init__(self, zenoh_bridge, response_resolver):
        self._zenoh = zenoh_bridge
        self._rr = response_resolver

    def start(self) -> None:
        self._zenoh.subscribe_human_responses(self._handle_response)
        logger.info("HumanAdapter subscribed to human responses")

    def _handle_response(self, data: dict) -> None:
        try:
            response = HumanResponse(**data)
            self._rr.handle_response(response)
        except Exception:
            logger.exception("Failed to process human response: %s", data)
