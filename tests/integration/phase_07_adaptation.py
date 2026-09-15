"""Phase 7 exit, live half: the deployed profile changes only on verified, trusted sessions.

Each verified session runs the real path: a transfer execution holds on step-up, the verification
completes it, the workflow publishes ``stepup.verified``, the rule delivers it, and the adaptation
function reads the decision and session, computes trust and rebuilds the profile. The decision and
session items are seeded so that each case's trust is exactly what the case is about.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from conftest import TEST_PREFIX
from phase_04_scoring import _field, user  # noqa: F401
from phase_05_workflow import Stack, _settled, stack, wait_for  # noqa: F401

from fraudcore.adaptation import PROFILE_FEATURES, AdaptationPolicy, displacement

pytestmark = pytest.mark.integration

ADAPT_WAIT_SECONDS = 90
# Long enough for a matching rule to have delivered and the function to have run.
RULE_SETTLE_SECONDS = 25
FAR = 5.0
TIGHT = 0.01


def _decimal(value: Any) -> Any:
    return json.loads(json.dumps(value), parse_float=Decimal)


@pytest.fixture
def partition(
    stack: Stack, aws: boto3.Session, outputs: dict[str, Any]  # noqa: F811
) -> Iterator[None]:
    yield
    # Adaptation writes device, buffer, verification and profile items; remove the whole partition.
    table = aws.resource("dynamodb").Table(outputs["table_name"])
    items = table.query(KeyConditionExpression=Key("PK").eq(f"USER#{stack.sub}"))["Items"]
    for item in items:
        table.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})


def _seed_device(stack: Stack, device_id: str, sessions: int) -> None:  # noqa: F811
    stack.put(
        {
            "PK": f"USER#{stack.sub}",
            "SK": f"DEV#{device_id}",
            "session_count": sessions,
            "device_class": "desktop",
        }
    )


def _seed_profile(stack: Stack, version: int = 3) -> None:  # noqa: F811
    width = len(PROFILE_FEATURES)
    stack.put(
        {
            "PK": f"USER#{stack.sub}",
            "SK": "PROFILE#desktop",
            "features": list(PROFILE_FEATURES),
            "mu": [Decimal(str(FAR))] * width,
            "sigma": [Decimal(str(TIGHT))] * width,
            "anchor_mu": [Decimal(str(FAR))] * width,
            "anchor_sigma": [Decimal(str(TIGHT))] * width,
            "anchor_at": 0,
            "saturations": 0,
            "n_sessions": 20,
            "version": version,
        }
    )


def _seed_buffer(stack: Stack, count: int) -> None:  # noqa: F811
    for index in range(count):
        stack.put(
            {
                "PK": f"USER#{stack.sub}",
                "SK": f"BUF#desktop#{index + 1:013d}#{TEST_PREFIX}seed{index}",
                "features": [Decimal("0.1")] * len(PROFILE_FEATURES),
                "trust": Decimal("1"),
                "verified": True,
            }
        )


def verified_session(stack: Stack, device_id: str, method: str = "passkey") -> str:  # noqa: F811
    """Run one step-up transfer to completion and return its decision id."""
    decision_id = f"{TEST_PREFIX}adapt{time.time_ns()}"
    expires = int(time.time()) + 3600
    stack.put(
        {
            "PK": f"SESS#{decision_id}",
            "SK": "META",
            "uid": stack.sub,
            "fields": _decimal([_field(), _field(1.1)]),
            "ttl": expires,
        }
    )
    stack.put(
        {
            "PK": f"DEC#{decision_id}",
            "SK": "META",
            "uid": stack.sub,
            "session_id": decision_id,
            "checkpoint": "confirmation",
            "device_class": "desktop",
            "device_id": device_id,
            "action": "step_up",
            # Quiet non-behavioural channels, so c_txn is 1.0 and trust turns on device and method.
            "scores": {
                name: {"score": Decimal("0"), "confidence": Decimal("1")}
                for name in ("transaction", "context", "payee")
            },
            "ttl": expires,
        }
    )
    stack.created.append((f"LEDGER#{stack.sub}", f"TXN#{decision_id}"))
    stack.sfn.start_execution(
        stateMachineArn=stack.outputs["state_machine_arn"],
        name=decision_id,
        input=json.dumps(
            {
                "transfer_id": decision_id,
                "decision_id": decision_id,
                "uid": stack.sub,
                "amount": 10.0,
                "payee_id": stack.payee_id("adaptation"),
                "action": "step_up",
                "response": "step_up",
                "risk": 90.0,
                "hold_seconds": 120,
                "review_seconds": 120,
            }
        ),
    )
    held = wait_for(
        lambda: stack.transfer(decision_id), lambda item: "task_token" in item, ADAPT_WAIT_SECONDS
    )
    stack.sfn.send_task_success(
        taskToken=held["task_token"],
        output=json.dumps({"verified": True, "method": method, "verified_at": int(time.time())}),
    )
    assert _settled(stack, decision_id)["status"] == "released"
    return decision_id


def _profile(stack: Stack) -> dict[str, Any]:  # noqa: F811
    return stack.item(f"USER#{stack.sub}", "PROFILE#desktop")


def test_verified_trusted_sessions_bootstrap_a_profile_after_cold_start(
    stack: Stack, partition: None  # noqa: F811
) -> None:
    device = f"{TEST_PREFIX}enrolled"
    _seed_device(stack, device, sessions=12)
    policy = AdaptationPolicy.load()
    _seed_buffer(stack, policy.cold_start_sessions - 1)

    decision_id = verified_session(stack, device)

    profile = wait_for(lambda: _profile(stack), bool, ADAPT_WAIT_SECONDS)
    assert profile["version"] == 1
    assert profile["n_sessions"] == policy.cold_start_sessions
    assert profile["anchor_mu"] == profile["mu"]

    record = stack.item(f"USER#{stack.sub}", f"VERIFIED#{decision_id}")
    assert (record["trust"], record["admitted"]) == (Decimal("1"), True)
    assert stack.item(f"USER#{stack.sub}", f"DEV#{device}")["session_count"] == 13


def test_a_poisoning_step_up_moves_the_profile_exactly_one_budget(
    stack: Stack, partition: None  # noqa: F811
) -> None:
    device = f"{TEST_PREFIX}enrolled"
    _seed_device(stack, device, sessions=12)
    _seed_profile(stack, version=3)

    verified_session(stack, device)

    profile = wait_for(
        lambda: _profile(stack), lambda item: item.get("version") == 4, ADAPT_WAIT_SECONDS
    )
    width = len(PROFILE_FEATURES)
    moved = displacement(
        [float(value) for value in profile["mu"]], [FAR] * width, [TIGHT] * width
    )
    # The session sits hundreds of scaled units away; the budget lets the profile move exactly B.
    assert moved == pytest.approx(AdaptationPolicy.load().budget, rel=1e-6)
    assert profile["saturations"] == 1
    # A passed step-up is full trust, so the projected position becomes the new anchor.
    assert profile["anchor_mu"] == profile["mu"]


def test_unverified_or_untrusted_sessions_never_change_the_profile(
    stack: Stack, partition: None  # noqa: F811
) -> None:
    _seed_profile(stack, version=3)
    enrolled = f"{TEST_PREFIX}enrolled"
    _seed_device(stack, enrolled, sessions=12)

    by_password = verified_session(stack, enrolled, method="password")
    from_new_device = verified_session(stack, f"{TEST_PREFIX}brand-new")

    record = wait_for(
        lambda: stack.item(f"USER#{stack.sub}", f"VERIFIED#{from_new_device}"),
        bool,
        ADAPT_WAIT_SECONDS,
    )
    assert (record["trust"], record["admitted"]) == (Decimal("0"), False)

    time.sleep(RULE_SETTLE_SECONDS)
    assert stack.item(f"USER#{stack.sub}", f"VERIFIED#{by_password}") == {}
    assert _profile(stack)["version"] == 3
    buffered = [
        item
        for item in stack.table.query(
            KeyConditionExpression=Key("PK").eq(f"USER#{stack.sub}") & Key("SK").begins_with("BUF#")
        )["Items"]
    ]
    assert buffered == []
