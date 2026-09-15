"""Scoring Lambda: the fast plane's only compute (architecture section 5.1).

Parse the checkpoint, read state in one BatchGetItem, hand the evidence to fraudcore, write the
session and the decision, return. Every scoring decision is made in fraudcore; this module only
translates between HTTP, DynamoDB and fraudcore types.

Fail open (CLAUDE.md section 2). A failure reading state, or any error while scoring, returns
``monitor`` with zero confidence. A failure writing returns the decision anyway. The only responses
that are not a decision are for requests that are malformed or not the caller's to make.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from fraudcore.features import KeystrokeTiming, Transfer
from fraudcore.fusion import FusionModel
from fraudcore.policy import Decision, Thresholds, fail_open
from fraudcore.scoring import ChannelScore
from fraudcore.session import SessionEvidence, score_session
from scoring.request import CheckpointRequest, parse
from scoring.store import SESSION_MAX_FIELDS, ForeignSessionError, ScoringState, Store

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)


class StateStore(Protocol):
    def load(self, uid: str, request: CheckpointRequest, now: float) -> ScoringState: ...

    def save_session(
        self, uid: str, session_id: str, fields: tuple[KeystrokeTiming, ...], now: float
    ) -> None: ...

    def save_decision(
        self,
        uid: str,
        decision_id: str,
        request: CheckpointRequest,
        scores: Mapping[str, ChannelScore],
        decision: Decision,
        now: float,
    ) -> None: ...


@dataclass(frozen=True)
class Dependencies:
    store: StateStore
    model: FusionModel
    thresholds: Thresholds
    clock: Callable[[], float] = time.time
    new_id: Callable[[], str] = field(default=lambda: uuid.uuid4().hex)


_dependencies: Dependencies | None = None


def lambda_handler(event: Mapping[str, Any], context: Any) -> dict[str, Any]:
    global _dependencies
    if _dependencies is None:
        # Built once per execution environment, so warm invocations pay nothing for it.
        import boto3

        _dependencies = Dependencies(
            store=Store(boto3.client("dynamodb"), os.environ["TABLE_NAME"]),
            model=FusionModel.load(),
            thresholds=Thresholds.load(),
        )
    return handle(event, _dependencies)


def handle(event: Mapping[str, Any], deps: Dependencies) -> dict[str, Any]:
    started = time.perf_counter()

    uid = _subject(event)
    if uid is None:
        return _response(401, {"error": "unauthenticated"}, started)

    try:
        request = parse(_body(event))
    except ValueError as error:
        return _response(400, {"error": str(error)}, started)

    now = deps.clock()
    decision_id = deps.new_id()
    scores: Mapping[str, ChannelScore] = {}
    fields = request.fields
    loaded = False

    try:
        state = deps.store.load(uid, request, now)
        loaded = True
        fields = (state.session_fields + request.fields)[-SESSION_MAX_FIELDS:]
        scored = score_session(
            _evidence(state, fields, request, now),
            deps.model,
            deps.thresholds,
            request.checkpoint,
        )
        scores, decision = scored.scores, scored.decision
    except ForeignSessionError:
        return _response(403, {"error": "session belongs to another user"}, started)
    except Exception:
        LOGGER.exception("scoring failed, failing open to monitor")
        decision = fail_open()

    try:
        # Without the earlier fields a session write would erase them, so it is skipped when the
        # read failed. The decision is always recorded.
        if loaded:
            deps.store.save_session(uid, request.session_id, fields, now)
        deps.store.save_decision(uid, decision_id, request, scores, decision, now)
    except Exception:
        LOGGER.exception("write failed, returning the decision anyway")

    return _response(200, _decision_body(decision_id, request, decision), started)


def _evidence(
    state: ScoringState,
    fields: tuple[KeystrokeTiming, ...],
    request: CheckpointRequest,
    now: float,
) -> SessionEvidence:
    transfer = None
    if request.transaction is not None:
        transfer = Transfer(
            amount=request.transaction.amount,
            # UTC, matching the aggregator's hour histogram.
            hour=datetime.fromtimestamp(now, UTC).hour,
            # The 24-hour transfer count comes from the ledger, which arrives in Phase 5. Until
            # then a transfer is counted alone, so velocity contributes nothing.
            transfers_24h=1,
        )
    return SessionEvidence(
        fields=fields,
        profile=state.profile,
        profile_sessions=state.profile_sessions,
        context=state.context,
        transfer=transfer,
        aggregates=state.aggregates,
        edge=state.edge,
        risk=state.risk,
    )


def _subject(event: Mapping[str, Any]) -> str | None:
    claims = (
        event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    )
    subject = claims.get("sub")
    return subject if isinstance(subject, str) and subject else None


def _body(event: Mapping[str, Any]) -> str:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(body, validate=True).decode("utf-8")
    return body


def _decision_body(
    decision_id: str, request: CheckpointRequest, decision: Decision
) -> dict[str, Any]:
    return {
        "decision_id": decision_id,
        "session_id": request.session_id,
        "checkpoint": request.checkpoint,
        "action": decision.action,
        "risk": None if decision.risk is None else round(decision.risk, 2),
        "confidence": round(decision.confidence, 4),
        "contributions": [
            {"channel": contribution.channel, "value": round(contribution.value, 4)}
            for contribution in decision.contributions
        ],
        "constraints": list(decision.constraints),
    }


def _response(status: int, body: Mapping[str, Any], started: float) -> dict[str, Any]:
    elapsed_ms = (time.perf_counter() - started) * 1000
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            # Handler time, readable from the browser and the latency script, so API Gateway and
            # network overhead can be separated from compute.
            "Server-Timing": f"handler;dur={elapsed_ms:.1f}",
        },
        "body": json.dumps(body),
    }
