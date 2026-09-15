"""Mock ledger: money moves only when the response workflow releases a transfer.

This is the invariant the response plane exists to protect. Nothing debits a balance except
``release``, and the workflow reaches ``release`` only from the allow path, a verified step-up or an
analyst's review. Every operation is idempotent, because Step Functions retries failed Lambda tasks
and a retry must never debit twice or reopen a closed transfer.

Items, all in the user's ledger partition:

    LEDGER#<uid> / BALANCE      balance, opening
    LEDGER#<uid> / TXN#<id>     status, amount, payee_id, decision_id, action, reason, task_token
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

from botocore.exceptions import ClientError

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

# Every demo account starts with this much, seeded on its first transfer.
OPENING_BALANCE = Decimal("100000")

PENDING = "pending"
AWAITING_STEP_UP = "awaiting_step_up"
UNDER_REVIEW = "under_review"
RELEASED = "released"
CANCELLED = "cancelled"
REJECTED = "rejected"

OPEN_STATUSES = (PENDING, AWAITING_STEP_UP, UNDER_REVIEW)
HOLD_STATUSES = {"step_up": AWAITING_STEP_UP, "review": UNDER_REVIEW}


def _code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def _money(value: Any) -> str:
    return str(Decimal(str(value)).quantize(Decimal("0.01")))


class Ledger:
    def __init__(self, client: Any, table: str, clock: Callable[[], float] = time.time) -> None:
        self._client = client
        self._table = table
        self._clock = clock

    def _key(self, uid: str, sort: str) -> dict[str, dict[str, str]]:
        return {"PK": {"S": f"LEDGER#{uid}"}, "SK": {"S": sort}}

    def _open_values(self) -> dict[str, dict[str, str]]:
        return {f":open{i}": {"S": status} for i, status in enumerate(OPEN_STATUSES)}

    def _open_condition(self) -> str:
        return "#s IN (" + ", ".join(f":open{i}" for i in range(len(OPEN_STATUSES))) + ")"

    def status(self, uid: str, transfer_id: str) -> str:
        item = self._client.get_item(
            TableName=self._table, Key=self._key(uid, f"TXN#{transfer_id}"), ConsistentRead=True
        ).get("Item")
        return item["status"]["S"] if item else "missing"

    def record(self, transfer: Mapping[str, Any]) -> str:
        uid, transfer_id = transfer["uid"], transfer["transfer_id"]
        # Seeds the balance on first use and leaves it alone afterwards.
        self._client.update_item(
            TableName=self._table,
            Key=self._key(uid, "BALANCE"),
            UpdateExpression=(
                "SET balance = if_not_exists(balance, :opening), "
                "opening = if_not_exists(opening, :opening)"
            ),
            ExpressionAttributeValues={":opening": {"N": str(OPENING_BALANCE)}},
        )
        try:
            self._client.put_item(
                TableName=self._table,
                Item={
                    **self._key(uid, f"TXN#{transfer_id}"),
                    "status": {"S": PENDING},
                    "amount": {"N": _money(transfer["amount"])},
                    "payee_id": {"S": transfer["payee_id"]},
                    "decision_id": {"S": transfer["decision_id"]},
                    "action": {"S": transfer["action"]},
                    "created_at": {"N": str(int(self._clock()))},
                },
                ConditionExpression="attribute_not_exists(PK)",
            )
        except ClientError as error:
            if _code(error) != "ConditionalCheckFailedException":
                raise
            return self.status(uid, transfer_id)
        return PENDING

    def hold(self, transfer: Mapping[str, Any], awaiting: str, task_token: str) -> str:
        if awaiting not in HOLD_STATUSES:
            raise ValueError(f"unknown hold {awaiting!r}")
        uid, transfer_id = transfer["uid"], transfer["transfer_id"]
        held = HOLD_STATUSES[awaiting]
        try:
            self._client.update_item(
                TableName=self._table,
                Key=self._key(uid, f"TXN#{transfer_id}"),
                UpdateExpression="SET #s = :held, task_token = :token, held_at = :now",
                # A retried hold arrives with a fresh token and must replace the stale one.
                ConditionExpression="#s IN (:pending, :held)",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":held": {"S": held},
                    ":pending": {"S": PENDING},
                    ":token": {"S": task_token},
                    ":now": {"N": str(int(self._clock()))},
                },
            )
        except ClientError as error:
            if _code(error) != "ConditionalCheckFailedException":
                raise
            return self.status(uid, transfer_id)
        return held

    def release(self, transfer: Mapping[str, Any]) -> str:
        uid, transfer_id = transfer["uid"], transfer["transfer_id"]
        try:
            self._client.transact_write_items(
                TransactItems=[
                    {
                        "Update": {
                            "TableName": self._table,
                            "Key": self._key(uid, "BALANCE"),
                            "UpdateExpression": "SET balance = balance - :amount",
                            "ConditionExpression": "balance >= :amount",
                            "ExpressionAttributeValues": {
                                ":amount": {"N": _money(transfer["amount"])}
                            },
                        }
                    },
                    {
                        "Update": {
                            "TableName": self._table,
                            "Key": self._key(uid, f"TXN#{transfer_id}"),
                            "UpdateExpression": (
                                "SET #s = :released, closed_at = :now REMOVE task_token"
                            ),
                            "ConditionExpression": self._open_condition(),
                            "ExpressionAttributeNames": {"#s": "status"},
                            "ExpressionAttributeValues": {
                                ":released": {"S": RELEASED},
                                ":now": {"N": str(int(self._clock()))},
                                **self._open_values(),
                            },
                        }
                    },
                ]
            )
        except ClientError as error:
            if _code(error) != "TransactionCanceledException":
                raise
            reasons = [
                reason.get("Code", "None")
                for reason in error.response.get("CancellationReasons", [])
            ]
            # The transfer is already closed: this is a retry of a release that succeeded, or a
            # release racing a cancel. Either way the balance must not move.
            if len(reasons) > 1 and reasons[1] == "ConditionalCheckFailed":
                return self.status(uid, transfer_id)
            if reasons and reasons[0] == "ConditionalCheckFailed":
                return self._close(uid, transfer_id, REJECTED, "insufficient_funds")
            raise
        return RELEASED

    def cancel(self, transfer: Mapping[str, Any], reason: str) -> str:
        return self._close(transfer["uid"], transfer["transfer_id"], CANCELLED, reason)

    def _close(self, uid: str, transfer_id: str, status: str, reason: str) -> str:
        try:
            self._client.update_item(
                TableName=self._table,
                Key=self._key(uid, f"TXN#{transfer_id}"),
                UpdateExpression="SET #s = :status, reason = :reason, closed_at = :now "
                "REMOVE task_token",
                ConditionExpression=self._open_condition(),
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={
                    ":status": {"S": status},
                    ":reason": {"S": reason},
                    ":now": {"N": str(int(self._clock()))},
                    **self._open_values(),
                },
            )
        except ClientError as error:
            if _code(error) != "ConditionalCheckFailedException":
                raise
            # Never reopen or overwrite a closed transfer, in particular a released one.
            return self.status(uid, transfer_id)
        return status


def handle(event: Mapping[str, Any], ledger: Ledger) -> dict[str, str]:
    operation = event.get("op")
    transfer = event["transfer"]
    if operation == "record":
        status = ledger.record(transfer)
    elif operation == "hold":
        status = ledger.hold(transfer, event["awaiting"], event["task_token"])
    elif operation == "release":
        status = ledger.release(transfer)
    elif operation == "cancel":
        status = ledger.cancel(transfer, event["reason"])
    else:
        raise ValueError(f"unknown ledger operation {operation!r}")
    LOGGER.info("ledger %s %s -> %s", operation, transfer["transfer_id"], status)
    return {"status": status}


_ledger: Ledger | None = None


def lambda_handler(event: Mapping[str, Any], context: Any) -> dict[str, str]:
    global _ledger
    if _ledger is None:
        import boto3

        _ledger = Ledger(boto3.client("dynamodb"), os.environ["TABLE_NAME"])
    return handle(event, _ledger)
