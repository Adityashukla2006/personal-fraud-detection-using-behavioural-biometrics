"""Tests for the scoring handler, with a fake store: no AWS, no network.

Fail-open is a CLAUDE.md section 2 rule. The dependency-failure tests here fail if the handler ever
lets a store or scoring error escape, or answers one with anything but ``monitor``.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Any

import pytest

from fraudcore.features import KeystrokeTiming, SessionContext
from fraudcore.fusion import FusionModel
from fraudcore.policy import ACTIONS, Decision, Thresholds
from fraudcore.scoring import ChannelScore
from scoring import handler
from scoring.request import CheckpointRequest
from scoring.store import ForeignSessionError, ScoringState

NOW = 1_767_225_600.0
PRIOR = KeystrokeTiming(hold=(0.1, 0.2, 0.3), down_down=(0.5, 0.6), up_down=(0.4, 0.4))
EMPTY = ScoringState(
    profile=None,
    profile_sessions=0,
    context=SessionContext(None, None, None, 0),
    edge=None,
    risk=None,
    aggregates=None,
    session_fields=(),
)


class FakeStore:
    def __init__(
        self,
        state: ScoringState = EMPTY,
        load_error: Exception | None = None,
        save_error: Exception | None = None,
    ) -> None:
        self.state = state
        self.load_error = load_error
        self.save_error = save_error
        self.sessions: list[tuple[str, str, tuple[KeystrokeTiming, ...]]] = []
        self.decisions: list[tuple[str, str, Decision]] = []

    def load(self, uid: str, request: CheckpointRequest, now: float) -> ScoringState:
        if self.load_error:
            raise self.load_error
        return self.state

    def save_session(
        self, uid: str, session_id: str, fields: tuple[KeystrokeTiming, ...], now: float
    ) -> None:
        if self.save_error:
            raise self.save_error
        self.sessions.append((uid, session_id, fields))

    def save_decision(
        self,
        uid: str,
        decision_id: str,
        request: CheckpointRequest,
        scores: Mapping[str, ChannelScore],
        decision: Decision,
        now: float,
    ) -> None:
        if self.save_error:
            raise self.save_error
        self.decisions.append((uid, decision_id, decision))


def _payload(**overrides: Any) -> dict[str, Any]:
    hold = [0.09, 0.11, 0.08, 0.10, 0.12, 0.09]
    down_down = [0.21, 0.35, 0.18, 0.42, 0.27]
    payload: dict[str, Any] = {
        "session_id": "session-0001",
        "checkpoint": "login",
        "device": {
            "device_id": "device-0001",
            "max_touch_points": 0,
            "coarse_pointer": False,
            "short_side_px": 1080,
        },
        "behaviour": {
            "fields": [
                {
                    "hold": hold,
                    "down_down": down_down,
                    "up_down": [round(d - h, 4) for d, h in zip(down_down, hold, strict=False)],
                    "backspaces": 0,
                    "corrections": 0,
                    "pastes": 0,
                }
            ]
        },
    }
    payload.update(overrides)
    return payload


def _event(payload: Any, subject: str | None = "user-1") -> dict[str, Any]:
    claims = {} if subject is None else {"sub": subject}
    return {
        "requestContext": {"authorizer": {"jwt": {"claims": claims}}},
        "body": payload if isinstance(payload, str) else json.dumps(payload),
        "isBase64Encoded": False,
    }


class FakeWorkflow:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.started: list[tuple[str, str, str, str]] = []

    def start(
        self,
        uid: str,
        transfer_id: str,
        request: CheckpointRequest,
        decision: Decision,
        response: str,
    ) -> None:
        if self.error:
            raise self.error
        self.started.append((uid, transfer_id, decision.action, response))


def _call(
    event: dict[str, Any], store: FakeStore, workflow: FakeWorkflow | None = None
) -> tuple[int, dict[str, Any], dict[str, Any]]:
    deps = handler.Dependencies(
        store=store,
        model=FusionModel.load(),
        thresholds=Thresholds.load(),
        clock=lambda: NOW,
        new_id=lambda: "dec-1",
        workflow=workflow,
    )
    response = handler.handle(event, deps)
    return response["statusCode"], json.loads(response["body"]), response["headers"]


class TestScoredDecision:
    def test_a_checkpoint_returns_and_records_a_decision(self) -> None:
        store = FakeStore()
        status, body, headers = _call(_event(_payload()), store)

        assert status == 200
        assert body["action"] in ACTIONS
        assert 0 <= body["risk"] <= 100
        assert len(body["contributions"]) == 3
        assert headers["Server-Timing"].startswith("handler;dur=")
        assert store.decisions[0][:2] == ("user-1", "dec-1")

    def test_session_evidence_accumulates_across_checkpoints(self) -> None:
        store = FakeStore(state=EMPTY.__class__(**{**EMPTY.__dict__, "session_fields": (PRIOR,)}))
        _call(_event(_payload()), store)
        assert len(store.sessions[0][2]) == 2
        assert store.sessions[0][2][0] == PRIOR

    def test_a_base64_body_is_decoded(self) -> None:
        event = _event(_payload())
        event["body"] = base64.b64encode(event["body"].encode()).decode()
        event["isBase64Encoded"] = True
        assert _call(event, FakeStore())[0] == 200


class TestRejectedRequests:
    def test_a_request_without_a_subject_is_unauthenticated(self) -> None:
        assert _call(_event(_payload(), subject=None), FakeStore())[0] == 401

    def test_malformed_json_is_a_bad_request(self) -> None:
        store = FakeStore()
        assert _call(_event("{nope"), store)[0] == 400
        assert not store.decisions

    def test_a_content_field_is_a_bad_request_and_nothing_is_stored(self) -> None:
        payload = _payload()
        payload["behaviour"]["fields"][0]["key"] = "h"
        store = FakeStore()
        status, body, _ = _call(_event(payload), store)
        assert status == 400
        assert "unexpected keys" in body["error"]
        assert not store.decisions and not store.sessions

    def test_another_users_session_is_forbidden(self) -> None:
        store = FakeStore(load_error=ForeignSessionError("SESS#session-0001"))
        assert _call(_event(_payload()), store)[0] == 403


class TestFailOpen:
    def test_a_dependency_error_returns_monitor_with_no_confidence(self) -> None:
        store = FakeStore(load_error=RuntimeError("dynamodb unavailable"))
        status, body, _ = _call(_event(_payload()), store)

        assert status == 200
        assert body["action"] == "monitor"
        assert body["confidence"] == 0.0
        assert body["risk"] is None
        assert body["constraints"] == ["fail_open"]

    def test_a_failed_read_records_the_decision_but_never_overwrites_the_session(self) -> None:
        store = FakeStore(load_error=RuntimeError("dynamodb unavailable"))
        _call(_event(_payload()), store)
        assert store.decisions and not store.sessions

    def test_an_error_inside_scoring_also_fails_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def explode(*args: object, **kwargs: object) -> None:
            raise ZeroDivisionError("scoring bug")

        monkeypatch.setattr(handler, "score_session", explode)
        status, body, _ = _call(_event(_payload()), FakeStore())
        assert (status, body["action"]) == (200, "monitor")

    def test_a_write_failure_still_returns_the_decision(self) -> None:
        store = FakeStore(save_error=RuntimeError("dynamodb unavailable"))
        status, body, _ = _call(_event(_payload()), store)
        assert status == 200
        assert body["constraints"] != ["fail_open"]

    def test_a_fail_open_confirmation_releases_the_transfer(self) -> None:
        workflow = FakeWorkflow()
        store = FakeStore(load_error=RuntimeError("dynamodb unavailable"))
        _, body, _ = _call(_event(_confirmation()), store, workflow)
        assert workflow.started == [("user-1", "dec-1", "monitor", "release")]
        assert body["transfer"] == {"transfer_id": "dec-1", "status": "processing"}


def _confirmation() -> dict[str, Any]:
    return _payload(checkpoint="confirmation", transaction={"payee_id": "a" * 64, "amount": 250})


class TestTransferWorkflow:
    def test_a_confirmation_starts_the_workflow_with_the_policy_response(self) -> None:
        workflow = FakeWorkflow()
        status, body, _ = _call(_event(_confirmation()), FakeStore(), workflow)

        assert status == 200
        uid, transfer_id, action, response = workflow.started[0]
        assert (uid, transfer_id, action) == ("user-1", "dec-1", body["action"])
        assert body["transfer"] == {
            "transfer_id": "dec-1",
            "status": handler.TRANSFER_STATUS[response],
        }

    def test_earlier_checkpoints_never_start_a_transfer(self) -> None:
        workflow = FakeWorkflow()
        _, body, _ = _call(_event(_payload()), FakeStore(), workflow)
        assert workflow.started == []
        assert "transfer" not in body

    def test_a_workflow_that_cannot_start_reports_a_failed_transfer(self) -> None:
        workflow = FakeWorkflow(error=RuntimeError("states unavailable"))
        status, body, _ = _call(_event(_confirmation()), FakeStore(), workflow)
        assert status == 200
        assert body["transfer"]["status"] == "failed"

    def test_every_workflow_response_has_a_client_status(self) -> None:
        assert set(handler.TRANSFER_STATUS) == {"release", "step_up", "review", "cancel"}


class FakeEvents:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.published: list[tuple[str, dict[str, Any]]] = []

    def publish(self, detail_type: str, detail: Mapping[str, Any]) -> None:
        if self.error:
            raise self.error
        self.published.append((detail_type, dict(detail)))


def _call_with_events(
    event: dict[str, Any], store: FakeStore, events: FakeEvents
) -> tuple[int, dict[str, Any]]:
    deps = handler.Dependencies(
        store=store,
        model=FusionModel.load(),
        thresholds=Thresholds.load(),
        clock=lambda: NOW,
        new_id=lambda: "dec-1",
        events=events,
    )
    response = handler.handle(event, deps)
    return response["statusCode"], json.loads(response["body"])


class TestDecisionEvents:
    def test_every_decision_is_published_as_it_was_returned(self) -> None:
        events = FakeEvents()
        _, body = _call_with_events(_event(_payload()), FakeStore(), events)

        detail_type, detail = events.published[0]
        assert detail_type == "decision.scored"
        assert (detail["decision_id"], detail["action"]) == ("dec-1", body["action"])
        assert detail["uid"] == "user-1"
        assert "transaction" not in detail

    def test_the_event_never_carries_keystroke_timing(self) -> None:
        events = FakeEvents()
        _call_with_events(_event(_payload()), FakeStore(), events)
        serialised = json.dumps(events.published[0][1])
        for timing_key in ("fields", "hold", "down_down", "up_down"):
            assert f'"{timing_key}"' not in serialised

    def test_a_confirmation_event_carries_its_transaction(self) -> None:
        events = FakeEvents()
        _call_with_events(_event(_confirmation()), FakeStore(), events)
        assert events.published[0][1]["transaction"] == {"payee_id": "a" * 64, "amount": 250.0}

    def test_a_fail_open_decision_is_still_published(self) -> None:
        events = FakeEvents()
        store = FakeStore(load_error=RuntimeError("dynamodb unavailable"))
        _call_with_events(_event(_payload()), store, events)
        assert events.published[0][1]["constraints"] == ["fail_open"]

    def test_server_timing_breaks_the_handler_into_stages(self) -> None:
        deps = handler.Dependencies(
            store=FakeStore(),
            model=FusionModel.load(),
            thresholds=Thresholds.load(),
            clock=lambda: NOW,
            new_id=lambda: "dec-1",
            workflow=FakeWorkflow(),
            events=FakeEvents(),
        )
        confirmation = handler.handle(_event(_confirmation()), deps)["headers"]["Server-Timing"]
        login = handler.handle(_event(_payload()), deps)["headers"]["Server-Timing"]

        def stages(header: str) -> list[str]:
            return [part.split(";")[0].strip() for part in header.split(",")]

        # handler comes first, so a reader that takes the first duration gets the total.
        assert stages(confirmation) == ["handler", "read", "score", "write", "publish", "workflow"]
        assert stages(login) == ["handler", "read", "score", "write", "publish"]

    def test_a_publish_failure_never_changes_the_response(self) -> None:
        quiet = FakeEvents()
        _, expected = _call_with_events(_event(_payload()), FakeStore(), quiet)
        status, body = _call_with_events(
            _event(_payload()), FakeStore(), FakeEvents(error=RuntimeError("bus unavailable"))
        )
        assert status == 200
        assert body == expected
