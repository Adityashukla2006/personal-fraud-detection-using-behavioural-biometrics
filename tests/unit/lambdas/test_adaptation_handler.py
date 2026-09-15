"""Tests for the adaptation Lambda's handling of verified step-ups.

The rule under test: only passkey verification is recorded, and each is recorded once.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError

from adaptation.handler import VERIFIED_TTL_SECONDS, VerificationStore, handle

NOW = 1_789_463_210
EVENT = {
    "source": "bfd.workflow",
    "detail-type": "stepup.verified",
    "detail": {
        "schema": 1,
        "uid": "21d35d6a-a031-708a-d069-7ca41d1ac623",
        "transfer_id": "636de234abcd",
        "decision_id": "636de234abcd",
        "verification": {"verified": True, "method": "passkey", "verified_at": 1_789_463_206},
    },
}


def _error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "PutItem")  # type: ignore[arg-type]


class FakeClient:
    def __init__(self, error: ClientError | None = None) -> None:
        self.error = error
        self.puts: list[dict[str, Any]] = []

    def put_item(self, **kwargs: Any) -> None:
        self.puts.append(kwargs)
        if self.error:
            raise self.error


def _store(client: FakeClient) -> VerificationStore:
    return VerificationStore(client, "table", clock=lambda: NOW)


def _with_method(method: str) -> dict[str, Any]:
    detail = {**EVENT["detail"], "verification": {"verified": True, "method": method}}
    return {**EVENT, "detail": detail}


def test_a_passkey_step_up_is_recorded_against_the_user() -> None:
    client = FakeClient()
    assert handle(EVENT, _store(client)) == {"recorded": True, "reason": "recorded"}

    put = client.puts[0]
    item = put["Item"]
    assert item["PK"] == {"S": "USER#21d35d6a-a031-708a-d069-7ca41d1ac623"}
    assert item["SK"] == {"S": "VERIFIED#636de234abcd"}
    assert item["method"] == {"S": "passkey"}
    assert item["verified_at"] == {"N": "1789463206"}
    assert item["ttl"] == {"N": str(NOW + VERIFIED_TTL_SECONDS)}
    assert put["ConditionExpression"] == "attribute_not_exists(PK)"


@pytest.mark.parametrize("method", ["password", "sms", "email_otp"])
def test_weaker_verification_is_never_recorded(method: str) -> None:
    client = FakeClient()
    assert handle(_with_method(method), _store(client))["reason"] == "not_passkey"
    assert client.puts == []


@pytest.mark.parametrize(
    "change",
    [{"source": "bfd.scoring"}, {"detail-type": "transfer.completed"}],
)
def test_events_other_than_workflow_step_ups_are_ignored(change: dict[str, str]) -> None:
    client = FakeClient()
    assert handle({**EVENT, **change}, _store(client))["reason"] == "unexpected_event"
    assert client.puts == []


def test_a_redelivered_step_up_is_recorded_once() -> None:
    client = FakeClient(error=_error("ConditionalCheckFailedException"))
    assert handle(EVENT, _store(client)) == {"recorded": False, "reason": "duplicate"}


def test_an_unexpected_write_error_propagates_for_retry() -> None:
    with pytest.raises(ClientError):
        handle(EVENT, _store(FakeClient(error=_error("ThrottlingException"))))


def test_a_malformed_detail_is_rejected_before_any_write() -> None:
    client = FakeClient()
    broken = {**EVENT, "detail": {**EVENT["detail"], "uid": None}}
    with pytest.raises(ValueError, match="uid"):
        handle(broken, _store(client))
    assert client.puts == []
