"""Tests for the archive Lambda that writes events to the audit lake."""

from __future__ import annotations

import json
from typing import Any

import pytest

from archive.handler import handle

EVENT = {
    "version": "0",
    "id": "4e5b2f6a-1c2d-4e3f-8a9b-0c1d2e3f4a5b",
    "detail-type": "transfer.completed",
    "source": "bfd.workflow",
    "time": "2026-09-15T08:46:46Z",
    "detail": {"schema": 1, "transfer_id": "txn0001", "status": "released"},
}


class FakeS3:
    def __init__(self) -> None:
        self.objects: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> None:
        self.objects.append(kwargs)


def test_an_event_is_written_as_one_json_line_under_its_partition() -> None:
    s3 = FakeS3()
    result = handle(EVENT, s3, "lake")

    written = s3.objects[0]
    assert written["Bucket"] == "lake"
    assert written["Key"] == result["key"]
    assert written["Key"].startswith("events/type=transfer.completed/dt=2026-09-15/txn0001.")
    body = written["Body"].decode("utf-8")
    assert body.endswith("\n") and body.count("\n") == 1
    assert json.loads(body) == EVENT


def test_a_redelivered_event_writes_the_same_key() -> None:
    s3 = FakeS3()
    handle(EVENT, s3, "lake")
    handle(EVENT, s3, "lake")
    assert s3.objects[0]["Key"] == s3.objects[1]["Key"]


def test_an_event_that_cannot_be_keyed_writes_nothing() -> None:
    s3 = FakeS3()
    with pytest.raises(ValueError):
        handle({**EVENT, "detail-type": "unknown.type"}, s3, "lake")
    assert s3.objects == []
