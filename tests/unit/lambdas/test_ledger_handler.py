"""Tests for the mock ledger, against a fake DynamoDB client.

The invariant under test is that money moves only on release, exactly once. DynamoDB itself
evaluates the conditional expressions, so the Phase 5 integration test proves them against the
real table; here each operation's error handling and write shape is pinned.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError

from ledger.handler import (
    AWAITING_STEP_UP,
    CANCELLED,
    PENDING,
    REJECTED,
    RELEASED,
    UNDER_REVIEW,
    Ledger,
    handle,
)

TRANSFER = {
    "transfer_id": "transfer-0001",
    "decision_id": "transfer-0001",
    "uid": "user-1",
    "amount": 250.5,
    "payee_id": "a" * 64,
    "action": "step_up",
    "response": "step_up",
}


def _error(code: str, reasons: list[str] | None = None) -> ClientError:
    response: dict[str, Any] = {"Error": {"Code": code, "Message": code}}
    if reasons is not None:
        response["CancellationReasons"] = [{"Code": reason} for reason in reasons]
    return ClientError(response, "Operation")  # type: ignore[arg-type]


class FakeClient:
    def __init__(self, status: str = PENDING, errors: dict[str, ClientError] | None = None) -> None:
        self.status = status
        self.errors = dict(errors or {})
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _record(self, name: str, kwargs: dict[str, Any]) -> None:
        self.calls.append((name, kwargs))
        if name in self.errors:
            raise self.errors.pop(name)

    def update_item(self, **kwargs: Any) -> dict[str, Any]:
        self._record("update_item", kwargs)
        return {}

    def put_item(self, **kwargs: Any) -> dict[str, Any]:
        self._record("put_item", kwargs)
        return {}

    def transact_write_items(self, **kwargs: Any) -> dict[str, Any]:
        self._record("transact_write_items", kwargs)
        return {}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        self._record("get_item", kwargs)
        return {"Item": {"status": {"S": self.status}}}

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]


def _ledger(client: FakeClient) -> Ledger:
    return Ledger(client, "table", clock=lambda: 1_767_225_600.0)


class TestRecord:
    def test_record_seeds_the_balance_and_creates_a_pending_transfer(self) -> None:
        client = FakeClient()
        assert _ledger(client).record(TRANSFER) == PENDING

        seed = client.calls[0][1]
        assert "if_not_exists(balance, :opening)" in seed["UpdateExpression"]
        put = client.calls[1][1]
        assert put["ConditionExpression"] == "attribute_not_exists(PK)"
        assert put["Item"]["amount"] == {"N": "250.50"}
        assert put["Item"]["status"] == {"S": PENDING}

    def test_a_retried_record_returns_the_existing_status(self) -> None:
        client = FakeClient(
            status=AWAITING_STEP_UP,
            errors={"put_item": _error("ConditionalCheckFailedException")},
        )
        assert _ledger(client).record(TRANSFER) == AWAITING_STEP_UP


class TestHold:
    @pytest.mark.parametrize(
        ("awaiting", "status"), [("step_up", AWAITING_STEP_UP), ("review", UNDER_REVIEW)]
    )
    def test_a_hold_stores_the_task_token(self, awaiting: str, status: str) -> None:
        client = FakeClient()
        assert _ledger(client).hold(TRANSFER, awaiting, "token-1") == status
        values = client.calls[0][1]["ExpressionAttributeValues"]
        assert values[":token"] == {"S": "token-1"}
        assert values[":held"] == {"S": status}

    def test_an_unknown_hold_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown hold"):
            _ledger(FakeClient()).hold(TRANSFER, "prayer", "token-1")

    def test_a_hold_on_a_closed_transfer_leaves_it_closed(self) -> None:
        client = FakeClient(
            status=CANCELLED, errors={"update_item": _error("ConditionalCheckFailedException")}
        )
        assert _ledger(client).hold(TRANSFER, "step_up", "token-1") == CANCELLED


class TestRelease:
    def test_release_debits_and_closes_in_one_transaction(self) -> None:
        client = FakeClient()
        assert _ledger(client).release(TRANSFER) == RELEASED

        items = client.calls[0][1]["TransactItems"]
        debit, close = items[0]["Update"], items[1]["Update"]
        assert debit["UpdateExpression"] == "SET balance = balance - :amount"
        assert debit["ConditionExpression"] == "balance >= :amount"
        assert debit["ExpressionAttributeValues"][":amount"] == {"N": "250.50"}
        assert close["ConditionExpression"].startswith("#s IN (")

    def test_a_retried_release_does_not_debit_twice(self) -> None:
        client = FakeClient(
            status=RELEASED,
            errors={
                "transact_write_items": _error(
                    "TransactionCanceledException", ["None", "ConditionalCheckFailed"]
                )
            },
        )
        assert _ledger(client).release(TRANSFER) == RELEASED
        assert client.names() == ["transact_write_items", "get_item"]

    def test_insufficient_funds_rejects_the_transfer(self) -> None:
        client = FakeClient(
            errors={
                "transact_write_items": _error(
                    "TransactionCanceledException", ["ConditionalCheckFailed", "None"]
                )
            }
        )
        assert _ledger(client).release(TRANSFER) == REJECTED
        values = client.calls[1][1]["ExpressionAttributeValues"]
        assert values[":status"] == {"S": REJECTED}
        assert values[":reason"] == {"S": "insufficient_funds"}

    def test_an_unexpected_error_propagates_for_the_workflow_to_retry(self) -> None:
        client = FakeClient(errors={"transact_write_items": _error("ThrottlingException")})
        with pytest.raises(ClientError):
            _ledger(client).release(TRANSFER)


class TestCancel:
    def test_cancel_closes_with_a_reason(self) -> None:
        client = FakeClient()
        assert _ledger(client).cancel(TRANSFER, "timeout") == CANCELLED
        assert client.calls[0][1]["ExpressionAttributeValues"][":reason"] == {"S": "timeout"}

    def test_a_released_transfer_is_never_cancelled(self) -> None:
        client = FakeClient(
            status=RELEASED, errors={"update_item": _error("ConditionalCheckFailedException")}
        )
        assert _ledger(client).cancel(TRANSFER, "timeout") == RELEASED


class TestMoneyMovesOnlyOnRelease:
    @pytest.mark.parametrize(
        "event",
        [
            {"op": "record"},
            {"op": "hold", "awaiting": "step_up", "task_token": "t"},
            {"op": "cancel", "reason": "blocked"},
        ],
    )
    def test_no_other_operation_subtracts_from_a_balance(self, event: dict[str, Any]) -> None:
        client = FakeClient()
        handle({**event, "transfer": TRANSFER}, _ledger(client))
        expressions = " ".join(
            str(kwargs.get("UpdateExpression", "")) for _, kwargs in client.calls
        )
        assert "balance -" not in expressions
        assert "transact_write_items" not in client.names()

    def test_an_unknown_operation_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="unknown ledger operation"):
            handle({"op": "debit", "transfer": TRANSFER}, _ledger(FakeClient()))

    def test_handle_returns_the_status(self) -> None:
        assert handle({"op": "release", "transfer": TRANSFER}, _ledger(FakeClient())) == {
            "status": RELEASED
        }
