"""Publishes scoring decisions to the event bus."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from fraudcore.events import SCORING_SOURCE


class PublishError(Exception):
    """EventBridge accepted the call but rejected the entry."""


class Publisher:
    def __init__(self, client: Any, bus_name: str) -> None:
        self._client = client
        self._bus = bus_name

    def publish(self, detail_type: str, detail: Mapping[str, Any]) -> None:
        response = self._client.put_events(
            Entries=[
                {
                    "EventBusName": self._bus,
                    "Source": SCORING_SOURCE,
                    "DetailType": detail_type,
                    "Detail": json.dumps(detail),
                }
            ]
        )
        # PutEvents reports per-entry failures in the response rather than raising.
        if response.get("FailedEntryCount"):
            raise PublishError(str(response.get("Entries")))
