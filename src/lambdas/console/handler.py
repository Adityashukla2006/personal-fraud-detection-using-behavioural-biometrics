"""Operator console API: a read-mostly view across users, for analysts only.

It exists so the system can be shown and exercised end to end: what each checkpoint decided and
why, where each transfer is in the workflow, what the audit lake recorded, and what adaptation has
learned. Every route requires membership of the analyst Cognito group, checked here from the token's
groups claim, so an ordinary signed-in user sees nothing that is not their own.

The only write is releasing a transfer held for review, which replaces the command-line
SendTaskSuccess an analyst needed before. It sends ``method: analyst_review``, so it can never be
mistaken for a passkey verification and never reaches adaptation.

Routes, all behind the JWT authorizer:

    GET  /console/users                                  users in the pool
    GET  /console/users/{uid}                            decisions, transfers, balance, adaptation
    GET  /console/lake                                   the most recent audit-lake events
    POST /console/transfers/{uid}/{transfer_id}/release  release a transfer under review

Task tokens are never returned: holding one is holding the power to release a transfer.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from fraudcore.events import DETAIL_TYPES

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

DECISION_LIMIT = 25
LAKE_EVENT_LIMIT = 40
USER_PAGES = 3

_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{8,64}")
_CLOSED_HOLD = frozenset({"TaskTimedOut", "TaskDoesNotExist", "InvalidToken"})


@dataclass(frozen=True)
class Dependencies:
    table: Any
    cognito: Any
    s3: Any
    sfn: Any
    user_pool_id: str
    lake_bucket: str
    analyst_group: str
    clock: Callable[[], float] = time.time


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"cannot serialise {type(value).__name__}")


def _response(status: int, body: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=_json_default),
    }


def groups(claims: Mapping[str, Any]) -> set[str]:
    """The caller's Cognito groups.

    HTTP API flattens array claims into a string such as ``[analyst admin]``, so both that and a
    real list are accepted.
    """
    raw = claims.get("cognito:groups")
    if raw is None:
        return set()
    if isinstance(raw, list):
        values = raw
    else:
        text = str(raw).strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        values = [part for part in re.split(r"[,\s]+", text) if part]
    return {str(value).strip() for value in values if str(value).strip()}


def handle(event: Mapping[str, Any], deps: Dependencies) -> dict[str, Any]:
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    analyst = claims.get("sub")
    if not (isinstance(analyst, str) and analyst):
        return _response(401, {"error": "unauthenticated"})
    if deps.analyst_group not in groups(claims):
        return _response(403, {"error": "the operator console is for analysts only"})

    route = event.get("routeKey")
    params = event.get("pathParameters") or {}
    try:
        if route == "GET /console/users":
            return _response(200, {"users": list_users(deps)})
        if route == "GET /console/users/{uid}":
            uid = params.get("uid", "")
            if not _valid(uid):
                return _response(404, {"error": "user not found"})
            return _response(200, user_detail(uid, deps))
        if route == "GET /console/lake":
            return _response(200, {"events": recent_events(deps)})
        if route == "POST /console/transfers/{uid}/{transfer_id}/release":
            return release(params.get("uid", ""), params.get("transfer_id", ""), analyst, deps)
        return _response(404, {"error": "route not found"})
    except Exception:
        LOGGER.exception("console request failed")
        return _response(502, {"error": "console service unavailable"})


def _valid(identifier: Any) -> bool:
    return isinstance(identifier, str) and bool(_IDENTIFIER.fullmatch(identifier))


def list_users(deps: Dependencies) -> list[dict[str, Any]]:
    users: list[dict[str, Any]] = []
    token: str | None = None
    for _ in range(USER_PAGES):
        request: dict[str, Any] = {"UserPoolId": deps.user_pool_id, "Limit": 60}
        if token:
            request["PaginationToken"] = token
        page = deps.cognito.list_users(**request)
        for user in page.get("Users", []):
            attributes = {a["Name"]: a["Value"] for a in user.get("Attributes", [])}
            users.append(
                {
                    "uid": attributes.get("sub"),
                    "email": attributes.get("email"),
                    "status": user.get("UserStatus"),
                    "created": user.get("UserCreateDate"),
                }
            )
        token = page.get("PaginationToken")
        if not token:
            break
    return sorted(users, key=lambda user: str(user.get("email") or ""))


def _query_all(table: Any, condition: Any, pages: int = 3) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    request: dict[str, Any] = {"KeyConditionExpression": condition}
    for _ in range(pages):
        page = table.query(**request)
        items += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            break
        request["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    return items


def user_detail(uid: str, deps: Dependencies) -> dict[str, Any]:
    decisions = deps.table.query(
        IndexName="GSI1",
        KeyConditionExpression=Key("GSI1PK").eq(f"USER#{uid}"),
        ScanIndexForward=False,
        Limit=DECISION_LIMIT,
    ).get("Items", [])

    profiles: list[dict[str, Any]] = []
    devices: list[dict[str, Any]] = []
    verifications: list[dict[str, Any]] = []
    buffered: dict[str, int] = {}
    for item in _query_all(deps.table, Key("PK").eq(f"USER#{uid}")):
        sort = item["SK"]
        if sort.startswith("PROFILE#"):
            profiles.append(
                {
                    "device_class": sort.removeprefix("PROFILE#"),
                    "features": item.get("features", []),
                    "mu": item.get("mu", []),
                    "sigma": item.get("sigma", []),
                    "version": item.get("version", 0),
                    "sessions": item.get("n_sessions"),
                    "saturations": item.get("saturations", 0),
                    "anchored_at": item.get("anchor_at"),
                    "last_saturated_at": item.get("last_saturated_at"),
                    "updated_at": item.get("updated_at"),
                    "demo_seed": item.get("demo_seed"),
                }
            )
        elif sort.startswith("DEV#"):
            devices.append(
                {
                    "device_id": sort.removeprefix("DEV#"),
                    "device_class": item.get("device_class"),
                    "sessions": item.get("session_count", 0),
                }
            )
        elif sort.startswith("VERIFIED#"):
            verifications.append(
                {
                    "decision_id": sort.removeprefix("VERIFIED#"),
                    "method": item.get("method"),
                    "trust": item.get("trust"),
                    "admitted": item.get("admitted"),
                    "received_at": item.get("received_at"),
                }
            )
        elif sort.startswith("BUF#"):
            device_class = sort.split("#")[1]
            buffered[device_class] = buffered.get(device_class, 0) + 1

    balance = None
    transfers: list[dict[str, Any]] = []
    for item in _query_all(deps.table, Key("PK").eq(f"LEDGER#{uid}")):
        if item["SK"] == "BALANCE":
            balance = item.get("balance")
        elif item["SK"].startswith("TXN#"):
            transfers.append(
                {
                    "transfer_id": item["SK"].removeprefix("TXN#"),
                    "status": item.get("status"),
                    "reason": item.get("reason"),
                    "amount": item.get("amount"),
                    "action": item.get("action"),
                    "created_at": item.get("created_at"),
                    "closed_at": item.get("closed_at"),
                    "held": "task_token" in item,
                }
            )
    transfers.sort(key=lambda transfer: transfer.get("created_at") or 0, reverse=True)

    return {
        "uid": uid,
        "balance": balance,
        "decisions": [
            {
                "decision_id": item["PK"].removeprefix("DEC#"),
                "created": item.get("GSI1SK", "").removeprefix("TS#"),
                "session_id": item.get("session_id"),
                "checkpoint": item.get("checkpoint"),
                "device_class": item.get("device_class"),
                "action": item.get("action"),
                "risk": item.get("risk"),
                "confidence": item.get("confidence"),
                "contributions": item.get("contributions", []),
                "constraints": item.get("constraints", []),
                "scores": item.get("scores", {}),
            }
            for item in decisions
        ],
        "transfers": transfers,
        "adaptation": {
            "profiles": profiles,
            "devices": devices,
            "verifications": sorted(
                verifications, key=lambda v: v.get("received_at") or 0, reverse=True
            ),
            "buffered": buffered,
        },
    }


def recent_events(deps: Dependencies, limit: int = LAKE_EVENT_LIMIT) -> list[dict[str, Any]]:
    today = datetime.fromtimestamp(deps.clock(), UTC).date()
    listed: list[dict[str, Any]] = []
    for detail_type in sorted(DETAIL_TYPES):
        for day in (today, today - timedelta(days=1)):
            response = deps.s3.list_objects_v2(
                Bucket=deps.lake_bucket, Prefix=f"events/type={detail_type}/dt={day:%Y-%m-%d}/"
            )
            listed += response.get("Contents", [])

    newest = sorted(listed, key=lambda item: item["LastModified"], reverse=True)[:limit]

    def fetch(summary: Mapping[str, Any]) -> dict[str, Any]:
        body = deps.s3.get_object(Bucket=deps.lake_bucket, Key=summary["Key"])["Body"].read()
        event = json.loads(body)
        detail = event.get("detail", {})
        return {
            "key": summary["Key"],
            "time": event.get("time"),
            "type": event.get("detail-type"),
            "source": event.get("source"),
            "subject": detail.get("decision_id") or detail.get("transfer_id"),
            "uid": detail.get("uid"),
            "detail": detail,
        }

    with ThreadPoolExecutor(max_workers=8) as pool:
        return list(pool.map(fetch, newest))


def release(uid: str, transfer_id: str, analyst: str, deps: Dependencies) -> dict[str, Any]:
    if not (_valid(uid) and _valid(transfer_id)):
        return _response(404, {"error": "transfer not found"})
    item = deps.table.get_item(
        Key={"PK": f"LEDGER#{uid}", "SK": f"TXN#{transfer_id}"}, ConsistentRead=True
    ).get("Item")
    if item is None:
        return _response(404, {"error": "transfer not found"})
    if item.get("status") != "under_review" or not item.get("task_token"):
        return _response(
            409, {"error": "transfer is not held for review", "status": item.get("status")}
        )
    try:
        deps.sfn.send_task_success(
            taskToken=item["task_token"],
            output=json.dumps(
                {
                    "reviewed": True,
                    "method": "analyst_review",
                    "reviewed_by": analyst,
                    "reviewed_at": int(deps.clock()),
                }
            ),
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in _CLOSED_HOLD:
            return _response(409, {"error": "the review window has closed"})
        raise
    LOGGER.info("analyst %s released transfer %s for %s", analyst, transfer_id, uid)
    return _response(200, {"status": "release_requested"})


_dependencies: Dependencies | None = None


def lambda_handler(event: Mapping[str, Any], context: Any) -> dict[str, Any]:
    global _dependencies
    if _dependencies is None:
        import boto3

        _dependencies = Dependencies(
            table=boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"]),
            cognito=boto3.client("cognito-idp"),
            s3=boto3.client("s3"),
            sfn=boto3.client("stepfunctions"),
            user_pool_id=os.environ["USER_POOL_ID"],
            lake_bucket=os.environ["LAKE_BUCKET"],
            analyst_group=os.environ["ANALYST_GROUP"],
        )
    return handle(event, _dependencies)
