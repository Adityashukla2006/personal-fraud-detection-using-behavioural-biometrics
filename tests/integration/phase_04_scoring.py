"""Phase 4 exit, automated half: an authenticated checkpoint is scored end to end.

Cognito issues a token, API Gateway validates it, the Lambda reads state, scores and writes, and the
decision lands in DynamoDB. The other half of the exit -- a real browser session with a passkey --
needs a person and uses exactly this path.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from conftest import TEST_PREFIX

pytestmark = pytest.mark.integration

ACTIONS = {"allow", "monitor", "step_up", "restrict", "block"}


def _field(tempo: float = 1.0) -> dict[str, Any]:
    down_down = [v * tempo for v in (0.21, 0.35, 0.18, 0.42, 0.27, 0.31, 0.24, 0.39, 0.22, 0.33)]
    hold = [0.09, 0.11, 0.08, 0.10, 0.12, 0.09, 0.10, 0.11, 0.08, 0.10, 0.09]
    return {
        "hold": hold,
        "down_down": [round(v, 4) for v in down_down],
        "up_down": [round(d - h, 4) for d, h in zip(down_down, hold, strict=False)],
        "backspaces": 0,
        "corrections": 0,
        "pastes": 0,
    }


def _payload(session_id: str, checkpoint: str = "login", **extra: Any) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "checkpoint": checkpoint,
        "device": {
            "device_id": f"{TEST_PREFIX}device01",
            "max_touch_points": 0,
            "coarse_pointer": False,
            "short_side_px": 1080,
        },
        "behaviour": {"fields": [_field()]},
        **extra,
    }


def post(url: str, body: Any, token: str | None = None) -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def sign_in(cognito: Any, client_id: str, email: str, password: str) -> str:
    result = cognito.initiate_auth(
        AuthFlow="USER_AUTH",
        ClientId=client_id,
        AuthParameters={"USERNAME": email, "PREFERRED_CHALLENGE": "PASSWORD", "PASSWORD": password},
    )
    if "AuthenticationResult" not in result:
        result = cognito.respond_to_auth_challenge(
            ClientId=client_id,
            ChallengeName="SELECT_CHALLENGE",
            Session=result["Session"],
            ChallengeResponses={"USERNAME": email, "ANSWER": "PASSWORD", "PASSWORD": password},
        )
    return result["AuthenticationResult"]["AccessToken"]


@pytest.fixture(scope="module")
def user(aws: boto3.Session, outputs: dict[str, Any]) -> Iterator[dict[str, str]]:
    cognito = aws.client("cognito-idp")
    pool = outputs["user_pool_id"]
    email = f"{TEST_PREFIX}{secrets.token_hex(6)}@example.com"
    password = f"{secrets.token_urlsafe(18)}Aa1"

    created = cognito.admin_create_user(
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
        sub = next(a["Value"] for a in created["User"]["Attributes"] if a["Name"] == "sub")
        token = sign_in(cognito, outputs["user_pool_client_id"], email, password)
        yield {"email": email, "sub": sub, "token": token}
    finally:
        cognito.admin_delete_user(UserPoolId=pool, Username=email)


@pytest.fixture
def cleanup(aws: boto3.Session, outputs: dict[str, Any]) -> Iterator[list[str]]:
    partitions: list[str] = []
    yield partitions
    client = aws.client("dynamodb")
    for partition in partitions:
        client.delete_item(
            TableName=outputs["table_name"], Key={"PK": {"S": partition}, "SK": {"S": "META"}}
        )


def _item(aws: boto3.Session, outputs: dict[str, Any], partition: str) -> dict[str, Any]:
    response = aws.resource("dynamodb").Table(outputs["table_name"]).get_item(
        Key={"PK": partition, "SK": "META"}, ConsistentRead=True
    )
    return response.get("Item", {})


def test_an_unauthenticated_request_is_rejected_at_the_edge(
    outputs: dict[str, Any], test_key: str
) -> None:
    status, _ = post(f"{outputs['api_url']}/score", _payload(test_key))
    assert status == 401


def test_checkpoints_are_scored_accumulated_and_persisted(
    aws: boto3.Session,
    outputs: dict[str, Any],
    user: dict[str, str],
    test_key: str,
    cleanup: list[str],
) -> None:
    url = f"{outputs['api_url']}/score"
    cleanup.append(f"SESS#{test_key}")

    status, login = post(url, _payload(test_key), user["token"])
    assert status == 200, login
    cleanup.append(f"DEC#{login['decision_id']}")
    assert login["action"] in ACTIONS
    # A fail-open decision has no contributions, so three proves the state read succeeded.
    assert login["constraints"] != ["fail_open"]
    assert len(login["contributions"]) == 3

    transfer = {"payee_id": hashlib.sha256(test_key.encode()).hexdigest(), "amount": 250}
    status, confirm = post(
        url, _payload(test_key, "confirmation", transaction=transfer), user["token"]
    )
    assert status == 200, confirm
    cleanup.append(f"DEC#{confirm['decision_id']}")
    assert confirm["constraints"] != ["fail_open"]

    decision = _item(aws, outputs, f"DEC#{confirm['decision_id']}")
    assert decision["uid"] == user["sub"]
    assert decision["GSI1PK"] == f"USER#{user['sub']}"
    assert decision["action"] == confirm["action"]
    assert set(decision["scores"]) == {"behaviour", "automation", "transaction", "context", "payee"}

    session = _item(aws, outputs, f"SESS#{test_key}")
    assert session["uid"] == user["sub"]
    assert len(session["fields"]) == 2


def test_a_content_field_is_refused(
    outputs: dict[str, Any], user: dict[str, str], test_key: str
) -> None:
    payload = _payload(test_key)
    payload["behaviour"]["fields"][0]["key"] = "h"
    status, body = post(f"{outputs['api_url']}/score", payload, user["token"])
    assert status == 400
    assert "unexpected keys" in body["error"]


def test_a_replayed_session_is_recognised(
    aws: boto3.Session,
    outputs: dict[str, Any],
    user: dict[str, str],
    test_key: str,
    cleanup: list[str],
) -> None:
    """A confirmed session joins the replay history, and a later exact copy of it is flagged."""
    url = f"{outputs['api_url']}/score"
    table = aws.resource("dynamodb").Table(outputs["table_name"])
    original, copy = f"{test_key}a", f"{test_key}b"
    cleanup.extend([f"SESS#{original}", f"SESS#{copy}"])
    transfer = {"payee_id": hashlib.sha256(test_key.encode()).hexdigest(), "amount": 20}
    try:
        status, first = post(
            url, _payload(original, "confirmation", transaction=transfer), user["token"]
        )
        assert status == 200, first
        cleanup.append(f"DEC#{first['decision_id']}")

        status, second = post(url, _payload(copy), user["token"])
        assert status == 200, second
        cleanup.append(f"DEC#{second['decision_id']}")

        decision = _item(aws, outputs, f"DEC#{second['decision_id']}")
        assert decision["scores"]["automation"]["score"] == 1
    finally:
        table.delete_item(Key={"PK": f"REPLAY#{user['sub']}", "SK": "HISTORY"})


def test_the_client_is_served_with_live_configuration(outputs: dict[str, Any]) -> None:
    with urllib.request.urlopen(outputs["client_url"], timeout=15) as response:
        assert response.status == 200
        assert b"<html" in response.read()
    with urllib.request.urlopen(f"{outputs['client_url']}/config.mjs", timeout=15) as response:
        config = response.read().decode()
    assert outputs["user_pool_client_id"] in config
    assert outputs["api_url"] in config
