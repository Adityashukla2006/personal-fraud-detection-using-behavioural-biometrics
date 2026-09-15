"""Adaptation Lambda: the consumer of passkey-verified step-ups (architecture section 5.3).

That rule is the most important one in the system: profile learning becomes conditional on
independent verification rather than on repetition. In this phase the function records each
verified step-up against the user. Section 7's trust score reads the record as ``c_verify = 1.0``
when the robust profile update arrives in Phase 7.

    USER#<uid> / VERIFIED#<decision_id>  transfer_id, method, verified_at, received_at, TTL 180 days
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping
from typing import Any

from botocore.exceptions import ClientError

from fraudcore.events import (
    STEPUP_VERIFIED,
    WORKFLOW_SOURCE,
    StepUpVerified,
    parse_stepup_verified,
)

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

# Matches the trusted buffer's retention, which these records will gate.
VERIFIED_TTL_SECONDS = 180 * 86_400


class VerificationStore:
    def __init__(self, client: Any, table: str, clock: Callable[[], float] = time.time) -> None:
        self._client = client
        self._table = table
        self._clock = clock

    def record(self, verified: StepUpVerified) -> bool:
        """Store a verified step-up once. Returns False for a redelivered event."""
        now = int(self._clock())
        item: dict[str, Any] = {
            "PK": {"S": f"USER#{verified.uid}"},
            "SK": {"S": f"VERIFIED#{verified.decision_id}"},
            "transfer_id": {"S": verified.transfer_id},
            "method": {"S": verified.method},
            "received_at": {"N": str(now)},
            "ttl": {"N": str(now + VERIFIED_TTL_SECONDS)},
        }
        if verified.verified_at is not None:
            item["verified_at"] = {"N": str(verified.verified_at)}
        try:
            self._client.put_item(
                TableName=self._table, Item=item, ConditionExpression="attribute_not_exists(PK)"
            )
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            return False
        return True


def handle(event: Mapping[str, Any], store: VerificationStore) -> dict[str, Any]:
    if event.get("source") != WORKFLOW_SOURCE or event.get("detail-type") != STEPUP_VERIFIED:
        LOGGER.warning("ignoring unexpected event %s", event.get("detail-type"))
        return {"recorded": False, "reason": "unexpected_event"}

    verified = parse_stepup_verified(event.get("detail") or {})
    # The rule already matches passkey verification only. Checking again here means a rule edited by
    # mistake cannot let weaker evidence gate a profile update.
    if verified.method != "passkey":
        LOGGER.warning("ignoring %s verification for %s", verified.method, verified.decision_id)
        return {"recorded": False, "reason": "not_passkey"}

    recorded = store.record(verified)
    return {"recorded": recorded, "reason": "recorded" if recorded else "duplicate"}


_store: VerificationStore | None = None


def lambda_handler(event: Mapping[str, Any], context: Any) -> dict[str, Any]:
    global _store
    if _store is None:
        import boto3

        _store = VerificationStore(boto3.client("dynamodb"), os.environ["TABLE_NAME"])
    return handle(event, _store)
