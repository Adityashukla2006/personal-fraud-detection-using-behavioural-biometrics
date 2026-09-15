"""Phase 6 exit: every decision and workflow outcome reaches the audit lake, and a passkey-verified
step-up reaches the adaptation function while any other verification does not.

Everything enters through the real scoring endpoint and the real workflow; the assertions are on the
objects in S3 and the item adaptation writes, never on log lines.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import pytest
from conftest import TEST_PREFIX
from phase_04_scoring import _payload, post, user  # noqa: F401
from phase_05_workflow import Stack, _settled, stack, wait_for  # noqa: F401

pytestmark = pytest.mark.integration

LAKE_WAIT_SECONDS = 90
# Long enough for a matching rule to have delivered, once the archive already holds the event.
RULE_SETTLE_SECONDS = 20


class Lake:
    def __init__(self, aws: boto3.Session, bucket: str) -> None:
        self.s3 = aws.client("s3")
        self.bucket = bucket
        self.keys: set[str] = set()

    def events(self, detail_type: str, subject: str) -> list[dict[str, Any]]:
        today = datetime.now(UTC).date()
        found = []
        for day in (today, today - timedelta(days=1)):
            prefix = f"events/type={detail_type}/dt={day:%Y-%m-%d}/{subject}."
            listing = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix)
            for summary in listing.get("Contents", []):
                self.keys.add(summary["Key"])
                body = self.s3.get_object(Bucket=self.bucket, Key=summary["Key"])["Body"].read()
                found.append(json.loads(body))
        return found

    def wait(self, detail_type: str, subject: str) -> dict[str, Any]:
        return wait_for(
            lambda: self.events(detail_type, subject), bool, LAKE_WAIT_SECONDS
        )[0]

    def cleanup(self) -> None:
        for key in self.keys:
            self.s3.delete_object(Bucket=self.bucket, Key=key)


@pytest.fixture
def lake(aws: boto3.Session, outputs: dict[str, Any]) -> Iterator[Lake]:
    lake = Lake(aws, outputs["lake_bucket"])
    yield lake
    lake.cleanup()


def _step_up(stack: Stack, token: str) -> tuple[str, str]:  # noqa: F811
    stack.seed_takeover_profile()
    body = stack.confirm(token, 90.0, f"events-{time.time_ns()}", fields=4)
    assert body["action"] == "step_up", body
    held = wait_for(
        lambda: stack.transfer(body["decision_id"]),
        lambda item: "task_token" in item,
        LAKE_WAIT_SECONDS,
    )
    return body["decision_id"], held["task_token"]


def test_every_decision_and_the_transfer_outcome_reach_the_lake(
    stack: Stack, lake: Lake, outputs: dict[str, Any], user: dict[str, str]  # noqa: F811
) -> None:
    session = f"{TEST_PREFIX}lake{time.time_ns()}"
    status, login = post(f"{outputs['api_url']}/score", _payload(session), user["token"])
    assert status == 200, login
    stack.created += [(f"SESS#{session}", "META"), (f"DEC#{login['decision_id']}", "META")]

    confirm = stack.confirm(user["token"], 75.0, "lake")
    _settled(stack, confirm["decision_id"])

    login_event = lake.wait("decision.scored", login["decision_id"])
    assert login_event["source"] == "bfd.scoring"
    assert login_event["detail"]["action"] == login["action"]
    assert login_event["detail"]["checkpoint"] == "login"
    assert "transaction" not in login_event["detail"]

    confirm_event = lake.wait("decision.scored", confirm["decision_id"])
    assert confirm_event["detail"]["transaction"]["amount"] == 75.0
    # The audit lake records what was decided, never the keystroke evidence.
    for timing_key in ("fields", "hold", "down_down", "up_down"):
        assert f'"{timing_key}"' not in json.dumps(confirm_event)

    outcome = lake.wait("transfer.completed", confirm["decision_id"])
    assert outcome["source"] == "bfd.workflow"
    assert outcome["detail"]["status"] == "released"
    assert outcome["detail"]["amount"] == 75.0


def test_a_passkey_verified_step_up_reaches_adaptation(
    stack: Stack, lake: Lake, user: dict[str, str]  # noqa: F811
) -> None:
    transfer_id, token = _step_up(stack, user["token"])
    verified_key = (f"USER#{stack.sub}", f"VERIFIED#{transfer_id}")
    stack.created.append(verified_key)

    stack.sfn.send_task_success(
        taskToken=token,
        output=json.dumps(
            {"verified": True, "method": "passkey", "verified_at": int(time.time())}
        ),
    )
    assert _settled(stack, transfer_id)["status"] == "released"

    record = wait_for(lambda: stack.item(*verified_key), bool, LAKE_WAIT_SECONDS)
    assert record["method"] == "passkey"
    assert record["transfer_id"] == transfer_id

    verified = lake.wait("stepup.verified", transfer_id)
    assert verified["detail"]["verification"]["method"] == "passkey"
    assert lake.wait("transfer.completed", transfer_id)["detail"]["status"] == "released"


def test_a_non_passkey_verification_never_reaches_adaptation(
    stack: Stack, lake: Lake, user: dict[str, str]  # noqa: F811
) -> None:
    transfer_id, token = _step_up(stack, user["token"])
    verified_key = (f"USER#{stack.sub}", f"VERIFIED#{transfer_id}")
    stack.created.append(verified_key)

    stack.sfn.send_task_success(
        taskToken=token, output=json.dumps({"verified": True, "method": "password"})
    )
    _settled(stack, transfer_id)

    # The archive receives every event, so its copy proves the event was published; the adaptation
    # rule has then had ample time to deliver it, had it matched.
    published = lake.wait("stepup.verified", transfer_id)
    assert published["detail"]["verification"]["method"] == "password"
    time.sleep(RULE_SETTLE_SECONDS)
    assert stack.item(*verified_key) == {}
