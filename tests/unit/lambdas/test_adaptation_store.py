"""Tests for adaptation's DynamoDB translation, against a fake client."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from adaptation.store import AdaptationStore, DecisionRecord, decode_profile
from fraudcore.adaptation import PROFILE_FEATURES, Profile
from fraudcore.events import StepUpVerified
from fraudcore.features import KeystrokeTiming, vector
from fraudcore.scoring import ChannelScore
from fraudcore.session import pooled_extract

TABLE = "table"
UID = "user-1"
NOW = 1_789_463_210.0
WIDTH = len(PROFILE_FEATURES)

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()

FIELD = {
    "hold": [Decimal("0.1"), Decimal("0.2"), Decimal("0.3")],
    "down_down": [Decimal("0.5"), Decimal("0.6")],
    "up_down": [Decimal("0.4"), Decimal("0.4")],
    "backspaces": 0,
    "corrections": 0,
    "pastes": 0,
}
VERIFIED = StepUpVerified(UID, "transfer-1", "decision-1", "passkey", 1_789_463_206)
DECISION = DecisionRecord(UID, "session-1", "desktop", "device-1", "step_up", {})


def _error(code: str, reasons: list[str] | None = None) -> ClientError:
    response: dict[str, Any] = {"Error": {"Code": code, "Message": code}}
    if reasons is not None:
        response["CancellationReasons"] = [{"Code": reason} for reason in reasons]
    return ClientError(response, "Operation")  # type: ignore[arg-type]


class FakeClient:
    def __init__(self, items: list[dict[str, Any]] = (), error: ClientError | None = None) -> None:
        self.items = {(item["PK"], item["SK"]): item for item in items}
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _serialized(self, item: dict[str, Any]) -> dict[str, Any]:
        return {name: _serializer.serialize(value) for name, value in item.items()}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        key = (kwargs["Key"]["PK"]["S"], kwargs["Key"]["SK"]["S"])
        item = self.items.get(key)
        return {} if item is None else {"Item": self._serialized(item)}

    def batch_get_item(self, **kwargs: Any) -> dict[str, Any]:
        keys = [(k["PK"]["S"], k["SK"]["S"]) for k in kwargs["RequestItems"][TABLE]["Keys"]]
        found = [self._serialized(self.items[key]) for key in keys if key in self.items]
        return {"Responses": {TABLE: found}, "UnprocessedKeys": {}}

    def transact_write_items(self, **kwargs: Any) -> None:
        self.calls.append(("transact_write_items", kwargs))
        if self.error:
            raise self.error

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("query", kwargs))
        newest_first = sorted(
            (item for item in self.items.values() if item["SK"].startswith("BUF#")),
            key=lambda item: item["SK"],
            reverse=True,
        )
        return {"Items": [self._serialized(item) for item in newest_first[: kwargs["Limit"]]]}

    def put_item(self, **kwargs: Any) -> None:
        self.calls.append(("put_item", kwargs))


def _store(client: FakeClient) -> AdaptationStore:
    return AdaptationStore(client, TABLE)


class TestReads:
    def test_a_decision_decodes_with_its_scores(self) -> None:
        item = {
            "PK": "DEC#decision-1",
            "SK": "META",
            "uid": UID,
            "session_id": "session-1",
            "device_class": "desktop",
            "device_id": "device-1",
            "action": "step_up",
            "scores": {"payee": {"score": Decimal("2"), "confidence": Decimal("0.5")}},
        }
        decision = _store(FakeClient([item])).decision("decision-1")
        assert decision is not None
        assert decision.device_id == "device-1"
        assert decision.scores == {"payee": ChannelScore(2.0, 0.5)}

    def test_a_decision_from_before_device_ids_were_recorded_is_unusable(self) -> None:
        item = {"PK": "DEC#decision-1", "SK": "META", "uid": UID}
        assert _store(FakeClient([item])).decision("decision-1") is None

    def test_session_features_are_the_pooled_profile_vector(self) -> None:
        session = {"PK": "SESS#session-1", "SK": "META", "uid": UID, "fields": [FIELD, FIELD]}
        timing = KeystrokeTiming((0.1, 0.2, 0.3), (0.5, 0.6), (0.4, 0.4))
        expected = vector(pooled_extract([timing, timing]), PROFILE_FEATURES)
        features = _store(FakeClient([session])).session_features("session-1", UID)
        assert features == pytest.approx(tuple(expected))

    def test_another_users_session_yields_no_features(self) -> None:
        session = {"PK": "SESS#session-1", "SK": "META", "uid": "someone-else", "fields": [FIELD]}
        assert _store(FakeClient([session])).session_features("session-1", UID) is None

    def test_an_expired_session_yields_no_features(self) -> None:
        assert _store(FakeClient()).session_features("session-1", UID) is None

    def test_context_decodes_device_account_and_a_legacy_profile(self) -> None:
        items = [
            {
                "PK": f"USER#{UID}",
                "SK": "PROFILE#desktop",
                "features": list(PROFILE_FEATURES),
                "mu": [Decimal("5")] * WIDTH,
                "sigma": [Decimal("0.01")] * WIDTH,
                "n_sessions": 20,
            },
            {"PK": f"USER#{UID}", "SK": "DEV#device-1", "session_count": 12},
            {
                "PK": f"USER#{UID}",
                "SK": "ACCOUNT",
                "credential_changed_at": Decimal(str(NOW - 2 * 86_400)),
            },
        ]
        context = _store(FakeClient(items)).context(UID, "desktop", "device-1", NOW)
        assert context.device_sessions == 12
        assert context.days_since_credential_change == pytest.approx(2.0)
        assert context.days_since_contact_change is None
        # No anchor or version stored: the profile anchors on itself at version 0.
        assert context.profile_version == 0
        assert context.profile is not None
        assert context.profile.anchor_centre == context.profile.centre

    def test_a_profile_over_another_feature_set_is_rejected(self) -> None:
        item = {"features": ["mean_hold"], "mu": [1], "sigma": [1]}
        with pytest.raises(ValueError, match="feature set"):
            decode_profile(item)


class TestClaim:
    def test_an_admitted_claim_records_counts_and_buffers_in_one_transaction(self) -> None:
        client = FakeClient()
        claim = _store(client).claim(VERIFIED, DECISION, 1.0, (0.1,) * WIDTH, NOW)
        assert (claim.trust, claim.admitted) == (1.0, True)

        items = client.calls[0][1]["TransactItems"]
        assert len(items) == 3
        record = items[0]["Put"]
        assert record["ConditionExpression"] == "attribute_not_exists(PK)"
        assert record["Item"]["SK"] == {"S": "VERIFIED#decision-1"}
        assert items[1]["Update"]["UpdateExpression"].startswith("ADD session_count :one")
        buffered = items[2]["Put"]["Item"]
        assert buffered["SK"]["S"].startswith("BUF#desktop#")
        assert buffered["SK"]["S"].endswith("#decision-1")

    def test_an_unadmitted_claim_still_counts_the_device_but_buffers_nothing(self) -> None:
        client = FakeClient()
        claim = _store(client).claim(VERIFIED, DECISION, 0.0, None, NOW)
        assert claim is not None and not claim.admitted
        assert len(client.calls[0][1]["TransactItems"]) == 2

    def test_an_already_claimed_verification_returns_none(self) -> None:
        cancelled = _error("TransactionCanceledException", ["ConditionalCheckFailed", "None"])
        assert _store(FakeClient(error=cancelled)).claim(VERIFIED, DECISION, 1.0, None, NOW) is None

    def test_any_other_failure_propagates_for_retry(self) -> None:
        with pytest.raises(ClientError):
            _store(FakeClient(error=_error("ThrottlingException"))).claim(
                VERIFIED, DECISION, 1.0, None, NOW
            )


class TestBufferAndProfile:
    def test_the_buffer_is_the_newest_sessions_oldest_first(self) -> None:
        items = [
            {
                "PK": f"USER#{UID}",
                "SK": f"BUF#desktop#{ms:013d}#d{ms}",
                "features": [Decimal(ms)] * WIDTH,
                "trust": Decimal("0.6"),
            }
            for ms in (1, 2, 3)
        ]
        client = FakeClient(items)
        buffer = _store(client).buffer(UID, "desktop", capacity=2)
        assert [session.features[0] for session in buffer] == [2.0, 3.0]
        assert client.calls[0][1]["ScanIndexForward"] is False

    @pytest.mark.parametrize(
        ("expected", "condition"),
        [
            (None, "attribute_not_exists(PK)"),
            (0, "attribute_not_exists(version) OR version = :v"),
            (3, "version = :v"),
        ],
    )
    def test_profiles_are_written_under_optimistic_concurrency(
        self, expected: int | None, condition: str
    ) -> None:
        client = FakeClient()
        vector_ = tuple([0.1] * WIDTH)
        profile = Profile(vector_, vector_, vector_, vector_, NOW, None, 0, 4)
        _store(client).save_profile(UID, "desktop", profile, 7, expected, NOW)
        put = client.calls[0][1]
        assert put["ConditionExpression"] == condition
        item = {name: _deserializer.deserialize(value) for name, value in put["Item"].items()}
        assert (item["version"], item["n_sessions"]) == (4, 7)
        assert item["last_saturated_at"] is None
