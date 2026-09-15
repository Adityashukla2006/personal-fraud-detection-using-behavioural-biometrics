"""Tests for the scoring path's DynamoDB translation, against an in-memory fake client."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from fraudcore.features import KeystrokeTiming
from fraudcore.policy import fail_open
from fraudcore.scoring import ChannelScore
from scoring.request import CheckpointRequest, Device, TransactionRequest
from scoring.store import (
    DECISION_TTL_SECONDS,
    SECONDS_PER_DAY,
    SESSION_TTL_SECONDS,
    ForeignSessionError,
    Store,
    StoreError,
)

TABLE = "table"
NOW = 1_767_225_600.0
UID = "user-1"
PAYEE = "b" * 64
TIMING = KeystrokeTiming(hold=(0.1, 0.2, 0.3), down_down=(0.5, 0.6), up_down=(0.4, 0.4))

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


def _request(transaction: bool = False) -> CheckpointRequest:
    return CheckpointRequest(
        session_id="session-0001",
        checkpoint="confirmation" if transaction else "login",
        device=Device("device-0001", "desktop"),
        fields=(TIMING,),
        transaction=TransactionRequest(PAYEE, 100.0) if transaction else None,
    )


class FakeClient:
    def __init__(
        self, items: list[dict[str, Any]] = (), unprocessed: int = 0, error: bool = False
    ) -> None:
        self.items = {(item["PK"], item["SK"]): item for item in items}
        self.unprocessed = unprocessed
        self.error = error
        self.requested: list[tuple[str, str]] = []
        self.puts: list[dict[str, Any]] = []

    def batch_get_item(self, RequestItems: dict[str, Any]) -> dict[str, Any]:  # noqa: N803
        if self.error:
            raise RuntimeError("dynamodb unavailable")
        if self.unprocessed:
            self.unprocessed -= 1
            return {"Responses": {TABLE: []}, "UnprocessedKeys": RequestItems}
        keys = [(k["PK"]["S"], k["SK"]["S"]) for k in RequestItems[TABLE]["Keys"]]
        self.requested = keys
        found = [
            {name: _serializer.serialize(value) for name, value in self.items[key].items()}
            for key in keys
            if key in self.items
        ]
        return {"Responses": {TABLE: found}, "UnprocessedKeys": {}}

    def put_item(self, **kwargs: Any) -> None:
        if self.error:
            raise RuntimeError("dynamodb unavailable")
        self.puts.append(kwargs)


def _stored(put: dict[str, Any]) -> dict[str, Any]:
    return {name: _deserializer.deserialize(value) for name, value in put["Item"].items()}


class TestLoad:
    def test_an_empty_table_is_a_brand_new_user(self) -> None:
        state = Store(FakeClient(), TABLE).load(UID, _request(), NOW)
        assert state.profile is None
        assert state.context.device is None
        assert state.context.account_sessions == 0
        assert state.aggregates is None
        assert state.session_fields == ()

    def test_payee_items_are_only_requested_with_a_transaction(self) -> None:
        client = FakeClient()
        Store(client, TABLE).load(UID, _request(), NOW)
        assert len(client.requested) == 5
        Store(client, TABLE).load(UID, _request(transaction=True), NOW)
        assert (f"PAYEE#{PAYEE}", "RISK") in client.requested

    def test_every_item_decodes_into_fraudcore_types(self) -> None:
        items = [
            {
                "PK": f"USER#{UID}",
                "SK": "PROFILE#desktop",
                "features": ["mean_hold", "mean_flight"],
                "mu": [Decimal("0.1"), Decimal("0.2")],
                "sigma": [Decimal("0.01"), Decimal("0")],
                "n_sessions": 7,
            },
            {"PK": f"USER#{UID}", "SK": "DEV#device-0001", "session_count": 12},
            {
                "PK": f"USER#{UID}",
                "SK": "ACCOUNT",
                "session_count": 40,
                "credential_changed_at": Decimal(str(NOW - 1.5 * SECONDS_PER_DAY)),
                # A timestamp in the future is clock skew and clamps to zero days.
                "contact_changed_at": Decimal(str(NOW + 60)),
            },
            {
                "PK": f"USER#{UID}",
                "SK": f"PAYEE#{PAYEE}",
                "first_seen": Decimal(str(NOW - 6 * SECONDS_PER_DAY)),
                "verified_at": Decimal(str(NOW - SECONDS_PER_DAY)),
            },
            {
                "PK": f"PAYEE#{PAYEE}",
                "SK": "RISK",
                "risk_score": Decimal("0.4"),
                "computed_at": Decimal(str(NOW - 42 * 3600)),
                "flagged": True,
            },
            {
                "PK": f"AGG#{UID}",
                "SK": "WINDOW#30d",
                "amount_p50": Decimal("400"),
                "amount_p95": Decimal("1400"),
                "daily_count_p95": Decimal("3"),
                "hour_histogram": [Decimal("1")] * 24,
                "history_count": 60,
            },
        ]
        state = Store(FakeClient(items), TABLE).load(UID, _request(transaction=True), NOW)

        assert state.profile is not None
        assert state.profile.centre == (0.1, 0.2)
        assert state.profile.dispersion[1] > 0
        assert state.profile_sessions == 7
        assert state.context.device is not None and state.context.device.session_count == 12
        assert state.context.days_since_credential_change == pytest.approx(1.5)
        assert state.context.days_since_contact_change == 0.0
        assert state.context.account_sessions == 40
        assert state.edge is not None and state.edge.verified
        assert state.edge.days_since_first_seen == pytest.approx(6.0)
        assert state.risk is not None and state.risk.flagged
        assert state.risk.hours_since_computed == pytest.approx(42.0)
        assert state.aggregates is not None and state.aggregates.history_count == 60

    def test_a_session_owned_by_another_user_is_refused(self) -> None:
        session = {"PK": "SESS#session-0001", "SK": "META", "uid": "someone-else", "fields": []}
        with pytest.raises(ForeignSessionError):
            Store(FakeClient([session]), TABLE).load(UID, _request(), NOW)

    def test_one_unprocessed_response_is_retried(self) -> None:
        Store(FakeClient(unprocessed=1), TABLE).load(UID, _request(), NOW)

    def test_keys_still_unprocessed_after_the_retry_fail(self) -> None:
        with pytest.raises(StoreError, match="unprocessed"):
            Store(FakeClient(unprocessed=2), TABLE).load(UID, _request(), NOW)

    def test_a_client_error_becomes_a_store_error(self) -> None:
        with pytest.raises(StoreError, match="batch_get_item"):
            Store(FakeClient(error=True), TABLE).load(UID, _request(), NOW)

    def test_a_malformed_profile_becomes_a_store_error(self) -> None:
        profile = {
            "PK": f"USER#{UID}",
            "SK": "PROFILE#desktop",
            "features": ["a", "b"],
            "mu": [Decimal("1")],
            "sigma": [Decimal("1"), Decimal("1")],
            "n_sessions": 1,
        }
        with pytest.raises(StoreError, match="malformed"):
            Store(FakeClient([profile]), TABLE).load(UID, _request(), NOW)


class TestSave:
    def test_a_saved_session_round_trips_and_expires_in_a_day(self) -> None:
        client = FakeClient()
        store = Store(client, TABLE)
        store.save_session(UID, "session-0001", (TIMING, TIMING), NOW)

        put = client.puts[0]
        assert "attribute_not_exists(PK)" in put["ConditionExpression"]
        item = _stored(put)
        assert item["ttl"] == int(NOW) + SESSION_TTL_SECONDS

        client.items[(item["PK"], item["SK"])] = item
        assert store.load(UID, _request(), NOW).session_fields == (TIMING, TIMING)

    def test_a_decision_is_indexed_by_user_and_time(self) -> None:
        client = FakeClient()
        scores = {"behaviour": ChannelScore(1.5, 0.25)}
        Store(client, TABLE).save_decision(UID, "dec-1", _request(), scores, fail_open(), NOW)

        item = _stored(client.puts[0])
        assert item["PK"] == "DEC#dec-1"
        assert item["GSI1PK"] == f"USER#{UID}"
        assert item["GSI1SK"].startswith("TS#2026-01-01T00:00:00")
        assert item["ttl"] == int(NOW) + DECISION_TTL_SECONDS
        assert item["risk"] is None
        assert item["action"] == "monitor"
        assert item["device_id"] == "device-0001"
        assert item["scores"]["behaviour"] == {
            "score": Decimal("1.5"),
            "confidence": Decimal("0.25"),
        }

    def test_a_write_error_becomes_a_store_error(self) -> None:
        with pytest.raises(StoreError, match="put_item"):
            Store(FakeClient(error=True), TABLE).save_session(UID, "session-0001", (), NOW)
