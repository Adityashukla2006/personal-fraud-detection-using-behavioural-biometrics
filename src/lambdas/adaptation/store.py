"""DynamoDB access for adaptation. Item shapes follow architecture section 6.

The claim is the idempotency seam. Recording the verification, counting the device session and
admitting the session to the buffer happen in one transaction conditioned on the verification not
existing yet, so a redelivered event can never count a device twice or admit a session twice. Only
the profile rebuild after it may repeat, and a rebuild from the same buffer is safe to repeat.

    USER#<uid> / VERIFIED#<decision_id>          trust, admitted, method, transfer_id, TTL 180 days
    USER#<uid> / DEV#<device_id>                 session_count, device_class, first_seen
    USER#<uid> / BUF#<class>#<ms>#<decision_id>  features, trust, verified, TTL 180 days
    USER#<uid> / PROFILE#<class>                 features, mu, sigma, anchor_*, saturations, version
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from fraudcore.adaptation import PROFILE_FEATURES, BufferedSession, Profile
from fraudcore.events import StepUpVerified
from fraudcore.features import KeystrokeTiming, vector
from fraudcore.scoring import ChannelScore
from fraudcore.session import pooled_extract

SECONDS_PER_DAY = 86_400
RETENTION_SECONDS = 180 * SECONDS_PER_DAY

_serializer = TypeSerializer()
_deserializer = TypeDeserializer()


def _to_dynamo(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(repr(round(value, 9)))
    if isinstance(value, Mapping):
        return {name: _to_dynamo(item) for name, item in value.items()}
    if isinstance(value, list | tuple):
        return [_to_dynamo(item) for item in value]
    return value


def _serialize(item: Mapping[str, Any]) -> dict[str, Any]:
    return {name: _serializer.serialize(_to_dynamo(value)) for name, value in item.items()}


def _deserialize(item: Mapping[str, Any]) -> dict[str, Any]:
    return {name: _deserializer.deserialize(value) for name, value in item.items()}


def _key(partition: str, sort: str) -> dict[str, dict[str, str]]:
    return {"PK": {"S": partition}, "SK": {"S": sort}}


def _floats(values: Any) -> tuple[float, ...]:
    return tuple(float(value) for value in values)


@dataclass(frozen=True)
class DecisionRecord:
    uid: str
    session_id: str
    device_class: str
    device_id: str
    action: str
    scores: dict[str, ChannelScore]


@dataclass(frozen=True)
class UserContext:
    profile: Profile | None
    profile_version: int | None
    """The stored item's version, or None when there is no profile item at all."""
    device_sessions: int | None
    days_since_credential_change: float | None
    days_since_contact_change: float | None


@dataclass(frozen=True)
class Claim:
    trust: float
    admitted: bool


def decode_profile(item: Mapping[str, Any]) -> Profile:
    if tuple(item["features"]) != PROFILE_FEATURES:
        raise ValueError("stored profile uses a different feature set")
    centre, scale = _floats(item["mu"]), _floats(item["sigma"])
    return Profile(
        centre=centre,
        scale=scale,
        # A profile written before adaptation existed has no anchor: its own position is the anchor.
        anchor_centre=_floats(item.get("anchor_mu", centre)),
        anchor_scale=_floats(item.get("anchor_sigma", scale)),
        anchored_at=float(item.get("anchor_at", 0)),
        last_saturated_at=(
            float(item["last_saturated_at"]) if item.get("last_saturated_at") is not None else None
        ),
        saturations=int(item.get("saturations", 0)),
        version=int(item.get("version", 0)),
    )


