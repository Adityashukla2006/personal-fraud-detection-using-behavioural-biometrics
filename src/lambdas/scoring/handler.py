"""Scoring Lambda: the fast plane's only compute (architecture section 5.1).

Parse the checkpoint, read state in one BatchGetItem, hand the evidence to fraudcore, write the
session and the decision, publish the decision event, start the transfer workflow if this is a
confirmation, return. Every scoring decision is made in fraudcore; this module only translates
between HTTP, DynamoDB, EventBridge, Step Functions and fraudcore types.

Fail open (CLAUDE.md section 2). A failure reading state, or any error while scoring, returns
``monitor`` with zero confidence. A failure writing or publishing returns the decision anyway. The
only responses that are not a decision are for requests that are malformed or not the caller's to
make.

Replay. A confirmed session's timing shingles join the user's replay history, and every later
checkpoint is checked against that history. Only whole, confirmed sessions are remembered, so a
session never matches its own earlier checkpoints.

Latency is a reported result, so the response's Server-Timing header carries each stage's share:
``handler`` in total, then ``read``, ``score``, ``write``, ``publish`` and, at confirmation,
``workflow``. HTTP APIs do not support X-Ray, and a tracing SDK would be a runtime dependency, so
this is how the in-function breakdown is measured.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from fraudcore.events import DECISION_SCORED, decision_detail
from fraudcore.features import KeystrokeTiming, Transfer
from fraudcore.fusion import FusionModel
from fraudcore.policy import Decision, Response, Thresholds, fail_open, response_for
from fraudcore.scoring import ChannelScore
from fraudcore.session import SessionEvidence, score_session
from scoring.publisher import Publisher
from scoring.request import CheckpointRequest, parse
from scoring.store import SESSION_MAX_FIELDS, ForeignSessionError, ScoringState, Store
from scoring.workflow import Workflow

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

    def save_replay(
        self, uid: str, state: ScoringState, fields: tuple[KeystrokeTiming, ...], now: float
    ) -> None: ...


class TransferWorkflow(Protocol):
    def start(
        self,
        uid: str,
        transfer_id: str,
        request: CheckpointRequest,
        decision: Decision,
        response: Response,
    ) -> None: ...


class EventPublisher(Protocol):
    def publish(self, detail_type: str, detail: Mapping[str, Any]) -> None: ...


@dataclass(frozen=True)
class Dependencies:
    store: StateStore
    model: FusionModel
    thresholds: Thresholds
    clock: Callable[[], float] = time.time
    new_id: Callable[[], str] = field(default=lambda: uuid.uuid4().hex)
    workflow: TransferWorkflow | None = None
    events: EventPublisher | None = None


# How a confirmed transfer's state is reported back to the client, per workflow response.
TRANSFER_STATUS: dict[str, str] = {
    "release": "processing",
    "step_up": "awaiting_step_up",
    "review": "under_review",
    "cancel": "blocked",
}


class Timings:
    """Milliseconds spent in each stage of one request, in the order they ran."""

    def __init__(self) -> None:
        self.started = time.perf_counter()
        self.stages: dict[str, float] = {}

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        began = time.perf_counter()
        try:
            yield
        finally:
            self.stages[name] = self.stages.get(name, 0.0) + (time.perf_counter() - began) * 1000

    def header(self) -> str:
        total = (time.perf_counter() - self.started) * 1000
        parts = [f"handler;dur={total:.1f}"]
        parts += [f"{name};dur={value:.1f}" for name, value in self.stages.items()]
        return ", ".join(parts)


def _build() -> Dependencies:
    import boto3

    return Dependencies(
        store=Store(boto3.client("dynamodb"), os.environ["TABLE_NAME"]),
        model=FusionModel.load(),
        thresholds=Thresholds.load(),
        workflow=Workflow(
            boto3.client("stepfunctions"),
            os.environ["STATE_MACHINE_ARN"],
            int(os.environ["STEP_UP_TIMEOUT_SECONDS"]),
            int(os.environ["REVIEW_TIMEOUT_SECONDS"]),
        ),
        events=Publisher(boto3.client("events"), os.environ["EVENT_BUS_NAME"]),
    )


# Built during the Lambda init phase when running in Lambda, so creating clients and loading the
# model is paid before the first request rather than inside it. Importing this module elsewhere, in
# tests for instance, builds nothing and needs no AWS.
_IN_LAMBDA = bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))
_dependencies: Dependencies | None = _build() if _IN_LAMBDA else None


def lambda_handler(event: Mapping[str, Any], context: Any) -> dict[str, Any]:
    global _dependencies
    if _dependencies is None:
        _dependencies = _build()
    return handle(event, _dependencies)


def handle(event: Mapping[str, Any], deps: Dependencies) -> dict[str, Any]:
    timings = Timings()

    uid = _subject(event)
    if uid is None:
        return _response(401, {"error": "unauthenticated"}, timings)

    try:
        request = parse(_body(event))
    except ValueError as error:
        return _response(400, {"error": str(error)}, timings)

    now = deps.clock()
    decision_id = deps.new_id()
    scores: Mapping[str, ChannelScore] = {}
    fields = request.fields
    state: ScoringState | None = None

    try:
        with timings.stage("read"):
            state = deps.store.load(uid, request, now)
        with timings.stage("score"):
            fields = (state.session_fields + request.fields)[-SESSION_MAX_FIELDS:]
            scored = score_session(
                _evidence(state, fields, request, now),
                deps.model,
                deps.thresholds,
                request.checkpoint,
            )
        scores, decision = scored.scores, scored.decision
    except ForeignSessionError:
        return _response(403, {"error": "session belongs to another user"}, timings)
    except Exception:
        LOGGER.exception("scoring failed, failing open to monitor")
        decision = fail_open()

    try:
        with timings.stage("write"):
            # Without the earlier fields a session write would erase them, so it is skipped when
            # the read failed. The decision is always recorded.
            if state is not None:
                deps.store.save_session(uid, request.session_id, fields, now)
            deps.store.save_decision(uid, decision_id, request, scores, decision, now)
            if state is not None and request.checkpoint == "confirmation":
                deps.store.save_replay(uid, state, fields, now)
    except Exception:
        LOGGER.exception("write failed, returning the decision anyway")

    with timings.stage("publish"):
        _publish(uid, decision_id, request, scores, decision, now, deps)

    body = _decision_body(decision_id, request, decision)
    if request.checkpoint == "confirmation" and deps.workflow is not None:
        with timings.stage("workflow"):
            body["transfer"] = _start_transfer(uid, decision_id, request, decision, deps.workflow)
    return _response(200, body, timings)


def _publish(
    uid: str,
    decision_id: str,
    request: CheckpointRequest,
    scores: Mapping[str, ChannelScore],
    decision: Decision,
    now: float,
    deps: Dependencies,
) -> None:
    if deps.events is None:
        return
    transaction = request.transaction
    try:
        deps.events.publish(
            DECISION_SCORED,
            decision_detail(
                decision_id=decision_id,
                session_id=request.session_id,
                uid=uid,
                checkpoint=request.checkpoint,
                device_class=request.device.device_class,
                decision=decision,
                scores=scores,
                created_at=now,
                payee_id=None if transaction is None else transaction.payee_id,
                amount=None if transaction is None else transaction.amount,
            ),
        )
    except Exception:
        # The audit trail is downstream of the decision, never a condition on it.
        LOGGER.exception("could not publish the decision event")


def _start_transfer(
    uid: str,
    transfer_id: str,
    request: CheckpointRequest,
    decision: Decision,
    workflow: TransferWorkflow,
) -> dict[str, str]:
    response = response_for(decision.action)
    try:
        workflow.start(uid, transfer_id, request, decision, response)
    except Exception:
        # Not a risk decision: the transfer simply never started, so nothing is debited and the
        # client is told so plainly.
        LOGGER.exception("could not start the transfer workflow")
        return {"transfer_id": transfer_id, "status": "failed"}
    return {"transfer_id": transfer_id, "status": TRANSFER_STATUS[response]}


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
            # The 24-hour transfer count will come from the ledger's history. Until the aggregator
            # computes it, a transfer is counted alone, so velocity contributes nothing.
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
        replay_history=state.replay_history,
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


def _response(status: int, body: Mapping[str, Any], timings: Timings) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            # Per-stage handler time, readable from the browser and the simulator, so API Gateway
            # and network overhead can be separated from compute.
            "Server-Timing": timings.header(),
        },
        "body": json.dumps(body),
    }
