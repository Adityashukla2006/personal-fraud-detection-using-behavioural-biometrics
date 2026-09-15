"""Phase 5 exit: held transfers are released only by verification, and timeouts cancel them.

Transfers enter through the real scoring endpoint, so the action that decides each workflow path is
the deployed model's own. State for a takeover is seeded directly: a behavioural profile far from
how the test types, which the friction ceiling turns into a step-up, plus a batch-flagged payee,
which corroborates a block.

The passkey itself needs a person and an authenticator; the browser session covers that. Here the
verified step-up is completed with the transfer's task token, which is exactly the call the
transfers function makes once Cognito accepts a passkey, and a forged verification is refused.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import boto3
import pytest
from conftest import TEST_PREFIX
from phase_04_scoring import _field, _payload, post, user  # noqa: F401

from fraudcore.features import BENCHMARK_FEATURE_NAMES
from ledger.handler import OPENING_BALANCE

pytestmark = pytest.mark.integration

WORKFLOW_WAIT_SECONDS = 90


def wait_for(fetch: Callable[[], Any], ready: Callable[[Any], bool], timeout: float) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = fetch()
        if ready(value):
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout}s; last value {value!r}")
        time.sleep(1)


class Stack:
    def __init__(self, aws: boto3.Session, outputs: dict[str, Any], sub: str) -> None:
        self.outputs = outputs
        self.sub = sub
        self.table = aws.resource("dynamodb").Table(outputs["table_name"])
        self.sfn = aws.client("stepfunctions")
        self.created: list[tuple[str, str]] = []

    def put(self, item: dict[str, Any]) -> None:
        self.table.put_item(Item=item)
        self.created.append((item["PK"], item["SK"]))

    def item(self, partition: str, sort: str) -> dict[str, Any]:
        return self.table.get_item(Key={"PK": partition, "SK": sort}, ConsistentRead=True).get(
            "Item", {}
        )

    def transfer(self, transfer_id: str) -> dict[str, Any]:
        return self.item(f"LEDGER#{self.sub}", f"TXN#{transfer_id}")

    def balance(self) -> Decimal:
        return self.item(f"LEDGER#{self.sub}", "BALANCE").get("balance", OPENING_BALANCE)

    def execution(self, transfer_id: str) -> dict[str, Any]:
        arn = self.outputs["state_machine_arn"].replace(":stateMachine:", ":execution:")
        return self.sfn.describe_execution(executionArn=f"{arn}:{transfer_id}")

    def confirm(self, token: str, amount: float, payee: str, fields: int = 1) -> dict[str, Any]:
        digest = hashlib.sha256(f"{payee}{time.time_ns()}".encode()).hexdigest()
        session = f"{TEST_PREFIX}{digest[:24]}"
        payload = _payload(
            session,
            "confirmation",
            transaction={"payee_id": self.payee_id(payee), "amount": amount},
        )
        payload["behaviour"]["fields"] = [_field() for _ in range(fields)]
        status, body = post(f"{self.outputs['api_url']}/score", payload, token)
        assert status == 200, body
        self.created += [(f"SESS#{session}", "META"), (f"DEC#{body['decision_id']}", "META")]
        self.created.append((f"LEDGER#{self.sub}", f"TXN#{body['decision_id']}"))
        return body

    @staticmethod
    def payee_id(payee: str) -> str:
        return hashlib.sha256(f"{TEST_PREFIX}{payee}".encode()).hexdigest()

    def seed_takeover_profile(self) -> None:
        # Centred far from the fixture typing with tight dispersion, and old enough to be trusted.
        self.put(
            {
                "PK": f"USER#{self.sub}",
                "SK": "PROFILE#desktop",
                "features": list(BENCHMARK_FEATURE_NAMES),
                "mu": [Decimal("5")] * len(BENCHMARK_FEATURE_NAMES),
                "sigma": [Decimal("0.01")] * len(BENCHMARK_FEATURE_NAMES),
                "n_sessions": 20,
            }
        )

    def cleanup(self) -> None:
        for partition, sort in {*self.created, (f"LEDGER#{self.sub}", "BALANCE")}:
            self.table.delete_item(Key={"PK": partition, "SK": sort})


@pytest.fixture
def stack(
    aws: boto3.Session, outputs: dict[str, Any], user: dict[str, str]  # noqa: F811
) -> Iterator[Stack]:
    stack = Stack(aws, outputs, user["sub"])
    yield stack
    stack.cleanup()


def _settled(stack: Stack, transfer_id: str) -> dict[str, Any]:
    wait_for(
        lambda: stack.execution(transfer_id)["status"],
        lambda status: status != "RUNNING",
        WORKFLOW_WAIT_SECONDS,
    )
    return stack.transfer(transfer_id)


def test_an_allowed_transfer_is_released_and_debited(
    stack: Stack, user: dict[str, str]  # noqa: F811
) -> None:
    before = stack.balance()
    body = stack.confirm(user["token"], 120.0, "allowed")
    assert body["action"] in {"allow", "monitor"}
    assert body["transfer"]["status"] == "processing"

    transfer = _settled(stack, body["decision_id"])
    assert stack.execution(body["decision_id"])["status"] == "SUCCEEDED"
    assert transfer["status"] == "released"
    assert stack.balance() == before - Decimal("120.00")


def test_a_step_up_holds_the_transfer_until_verified_then_releases_it(
    stack: Stack, outputs: dict[str, Any], user: dict[str, str]  # noqa: F811
) -> None:
    stack.seed_takeover_profile()
    before = stack.balance()

    # Four fields reach the keystroke count at which identity evidence carries full confidence.
    body = stack.confirm(user["token"], 250.0, "step-up", fields=4)
    transfer_id = body["decision_id"]
    assert body["action"] == "step_up", body
    assert body["transfer"]["status"] == "awaiting_step_up"

    held = wait_for(
        lambda: stack.transfer(transfer_id),
        lambda item: item.get("status") == "awaiting_step_up" and "task_token" in item,
        WORKFLOW_WAIT_SECONDS,
    )
    assert stack.balance() == before

    base = f"{outputs['api_url']}/transfers/{transfer_id}"
    status, reported = _get(base, user["token"])
    assert (status, reported["status"]) == (200, "awaiting_step_up")

    # A forged verification is refused and the hold stays exactly where it was.
    forged = {"session": "forged-session", "credential": {"id": "forged", "response": {}}}
    status, _ = post(f"{base}/stepup/verify", forged, user["token"])
    assert status == 403
    assert stack.transfer(transfer_id)["status"] == "awaiting_step_up"
    assert stack.balance() == before

    # The call the transfers function makes once Cognito has accepted a passkey.
    stack.sfn.send_task_success(
        taskToken=held["task_token"], output=json.dumps({"verified": True, "method": "passkey"})
    )
    transfer = _settled(stack, transfer_id)
    assert stack.execution(transfer_id)["status"] == "SUCCEEDED"
    assert transfer["status"] == "released"
    assert "task_token" not in transfer
    assert stack.balance() == before - Decimal("250.00")


def test_a_step_up_that_times_out_is_cancelled_and_never_debited(
    stack: Stack, outputs: dict[str, Any]
) -> None:
    before = stack.balance()
    transfer_id = f"{TEST_PREFIX}timeout-{time.time_ns()}"
    stack.created.append((f"LEDGER#{stack.sub}", f"TXN#{transfer_id}"))

    # Started directly so the hold can be seconds rather than the deployed five minutes; the input
    # is exactly what scoring sends.
    stack.sfn.start_execution(
        stateMachineArn=outputs["state_machine_arn"],
        name=transfer_id,
        input=json.dumps(
            {
                "transfer_id": transfer_id,
                "decision_id": transfer_id,
                "uid": stack.sub,
                "amount": 300.0,
                "payee_id": stack.payee_id("timeout"),
                "action": "step_up",
                "response": "step_up",
                "risk": 90.0,
                "hold_seconds": 5,
                "review_seconds": 5,
            }
        ),
    )
    transfer = _settled(stack, transfer_id)
    assert stack.execution(transfer_id)["status"] == "SUCCEEDED"
    assert (transfer["status"], transfer["reason"]) == ("cancelled", "timeout")
    assert stack.balance() == before


def test_a_corroborated_block_alerts_and_is_cancelled(
    stack: Stack, user: dict[str, str]  # noqa: F811
) -> None:
    stack.seed_takeover_profile()
    payee = "blocked"
    stack.put(
        {
            "PK": f"PAYEE#{stack.payee_id(payee)}",
            "SK": "RISK",
            "risk_score": Decimal("1"),
            "computed_at": int(time.time()),
            "flagged": True,
        }
    )
    before = stack.balance()

    body = stack.confirm(user["token"], 5000.0, payee, fields=4)
    transfer_id = body["decision_id"]
    assert body["action"] == "block", body
    assert body["transfer"]["status"] == "blocked"

    transfer = _settled(stack, transfer_id)
    assert (transfer["status"], transfer["reason"]) == ("cancelled", "blocked")
    assert stack.balance() == before

    history = stack.sfn.get_execution_history(
        executionArn=stack.execution(transfer_id)["executionArn"], maxResults=100
    )["events"]
    exited = {
        event["stateExitedEventDetails"]["name"]
        for event in history
        if "stateExitedEventDetails" in event
    }
    assert {"NotifyBlocked", "CancelBlocked"} <= exited
    failures = [e for e in history if e["type"] == "TaskFailed"]
    assert not failures, failures


def _get(url: str, token: str) -> tuple[int, dict[str, Any]]:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")