class AdaptationStore:
    def __init__(self, client: Any, table: str) -> None:
        self._client = client
        self._table = table

    def _get(self, partition: str, sort: str) -> dict[str, Any] | None:
        item = self._client.get_item(
            TableName=self._table, Key=_key(partition, sort), ConsistentRead=True
        ).get("Item")
        return None if item is None else _deserialize(item)

    def decision(self, decision_id: str) -> DecisionRecord | None:
        item = self._get(f"DEC#{decision_id}", "META")
        if item is None or "device_id" not in item:
            return None
        return DecisionRecord(
            uid=item["uid"],
            session_id=item["session_id"],
            device_class=item["device_class"],
            device_id=item["device_id"],
            action=item["action"],
            scores={
                name: ChannelScore(float(value["score"]), float(value["confidence"]))
                for name, value in item.get("scores", {}).items()
            },
        )

    def session_features(self, session_id: str, uid: str) -> tuple[float, ...] | None:
        """The session's pooled keystroke features, or None if it expired or has no typing."""
        item = self._get(f"SESS#{session_id}", "META")
        if item is None or item.get("uid") != uid or not item.get("fields"):
            return None
        fields = [
            KeystrokeTiming(
                hold=_floats(field["hold"]),
                down_down=_floats(field["down_down"]),
                up_down=_floats(field["up_down"]),
                backspaces=int(field["backspaces"]),
                corrections=int(field["corrections"]),
                pastes=int(field["pastes"]),
            )
            for field in item["fields"]
        ]
        return tuple(vector(pooled_extract(fields), PROFILE_FEATURES))

    def context(self, uid: str, device_class: str, device_id: str, now: float) -> UserContext:
        user = f"USER#{uid}"
        keys = [
            (user, f"PROFILE#{device_class}"),
            (user, f"DEV#{device_id}"),
            (user, "ACCOUNT"),
        ]
        response = self._client.batch_get_item(
            RequestItems={
                self._table: {"Keys": [_key(*key) for key in keys], "ConsistentRead": True}
            }
        )
        if response.get("UnprocessedKeys"):
            raise RuntimeError("context read left keys unprocessed")
        found = {
            (item["PK"], item["SK"]): item
            for item in map(_deserialize, response.get("Responses", {}).get(self._table, []))
        }
        profile_item = found.get(keys[0])
        device_item = found.get(keys[1])
        account = found.get(keys[2]) or {}

        def days(name: str) -> float | None:
            if name not in account:
                return None
            return max(0.0, (now - float(account[name])) / SECONDS_PER_DAY)

        return UserContext(
            profile=None if profile_item is None else decode_profile(profile_item),
            profile_version=None if profile_item is None else int(profile_item.get("version", 0)),
            device_sessions=None if device_item is None else int(device_item["session_count"]),
            days_since_credential_change=days("credential_changed_at"),
            days_since_contact_change=days("contact_changed_at"),
        )

    def verification(self, uid: str, decision_id: str) -> Claim | None:
        item = self._get(f"USER#{uid}", f"VERIFIED#{decision_id}")
        if item is None:
            return None
        # Records from before trust was computed carry none, and were never admitted.
        return Claim(float(item.get("trust", 0.0)), bool(item.get("admitted", False)))

    def claim(
        self,
        verified: StepUpVerified,
        decision: DecisionRecord,
        tau: float,
        features: tuple[float, ...] | None,
        now: float,
    ) -> Claim | None:
        """Record, count and admit atomically. None means the verification was already claimed."""
        user = f"USER#{verified.uid}"
        admitted = features is not None
        record: dict[str, Any] = {
            "PK": user,
            "SK": f"VERIFIED#{verified.decision_id}",
            "transfer_id": verified.transfer_id,
            "method": verified.method,
            "trust": tau,
            "admitted": admitted,
            "received_at": int(now),
            "ttl": int(now) + RETENTION_SECONDS,
        }
        if verified.verified_at is not None:
            record["verified_at"] = verified.verified_at

        items: list[dict[str, Any]] = [
            {
                "Put": {
                    "TableName": self._table,
                    "Item": _serialize(record),
                    "ConditionExpression": "attribute_not_exists(PK)",
                }
            },
            {
                "Update": {
                    "TableName": self._table,
                    "Key": _key(user, f"DEV#{decision.device_id}"),
                    "UpdateExpression": (
                        "ADD session_count :one "
                        "SET device_class = :class, first_seen = if_not_exists(first_seen, :now)"
                    ),
                    "ExpressionAttributeValues": {
                        ":one": {"N": "1"},
                        ":class": {"S": decision.device_class},
                        ":now": {"N": str(int(now))},
                    },
                }
            },
        ]
        if features is not None:
            items.append(
                {
                    "Put": {
                        "TableName": self._table,
                        "Item": _serialize(
                            {
                                "PK": user,
                                "SK": (
                                    f"BUF#{decision.device_class}#{int(now * 1000):013d}"
                                    f"#{verified.decision_id}"
                                ),
                                "features": list(features),
                                "trust": tau,
                                "verified": True,
                                "ttl": int(now) + RETENTION_SECONDS,
                            }
                        ),
                    }
                }
            )

        try:
            self._client.transact_write_items(TransactItems=items)
        except ClientError as error:
            reasons = [r.get("Code") for r in error.response.get("CancellationReasons", [])]
            if error.response.get("Error", {}).get("Code") == "TransactionCanceledException" and (
                reasons and reasons[0] == "ConditionalCheckFailed"
            ):
                return None
            raise
        return Claim(tau, admitted)

    def buffer(self, uid: str, device_class: str, capacity: int) -> list[BufferedSession]:
        """The newest ``capacity`` admitted sessions, oldest first."""
        response = self._client.query(
            TableName=self._table,
            KeyConditionExpression="PK = :pk AND begins_with(SK, :prefix)",
            ExpressionAttributeValues={
                ":pk": {"S": f"USER#{uid}"},
                ":prefix": {"S": f"BUF#{device_class}#"},
            },
            ScanIndexForward=False,
            Limit=capacity,
            ConsistentRead=True,
        )
        sessions = [
            BufferedSession(_floats(item["features"]), float(item["trust"]))
            for item in map(_deserialize, response.get("Items", []))
        ]
        return list(reversed(sessions))

    def save_profile(
        self,
        uid: str,
        device_class: str,
        profile: Profile,
        sessions: int,
        expected_version: int | None,
        now: float,
    ) -> None:
        """Write under optimistic concurrency: a concurrent rebuild fails rather than vanishing."""
        item = {
            "PK": f"USER#{uid}",
            "SK": f"PROFILE#{device_class}",
            "features": list(PROFILE_FEATURES),
            "mu": list(profile.centre),
            "sigma": list(profile.scale),
            "anchor_mu": list(profile.anchor_centre),
            "anchor_sigma": list(profile.anchor_scale),
            "anchor_at": profile.anchored_at,
            "last_saturated_at": profile.last_saturated_at,
            "saturations": profile.saturations,
            "n_sessions": sessions,
            "version": profile.version,
            "updated_at": int(now),
        }
        if expected_version is None:
            conditions: dict[str, Any] = {"ConditionExpression": "attribute_not_exists(PK)"}
        elif expected_version == 0:
            conditions = {
                "ConditionExpression": "attribute_not_exists(version) OR version = :v",
                "ExpressionAttributeValues": {":v": {"N": "0"}},
            }
        else:
            conditions = {
                "ConditionExpression": "version = :v",
                "ExpressionAttributeValues": {":v": {"N": str(expected_version)}},
            }
        self._client.put_item(TableName=self._table, Item=_serialize(item), **conditions)
