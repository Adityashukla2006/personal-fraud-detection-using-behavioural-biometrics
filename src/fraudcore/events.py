"""Typed events on the bfd event bus.

One definition of each event's detail, shared by every producer and consumer, and by research when
it reads the audit lake back. Details carry a schema version so the lake can outlive a change of
shape.

Nothing behavioural is published. A decision event carries the scores, contributions and action --
what was decided and why -- never the keystroke timing it was decided from (CLAUDE.md section 2).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fraudcore.policy import Decision
from fraudcore.scoring import ChannelScore

SCHEMA_VERSION = 1

SCORING_SOURCE = "bfd.scoring"
WORKFLOW_SOURCE = "bfd.workflow"

DECISION_SCORED = "decision.scored"
STEPUP_VERIFIED = "stepup.verified"
TRANSFER_COMPLETED = "transfer.completed"
DETAIL_TYPES = frozenset({DECISION_SCORED, STEPUP_VERIFIED, TRANSFER_COMPLETED})

# Identifiers that become part of an S3 key: no separators, no traversal.
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,64}")


def decision_detail(
    *,
    decision_id: str,
    session_id: str,
    uid: str,
    checkpoint: str,
    device_class: str,
    decision: Decision,
    scores: Mapping[str, ChannelScore],
    created_at: float,
    payee_id: str | None = None,
    amount: float | None = None,
) -> dict[str, Any]:
    """The ``decision.scored`` detail. A transaction is included only for a transfer checkpoint."""
    detail: dict[str, Any] = {
        "schema": SCHEMA_VERSION,
        "decision_id": decision_id,
        "session_id": session_id,
        "uid": uid,
        "checkpoint": checkpoint,
        "device_class": device_class,
        "action": decision.action,
        "risk": decision.risk,
        "confidence": decision.confidence,
        "contributions": [
            {"channel": contribution.channel, "value": contribution.value}
            for contribution in decision.contributions
        ],
        "constraints": list(decision.constraints),
        "scores": {
            name: {"score": score.score, "confidence": score.confidence}
            for name, score in scores.items()
        },
        "created_at": datetime.fromtimestamp(created_at, UTC).isoformat(timespec="milliseconds"),
    }
    if payee_id is not None and amount is not None:
        detail["transaction"] = {"payee_id": payee_id, "amount": amount}
    return detail


@dataclass(frozen=True)
class StepUpVerified:
    uid: str
    transfer_id: str
    decision_id: str
    method: str
    verified_at: int | None


def parse_stepup_verified(detail: Mapping[str, Any]) -> StepUpVerified:
    if detail.get("schema") != SCHEMA_VERSION:
        raise ValueError(f"unsupported schema {detail.get('schema')!r}")

    identifiers = {name: detail.get(name) for name in ("uid", "transfer_id", "decision_id")}
    for name, value in identifiers.items():
        if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
            raise ValueError(f"{name} is missing or malformed")

    verification = detail.get("verification")
    if not isinstance(verification, Mapping) or not isinstance(verification.get("method"), str):
        raise ValueError("verification.method is missing")

    verified_at = verification.get("verified_at")
    usable = isinstance(verified_at, int | float) and not isinstance(verified_at, bool)
    return StepUpVerified(
        uid=identifiers["uid"],  # type: ignore[arg-type]
        transfer_id=identifiers["transfer_id"],  # type: ignore[arg-type]
        decision_id=identifiers["decision_id"],  # type: ignore[arg-type]
        method=verification["method"],
        verified_at=int(verified_at) if usable else None,
    )


def lake_key(event: Mapping[str, Any]) -> str:
    """The audit-lake object key for one EventBridge event.

    Partitioned by detail type, then UTC date, for Athena partition projection. The file name leads
    with the decision or transfer id, so one subject's events can be found by prefix, and ends with
    the event id, so a redelivered event overwrites its own object instead of duplicating it.
    """
    detail_type = event.get("detail-type")
    if not isinstance(detail_type, str) or detail_type not in DETAIL_TYPES:
        raise ValueError(f"unknown detail type {detail_type!r}")

    detail = event.get("detail") or {}
    subject = detail.get("decision_id" if detail_type == DECISION_SCORED else "transfer_id")
    for name, value in (("subject", subject), ("id", event.get("id"))):
        if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
            raise ValueError(f"event {name} is missing or malformed")

    moment = datetime.fromisoformat(str(event["time"]).replace("Z", "+00:00")).astimezone(UTC)
    return f"events/type={detail_type}/dt={moment:%Y-%m-%d}/{subject}.{event['id']}.json"
