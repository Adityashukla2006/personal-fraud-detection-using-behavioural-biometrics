"""DynamoDB access for the scoring path: one BatchGetItem in, two PutItems out.

Item shapes follow architecture section 6. This module only translates between DynamoDB items and
fraudcore types; no scoring decision is made here.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer

from fraudcore.features import (
    DeviceRecord,
    KeystrokeTiming,
    PayeeEdge,
    PayeeRisk,
    SessionContext,
    UserAggregates,
)
from fraudcore.policy import Decision
from fraudcore.scoring import DISPERSION_FLOOR, ChannelScore, ReferenceProfile
from scoring.request import CheckpointRequest

SECONDS_PER_HOUR = 3_600
SECONDS_PER_DAY = 86_400
SESSION_TTL_SECONDS = SECONDS_PER_DAY
DECISION_TTL_SECONDS = 30 * SECONDS_PER_DAY
AGGREGATE_WINDOW = "30d"

# Oldest fields are dropped beyond this, keeping a session item far below DynamoDB's 400 KB limit.
SESSION_MAX_FIELDS = 24

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


class StoreError(Exception):
    """Reading or writing state failed. The handler fails open on it."""


class ForeignSessionError(Exception):
    """The session id already belongs to a different user."""


@dataclass(frozen=True)
class ScoringState:
    profile: ReferenceProfile | None
    profile_sessions: int
    context: SessionContext
    edge: PayeeEdge | None
    risk: PayeeRisk | None
    aggregates: UserAggregates | None
    session_fields: tuple[KeystrokeTiming, ...]


def _key(partition: str, sort: str) -> dict[str, dict[str, str]]:
    return {"PK": {"S": partition}, "SK": {"S": sort}}


def _serialize(item: Mapping[str, Any]) -> dict[str, Any]:
    return {name: _serializer.serialize(_to_dynamo(value)) for name, value in item.items()}


def _to_dynamo(value: Any) -> Any:
    # DynamoDB has no float type. repr of a rounded float is an exact decimal string, so the
    # conversion never trips the serializer's inexact-rounding trap.
    if isinstance(value, float):
        return Decimal(repr(round(value, 6)))
    if isinstance(value, Mapping):
        return {name: _to_dynamo(item) for name, item in value.items()}
    if isinstance(value, list | tuple):
        return [_to_dynamo(item) for item in value]
    return value


def _elapsed(timestamp: Any, now: float, unit: float) -> float:
    # Clamped at zero: a timestamp slightly in the future is clock skew, not negative age.
    return max(0.0, (now - float(timestamp)) / unit)


def _timing(item: Mapping[str, Any]) -> KeystrokeTiming:
    return KeystrokeTiming(
        hold=tuple(float(value) for value in item["hold"]),
        down_down=tuple(float(value) for value in item["down_down"]),
        up_down=tuple(float(value) for value in item["up_down"]),
        backspaces=int(item["backspaces"]),
        corrections=int(item["corrections"]),
        pastes=int(item["pastes"]),
    )


def _timing_item(timing: KeystrokeTiming) -> dict[str, Any]:
    return {
        "hold": list(timing.hold),
        "down_down": list(timing.down_down),
        "up_down": list(timing.up_down),
        "backspaces": timing.backspaces,
        "corrections": timing.corrections,
        "pastes": timing.pastes,
    }


class Store:
    def __init__(self, client: Any, table_name: str) -> None:
        self._client = client
        self._table = table_name

    def load(self, uid: str, request: CheckpointRequest, now: float) -> ScoringState:
        user = f"USER#{uid}"
        keys = {
            "profile": (user, f"PROFILE#{request.device.device_class}"),
            "device": (user, f"DEV#{request.device.device_id}"),
            "account": (user, "ACCOUNT"),
            "aggregates": (f"AGG#{uid}", f"WINDOW#{AGGREGATE_WINDOW}"),
            "session": (f"SESS#{request.session_id}", "META"),
        }
        if request.transaction is not None:
            payee = request.transaction.payee_id
            keys["edge"] = (user, f"PAYEE#{payee}")
            keys["risk"] = (f"PAYEE#{payee}", "RISK")

        items = self._batch_get(list(keys.values()))
        found = {name: items.get(key) for name, key in keys.items()}
        try:
            return self._decode(uid, found, now)
        except (KeyError, TypeError, ValueError) as error:
            raise StoreError(f"malformed item: {error!r}") from error

    def _batch_get(self, keys: list[tuple[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
        pending: dict[str, Any] = {
            self._table: {"Keys": [_key(*key) for key in keys], "ConsistentRead": True}
        }
        items: dict[tuple[str, str], dict[str, Any]] = {}
        # One immediate retry. Unprocessed keys at this volume mean throttling, and a second wait
        # would spend the latency budget; failing open is the better answer.
        for _ in range(2):
            try:
                response = self._client.batch_get_item(RequestItems=pending)
            except Exception as error:
                raise StoreError(f"batch_get_item failed: {error!r}") from error
            for raw in response.get("Responses", {}).get(self._table, []):
                item = {name: _deserializer.deserialize(value) for name, value in raw.items()}
                items[(item["PK"], item["SK"])] = item
            pending = response.get("UnprocessedKeys") or {}
            if not pending:
                return items
        raise StoreError("keys remained unprocessed after a retry")

    def _decode(
        self, uid: str, found: Mapping[str, dict[str, Any] | None], now: float
    ) -> ScoringState:
        profile, profile_sessions = None, 0
        if item := found["profile"]:
            names = tuple(str(name) for name in item["features"])
            centre = tuple(float(value) for value in item["mu"])
            dispersion = tuple(max(float(value), DISPERSION_FLOOR) for value in item["sigma"])
            if not len(names) == len(centre) == len(dispersion):
                raise ValueError("profile vectors differ in length")
            profile = ReferenceProfile(names, centre, dispersion, "mad", 0)
            profile_sessions = int(item["n_sessions"])

        device = DeviceRecord(int(item["session_count"])) if (item := found["device"]) else None
        account = found["account"] or {}
        context = SessionContext(
            device=device,
            days_since_credential_change=(
                _elapsed(account["credential_changed_at"], now, SECONDS_PER_DAY)
                if "credential_changed_at" in account
                else None
            ),
            days_since_contact_change=(
                _elapsed(account["contact_changed_at"], now, SECONDS_PER_DAY)
                if "contact_changed_at" in account
                else None
            ),
            account_sessions=int(account.get("session_count", 0)),
        )

        edge = None
        if item := found.get("edge"):
            edge = PayeeEdge(
                days_since_first_seen=_elapsed(item["first_seen"], now, SECONDS_PER_DAY),
                verified="verified_at" in item,
            )

        risk = None
        if item := found.get("risk"):
            risk = PayeeRisk(
                risk_score=min(1.0, max(0.0, float(item["risk_score"]))),
                hours_since_computed=_elapsed(item["computed_at"], now, SECONDS_PER_HOUR),
                flagged=bool(item.get("flagged", False)),
            )

        aggregates = None
        if item := found["aggregates"]:
            aggregates = UserAggregates(
                amount_p50=float(item["amount_p50"]),
                amount_p95=float(item["amount_p95"]),
                daily_count_p95=float(item["daily_count_p95"]),
                hour_histogram=tuple(float(count) for count in item["hour_histogram"]),
                history_count=int(item["history_count"]),
            )

        session_fields: tuple[KeystrokeTiming, ...] = ()
        if item := found["session"]:
            if item["uid"] != uid:
                raise ForeignSessionError(item["PK"])
            session_fields = tuple(_timing(field) for field in item["fields"])

        return ScoringState(
            profile=profile,
            profile_sessions=profile_sessions,
            context=context,
            edge=edge,
            risk=risk,
            aggregates=aggregates,
            session_fields=session_fields,
        )

    def save_session(
        self, uid: str, session_id: str, fields: tuple[KeystrokeTiming, ...], now: float
    ) -> None:
        item = {
            "PK": f"SESS#{session_id}",
            "SK": "META",
            "uid": uid,
            "fields": [_timing_item(field) for field in fields[-SESSION_MAX_FIELDS:]],
            "updated_at": int(now),
            "ttl": int(now) + SESSION_TTL_SECONDS,
        }
        self._put(
            item,
            # A session id is chosen by the client, so claiming one another user already holds must
            # fail rather than overwrite their evidence.
            ConditionExpression="attribute_not_exists(PK) OR #uid = :uid",
            ExpressionAttributeNames={"#uid": "uid"},
            ExpressionAttributeValues={":uid": {"S": uid}},
        )

    def save_decision(
        self,
        uid: str,
        decision_id: str,
        request: CheckpointRequest,
        scores: Mapping[str, ChannelScore],
        decision: Decision,
        now: float,
    ) -> None:
        timestamp = datetime.fromtimestamp(now, UTC).isoformat(timespec="milliseconds")
        item = {
            "PK": f"DEC#{decision_id}",
            "SK": "META",
            "uid": uid,
            "session_id": request.session_id,
            "checkpoint": request.checkpoint,
            "device_class": request.device.device_class,
            "action": decision.action,
            "risk": decision.risk,
            "confidence": decision.confidence,
            "contributions": [
                {"channel": contribution.channel, "value": contribution.value}
                for contribution in decision.contributions
            ],
            "constraints": list(decision.constraints),
            "scores": {
                name: {"score": score.score, "confidence": score.confidence}
                for name, score in scores.items()
            },
            "GSI1PK": f"USER#{uid}",
            "GSI1SK": f"TS#{timestamp}",
            "created_at": int(now),
            "ttl": int(now) + DECISION_TTL_SECONDS,
        }
        self._put(item)

    def _put(self, item: Mapping[str, Any], **conditions: Any) -> None:
        try:
            self._client.put_item(TableName=self._table, Item=_serialize(item), **conditions)
        except Exception as error:
            raise StoreError(f"put_item failed: {error!r}") from error
