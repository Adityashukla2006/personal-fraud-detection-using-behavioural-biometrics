"""Phase 9 exit: the deployed aggregator turns lake history into the items the fast path reads, a
siphoning pattern is flagged, and the nightly schedule targets the aggregator.

History is written to the lake exactly as the archive would have written it, dated in the past, and
the aggregator is invoked as of the day after. The assertions are on the items in DynamoDB.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import pytest
from conftest import TEST_PREFIX

from fraudcore.events import lake_key
from simulator import siphoning

pytestmark = pytest.mark.integration


class History:
    def __init__(self, aws: boto3.Session, outputs: dict[str, Any]) -> None:
        self.s3 = aws.client("s3")
        self.table = aws.resource("dynamodb").Table(outputs["table_name"])
        self.bucket = outputs["lake_bucket"]
        self.keys: list[str] = []
        self.items: list[tuple[str, str]] = []

    def transfer(
        self, uid: str, payee_id: str, amount: float, at: datetime, identity: float
    ) -> None:
        for event in siphoning.lake_events(
            uid, secrets.token_hex(16), payee_id, amount, at, identity
        ):
            key = lake_key(event)
            self.s3.put_object(
                Bucket=self.bucket, Key=key, Body=(json.dumps(event) + "\n").encode("utf-8")
            )
            self.keys.append(key)

    def item(self, partition: str, sort: str) -> dict[str, Any]:
        self.items.append((partition, sort))
        return self.table.get_item(Key={"PK": partition, "SK": sort}, ConsistentRead=True).get(
            "Item", {}
        )

    def cleanup(self) -> None:
        for offset in range(0, len(self.keys), 1000):
            objects = [{"Key": key} for key in self.keys[offset : offset + 1000]]
            self.s3.delete_objects(Bucket=self.bucket, Delete={"Objects": objects})
        for partition, sort in self.items:
            self.table.delete_item(Key={"PK": partition, "SK": sort})


@pytest.fixture
def history(aws: boto3.Session, outputs: dict[str, Any]) -> Iterator[History]:
    history = History(aws, outputs)
    yield history
    history.cleanup()


def test_the_aggregator_flags_a_siphon_from_lake_history(
    aws: boto3.Session, outputs: dict[str, Any], history: History
) -> None:
    uid = f"{TEST_PREFIX}{secrets.token_hex(8)}"
    known = f"{TEST_PREFIX}known-{secrets.token_hex(8)}"
    mule = f"{TEST_PREFIX}mule-{secrets.token_hex(8)}"
    as_of = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
        days=1
    )

    # Established payee: first paid 60 days back, typed by the owner.
    for days_ago in (60, 45, 25):
        history.transfer(uid, known, 1500.0, as_of - timedelta(days=days_ago), 20.0)
    # A new payee paid every two days, typed by someone scoring twice as far from the profile.
    for days_ago in (10, 8, 6, 4, 2):
        history.transfer(uid, mule, 900.0, as_of - timedelta(days=days_ago, hours=-10), 40.0)

    response = aws.client("lambda").invoke(
        FunctionName=outputs["aggregator_function_name"],
        Payload=json.dumps({"as_of": as_of.isoformat()}).encode(),
    )
    summary = json.loads(response["Payload"].read())
    assert "FunctionError" not in response, summary
    assert summary["transfers"] >= 8

    window = history.item(f"AGG#{uid}", "WINDOW#30d")
    assert window["history_count"] == 6

    edge = history.item(f"AGG#{uid}", f"EDGE#{mule}")
    assert edge["flagged"] is True
    assert edge["transfers"] == 5
    assert float(edge["identity"]) == pytest.approx(1.0)

    assert history.item(f"AGG#{uid}", f"EDGE#{known}") == {}
    assert history.item(f"PAYEE#{mule}", "RISK")["flagged"] is True
    history.item(f"PAYEE#{known}", "RISK")


def test_the_nightly_schedule_targets_the_aggregator(
    aws: boto3.Session, outputs: dict[str, Any]
) -> None:
    schedule = aws.client("scheduler").get_schedule(Name=outputs["aggregator_schedule_name"])
    assert schedule["State"] == "ENABLED"
    assert schedule["Target"]["Arn"].endswith(f":function:{outputs['aggregator_function_name']}")
