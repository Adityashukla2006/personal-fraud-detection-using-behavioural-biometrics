"""Operator console, end to end: analysts only, cross-user reads, and release of reviewed transfers.

Runs against the deployed dev stack. A plain user makes a real transfer through scoring; an analyst,
created in the analyst group, reads it back through the console API and releases a transfer held
for review.
"""

from __future__ import annotations

import json
import secrets
import time
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from conftest import TEST_PREFIX
from phase_04_scoring import post, sign_in, user  # noqa: F401
from phase_05_workflow import Stack, _get, _settled, stack, wait_for  # noqa: F401

pytestmark = pytest.mark.integration

WAIT_SECONDS = 90


@pytest.fixture(scope="module")
def analyst(aws: boto3.Session, outputs: dict[str, Any]) -> Iterator[dict[str, str]]:
    cognito = aws.client("cognito-idp")
    pool = outputs["user_pool_id"]
    email = f"{TEST_PREFIX}analyst-{secrets.token_hex(4)}@example.com"
    password = f"{secrets.token_urlsafe(18)}Aa1"
    cognito.admin_create_user(
        UserPoolId=pool,
        Username=email,
        TemporaryPassword=password,
        UserAttributes=[
            {"Name": "email", "Value": email},
            {"Name": "email_verified", "Value": "true"},
        ],
        MessageAction="SUPPRESS",
    )
    try:
        cognito.admin_set_user_password(
            UserPoolId=pool, Username=email, Password=password, Permanent=True
        )
        # Group membership is read into the token at sign-in, so join the group first.
        cognito.admin_add_user_to_group(
            UserPoolId=pool, Username=email, GroupName=outputs["analyst_group"]
        )
        token = sign_in(cognito, outputs["user_pool_client_id"], email, password)
        yield {"email": email, "token": token}
    finally:
        cognito.admin_delete_user(UserPoolId=pool, Username=email)


def test_a_signed_in_user_outside_the_analyst_group_is_refused(
    outputs: dict[str, Any], user: dict[str, str]  # noqa: F811
) -> None:
    status, _ = _get(f"{outputs['api_url']}/console/users", user["token"])
    assert status == 403


def test_the_analyst_sees_the_users_decisions_transfers_and_lake(
    stack: Stack,  # noqa: F811
    outputs: dict[str, Any],
    user: dict[str, str],  # noqa: F811
    analyst: dict[str, str],
) -> None:
    confirm = stack.confirm(user["token"], 42.0, "console")
    _settled(stack, confirm["decision_id"])
    base = f"{outputs['api_url']}/console"

    status, users = _get(f"{base}/users", analyst["token"])
    assert status == 200
    assert user["sub"] in {entry["uid"] for entry in users["users"]}

    status, detail = _get(f"{base}/users/{user['sub']}", analyst["token"])
    assert status == 200
    assert confirm["decision_id"] in {d["decision_id"] for d in detail["decisions"]}
    transfer = next(t for t in detail["transfers"] if t["transfer_id"] == confirm["decision_id"])
    assert transfer["status"] == "released"
    assert "task_token" not in json.dumps(detail)

    events = wait_for(
        lambda: _get(f"{base}/lake", analyst["token"])[1].get("events", []),
        lambda found: confirm["decision_id"] in {event["subject"] for event in found},
        WAIT_SECONDS,
    )
    assert any(event["type"] == "decision.scored" for event in events)


def test_an_analyst_releases_a_transfer_held_for_review(
    stack: Stack,  # noqa: F811
    outputs: dict[str, Any],
    analyst: dict[str, str],
) -> None:
    transfer_id = f"{TEST_PREFIX}review{time.time_ns()}"
    stack.created.append((f"LEDGER#{stack.sub}", f"TXN#{transfer_id}"))
    stack.sfn.start_execution(
        stateMachineArn=outputs["state_machine_arn"],
        name=transfer_id,
        input=json.dumps(
            {
                "transfer_id": transfer_id,
                "decision_id": transfer_id,
                "uid": stack.sub,
                "amount": 64.0,
                "payee_id": stack.payee_id("review"),
                "action": "restrict",
                "response": "review",
                "risk": 70.0,
                "hold_seconds": 120,
                "review_seconds": 120,
            }
        ),
    )
    wait_for(
        lambda: stack.transfer(transfer_id),
        lambda item: item.get("status") == "under_review" and "task_token" in item,
        WAIT_SECONDS,
    )

    url = f"{outputs['api_url']}/console/transfers/{stack.sub}/{transfer_id}/release"
    status, body = post(url, {}, analyst["token"])
    assert (status, body) == (200, {"status": "release_requested"})
    assert _settled(stack, transfer_id)["status"] == "released"

    # Released once: a second release finds nothing under review.
    status, body = post(url, {}, analyst["token"])
    assert (status, body["status"]) == (409, "released")
