"""Tests for the event detail shapes and the audit-lake key layout."""

from __future__ import annotations

from typing import Any

import pytest

from fraudcore import events
from fraudcore.fusion import Contribution
from fraudcore.policy import Decision
from fraudcore.scoring import ChannelScore

DECISION = Decision(
    action="step_up",
    risk=97.1,
    confidence=0.8,
    contributions=(Contribution("behaviour", 6.0), Contribution("payee", 1.27)),
    constraints=("friction_ceiling",),
)
SCORES = {"behaviour": ChannelScore(812.0, 1.0), "payee": ChannelScore(2.0, 0.5)}


def _decision_detail(**overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "decision_id": "dec-1",
        "session_id": "session-0001",
        "uid": "user-1",
        "checkpoint": "confirmation",
        "device_class": "desktop",
        "decision": DECISION,
        "scores": SCORES,
        "created_at": 1_767_225_600.0,
    }
    arguments.update(overrides)
    return events.decision_detail(**arguments)


class TestDecisionDetail:
    def test_the_detail_shape_is_pinned(self) -> None:
        # Pinned exactly: adding a field means arguing for it, and keystroke timing never qualifies.
        assert set(_decision_detail()) == {
            "schema",
            "decision_id",
            "session_id",
            "uid",
            "checkpoint",
            "device_class",
            "action",
            "risk",
            "confidence",
            "contributions",
            "constraints",
            "scores",
            "created_at",
        }

    def test_values_are_carried_through(self) -> None:
        detail = _decision_detail()
        assert detail["schema"] == events.SCHEMA_VERSION
        assert detail["contributions"][0] == {"channel": "behaviour", "value": 6.0}
        assert detail["scores"]["payee"] == {"score": 2.0, "confidence": 0.5}
        assert detail["created_at"] == "2026-01-01T00:00:00.000+00:00"

    def test_a_transaction_is_included_only_when_complete(self) -> None:
        assert "transaction" not in _decision_detail(payee_id="a" * 64)
        full = _decision_detail(payee_id="a" * 64, amount=250.0)
        assert full["transaction"] == {"payee_id": "a" * 64, "amount": 250.0}


class TestStepUpVerified:
    DETAIL = {
        "schema": 1,
        "uid": "21d35d6a-a031-708a-d069-7ca41d1ac623",
        "transfer_id": "636de234abcd",
        "decision_id": "636de234abcd",
        "verification": {"verified": True, "method": "passkey", "verified_at": 1789463206},
    }

    def test_a_valid_detail_parses(self) -> None:
        parsed = events.parse_stepup_verified(self.DETAIL)
        assert parsed.method == "passkey"
        assert parsed.verified_at == 1789463206
        assert parsed.uid == self.DETAIL["uid"]

    def test_a_missing_timestamp_is_tolerated(self) -> None:
        detail = {**self.DETAIL, "verification": {"method": "passkey"}}
        assert events.parse_stepup_verified(detail).verified_at is None

    def test_a_boolean_is_not_a_timestamp(self) -> None:
        detail = {**self.DETAIL, "verification": {"method": "passkey", "verified_at": True}}
        assert events.parse_stepup_verified(detail).verified_at is None

    @pytest.mark.parametrize(
        ("change", "message"),
        [
            ({"schema": 2}, "schema"),
            ({"uid": None}, "uid"),
            ({"decision_id": "../BALANCE"}, "decision_id"),
            ({"verification": {"verified": True}}, "method"),
        ],
    )
    def test_malformed_details_are_rejected(self, change: dict[str, Any], message: str) -> None:
        with pytest.raises(ValueError, match=message):
            events.parse_stepup_verified({**self.DETAIL, **change})


class TestLakeKey:
    EVENT_ID = "4e5b2f6a-1c2d-4e3f-8a9b-0c1d2e3f4a5b"

    def _event(self, **overrides: Any) -> dict[str, Any]:
        event = {
            "id": self.EVENT_ID,
            "detail-type": events.DECISION_SCORED,
            "time": "2026-09-15T08:34:25Z",
            "detail": {"decision_id": "dec0001", "transfer_id": "ignored"},
        }
        event.update(overrides)
        return event

    def test_a_decision_is_keyed_by_type_date_and_decision(self) -> None:
        assert events.lake_key(self._event()) == (
            f"events/type=decision.scored/dt=2026-09-15/dec0001.{self.EVENT_ID}.json"
        )

    def test_workflow_events_are_keyed_by_transfer(self) -> None:
        event = self._event(**{"detail-type": events.TRANSFER_COMPLETED})
        event["detail"] = {"transfer_id": "txn0001"}
        assert events.lake_key(event).endswith(f"/txn0001.{self.EVENT_ID}.json")

    def test_the_date_partition_is_utc(self) -> None:
        # 23:30 at UTC-2 is already the next day in UTC.
        key = events.lake_key(self._event(time="2026-09-15T23:30:00-02:00"))
        assert "/dt=2026-09-16/" in key

    def test_an_unknown_type_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="detail type"):
            events.lake_key(self._event(**{"detail-type": "profile.poisoned"}))

    def test_a_subject_that_could_escape_its_prefix_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="subject"):
            events.lake_key(self._event(detail={"decision_id": "../../other"}))
