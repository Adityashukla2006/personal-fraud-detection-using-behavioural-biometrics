"""Tests for the operator console API, with fakes for AWS.

The access rule under test: nothing is returned to a caller outside the analyst group, and no
response ever contains a task token.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from botocore.exceptions import ClientError

from console.handler import Dependencies, groups, handle

NOW = datetime(2026, 9, 15, 10, 0, tzinfo=UTC).timestamp()
UID = "21d35d6a-a031-708a-d069-7ca41d1ac623"
TRANSFER = "a" * 32
ANALYST = {"sub": "analyst-0001", "cognito:groups": "[analyst]"}


def _error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "Operation")  # type: ignore[arg-type]


class FakeTable:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items
        self.queries: list[dict[str, Any]] = []

    def query(self, **kwargs: Any) -> dict[str, Any]:
        self.queries.append(kwargs)
        condition = kwargs["KeyConditionExpression"]
        name, value = condition._values[0].name, condition._values[1]
        found = [item for item in self.items if item.get(name) == value]
        if kwargs.get("ScanIndexForward") is False:
            found = list(reversed(found))
        return {"Items": found[: kwargs.get("Limit", len(found))]}

    def get_item(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        for item in self.items:
            if item["PK"] == key["PK"] and item["SK"] == key["SK"]:
                return {"Item": item}
        return {}


class FakeCognito:
    def list_users(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "Users": [
                {
                    "UserStatus": "CONFIRMED",
                    "UserCreateDate": datetime(2026, 9, 1, tzinfo=UTC),
                    "Attributes": [
                        {"Name": "sub", "Value": UID},
                        {"Name": "email", "Value": "user@example.com"},
                    ],
                }
            ]
        }


class FakeS3:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.objects = {
            f"events/type={e['detail-type']}/dt=2026-09-15/{e['id']}.json": (i, e)
            for i, e in enumerate(events)
        }

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        return {
            "Contents": [
                {"Key": key, "LastModified": datetime(2026, 9, 15, 9, index, tzinfo=UTC)}
                for key, (index, _) in self.objects.items()
                if key.startswith(kwargs["Prefix"])
            ]
        }

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        return {"Body": io.BytesIO(json.dumps(self.objects[kwargs["Key"]][1]).encode())}


class FakeSfn:
    def __init__(self, error: ClientError | None = None) -> None:
        self.error = error
        self.successes: list[dict[str, Any]] = []

    def send_task_success(self, **kwargs: Any) -> None:
        if self.error:
            raise self.error
        self.successes.append(kwargs)


ITEMS: list[dict[str, Any]] = [
    {
        "PK": "DEC#dec-older",
        "SK": "META",
        "GSI1PK": f"USER#{UID}",
        "GSI1SK": "TS#2026-09-15T09:00:00.000+00:00",
        "checkpoint": "login",
        "action": "allow",
        "risk": Decimal("4.7"),
    },
    {
        "PK": "DEC#dec-newer",
        "SK": "META",
        "GSI1PK": f"USER#{UID}",
        "GSI1SK": "TS#2026-09-15T09:05:00.000+00:00",
        "checkpoint": "confirmation",
        "action": "restrict",
        "risk": Decimal("70.25"),
        "contributions": [{"channel": "payee", "value": Decimal("2.5")}],
    },
    {
        "PK": f"USER#{UID}",
        "SK": "PROFILE#desktop",
        "features": ["mean_hold"],
        "mu": [Decimal("0.1")],
        "sigma": [Decimal("0.01")],
        "version": 4,
        "n_sessions": 6,
        "saturations": 1,
    },
    {"PK": f"USER#{UID}", "SK": "DEV#device-1", "session_count": 13, "device_class": "desktop"},
    {
        "PK": f"USER#{UID}",
        "SK": "VERIFIED#dec-newer",
        "method": "passkey",
        "trust": Decimal("1"),
        "admitted": True,
        "received_at": 100,
    },
    {"PK": f"USER#{UID}", "SK": "BUF#desktop#0000000000001#dec-1", "trust": Decimal("1")},
    {"PK": f"USER#{UID}", "SK": "BUF#desktop#0000000000002#dec-2", "trust": Decimal("1")},
    {"PK": f"LEDGER#{UID}", "SK": "BALANCE", "balance": Decimal("99998")},
    {
        "PK": f"LEDGER#{UID}",
        "SK": f"TXN#{TRANSFER}",
        "status": "under_review",
        "amount": Decimal("5000.00"),
        "action": "restrict",
        "created_at": 200,
        "task_token": "secret-task-token",
    },
]


def _deps(
    table: FakeTable | None = None, sfn: FakeSfn | None = None, events: Any = ()
) -> Dependencies:
    return Dependencies(
        table=table or FakeTable(ITEMS),
        cognito=FakeCognito(),
        s3=FakeS3(list(events)),
        sfn=sfn or FakeSfn(),
        user_pool_id="pool",
        lake_bucket="lake",
        analyst_group="analyst",
        clock=lambda: NOW,
    )


def _call(
    route: str,
    deps: Dependencies,
    claims: dict[str, Any] = ANALYST,
    params: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    event = {
        "routeKey": route,
        "pathParameters": params or {},
        "requestContext": {"authorizer": {"jwt": {"claims": claims}}},
    }
    response = handle(event, deps)
    return response["statusCode"], json.loads(response["body"])


class TestAccess:
    def test_missing_claims_are_unauthenticated(self) -> None:
        assert _call("GET /console/users", _deps(), claims={})[0] == 401

    def test_a_signed_in_user_outside_the_analyst_group_sees_nothing(self) -> None:
        status, body = _call("GET /console/users", _deps(), claims={"sub": "user-1"})
        assert status == 403
        assert "users" not in body

    def test_another_group_is_not_enough(self) -> None:
        claims = {"sub": "user-1", "cognito:groups": "[support]"}
        assert _call("GET /console/users", _deps(), claims=claims)[0] == 403

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("[analyst]", {"analyst"}),
            ("[admin analyst]", {"admin", "analyst"}),
            ("[admin,analyst]", {"admin", "analyst"}),
            ("analyst", {"analyst"}),
            (["analyst", "admin"], {"analyst", "admin"}),
            ("[]", set()),
        ],
    )
    def test_the_groups_claim_is_parsed_in_every_shape(self, raw: Any, expected: set[str]) -> None:
        assert groups({"cognito:groups": raw}) == expected


class TestReads:
    def test_users_are_listed_by_email(self) -> None:
        status, body = _call("GET /console/users", _deps())
        assert status == 200
        assert body["users"][0] == {
            "uid": UID,
            "email": "user@example.com",
            "status": "CONFIRMED",
            "created": "2026-09-01T00:00:00+00:00",
        }

    def test_user_detail_gathers_decisions_transfers_and_adaptation(self) -> None:
        status, body = _call("GET /console/users/{uid}", _deps(), params={"uid": UID})
        assert status == 200
        assert [d["decision_id"] for d in body["decisions"]] == ["dec-newer", "dec-older"]
        assert body["decisions"][0]["risk"] == 70.25
        assert body["balance"] == 99998
        assert body["transfers"][0]["status"] == "under_review"
        assert body["transfers"][0]["held"] is True
        adaptation = body["adaptation"]
        assert adaptation["profiles"][0]["version"] == 4
        assert adaptation["devices"][0]["sessions"] == 13
        assert adaptation["verifications"][0]["trust"] == 1
        assert adaptation["buffered"] == {"desktop": 2}

    def test_no_response_ever_carries_a_task_token(self) -> None:
        _, body = _call("GET /console/users/{uid}", _deps(), params={"uid": UID})
        assert "secret-task-token" not in json.dumps(body)
        assert "task_token" not in json.dumps(body)

    def test_a_malformed_uid_is_not_found(self) -> None:
        assert _call("GET /console/users/{uid}", _deps(), params={"uid": "../x"})[0] == 404

    def test_lake_events_are_newest_first(self) -> None:
        events = [
            {
                "id": f"event-{index:04d}",
                "detail-type": "decision.scored",
                "source": "bfd.scoring",
                "time": f"2026-09-15T09:0{index}:00Z",
                "detail": {"decision_id": f"dec-{index}", "uid": UID},
            }
            for index in range(3)
        ]
        status, body = _call("GET /console/lake", _deps(events=events))
        assert status == 200
        assert [e["subject"] for e in body["events"]] == ["dec-2", "dec-1", "dec-0"]
        assert body["events"][0]["type"] == "decision.scored"


class TestRelease:
    ROUTE = "POST /console/transfers/{uid}/{transfer_id}/release"
    PARAMS = {"uid": UID, "transfer_id": TRANSFER}

    def test_a_transfer_under_review_is_released_as_an_analyst_review(self) -> None:
        sfn = FakeSfn()
        status, body = _call(self.ROUTE, _deps(sfn=sfn), params=self.PARAMS)
        assert (status, body) == (200, {"status": "release_requested"})
        assert sfn.successes[0]["taskToken"] == "secret-task-token"
        output = json.loads(sfn.successes[0]["output"])
        # Never "passkey": an analyst release must not look like a verification to adaptation.
        assert output["method"] == "analyst_review"
        assert output["reviewed_by"] == "analyst-0001"

    def test_a_transfer_not_under_review_is_not_released(self) -> None:
        items = [dict(item) for item in ITEMS]
        items[-1]["status"] = "awaiting_step_up"
        sfn = FakeSfn()
        status, body = _call(self.ROUTE, _deps(FakeTable(items), sfn), params=self.PARAMS)
        assert (status, body["status"]) == (409, "awaiting_step_up")
        assert sfn.successes == []

    def test_an_unknown_transfer_is_not_found(self) -> None:
        params = {"uid": UID, "transfer_id": "b" * 32}
        assert _call(self.ROUTE, _deps(), params=params)[0] == 404

    def test_a_closed_review_window_is_a_conflict(self) -> None:
        sfn = FakeSfn(error=_error("TaskTimedOut"))
        assert _call(self.ROUTE, _deps(sfn=sfn), params=self.PARAMS)[0] == 409

    def test_a_non_analyst_cannot_release(self) -> None:
        sfn = FakeSfn()
        status, _ = _call(self.ROUTE, _deps(sfn=sfn), claims={"sub": "u"}, params=self.PARAMS)
        assert status == 403
        assert sfn.successes == []
