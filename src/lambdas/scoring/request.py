"""Parse and validate one checkpoint request from the browser.

Strict by design. The behavioural part of the payload is matched against an exact set of keys, and
an unknown key is rejected rather than ignored: a client change that starts sending key identity,
typed characters, an account number or an amount inside ``behaviour`` fails here, loudly, instead of
being scored and stored (CLAUDE.md section 2).

Timing is checked for physical plausibility before it reaches a scorer (architecture section 5.1):
negative durations, impossible holds, and intervals that disagree with each other.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, cast

from fraudcore.features import DeviceClass, KeystrokeTiming, device_class
from fraudcore.policy import CHECKPOINTS, Checkpoint

REQUEST_KEYS = frozenset({"session_id", "checkpoint", "device", "behaviour", "transaction"})
OPTIONAL_REQUEST_KEYS = frozenset({"transaction"})
DEVICE_KEYS = frozenset({"device_id", "max_touch_points", "coarse_pointer", "short_side_px"})
BEHAVIOUR_KEYS = frozenset({"fields"})
FIELD_KEYS = frozenset({"hold", "down_down", "up_down", "backspaces", "corrections", "pastes"})
TRANSACTION_KEYS = frozenset({"payee_id", "amount"})

MAX_FIELDS = 16
MAX_KEYSTROKES = 256
# Held longer than this, a key is auto-repeating or stuck rather than being typed.
MAX_HOLD_SECONDS = 5.0
# A longer gap is the user leaving the field; the client splits the entry there.
MAX_INTERVAL_SECONDS = 60.0
# up_down must equal down_down minus hold. The client rounds each to 0.1 ms, so a small tolerance
# absorbs rounding; a larger disagreement was not produced by one clock.
INTERVAL_TOLERANCE_SECONDS = 0.002
MIN_AMOUNT = 0.01
MAX_AMOUNT = 1e9
MAX_TOUCH_POINTS = 32
MAX_SCREEN_PX = 10_000

_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{8,64}")
# Payee accounts are hashed in the browser, so the raw account number never reaches the API.
_PAYEE_ID = re.compile(r"[0-9a-f]{64}")


class RequestError(ValueError):
    """The request is malformed. Reported to the caller as a 400."""


@dataclass(frozen=True)
class Device:
    device_id: str
    device_class: DeviceClass


@dataclass(frozen=True)
class TransactionRequest:
    payee_id: str
    amount: float


@dataclass(frozen=True)
class CheckpointRequest:
    session_id: str
    checkpoint: Checkpoint
    device: Device
    fields: tuple[KeystrokeTiming, ...]
    transaction: TransactionRequest | None


def _object(
    value: Any, name: str, keys: frozenset[str], optional: frozenset[str] = frozenset()
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RequestError(f"{name} must be an object")
    unexpected = sorted(set(value) - keys)
    if unexpected:
        raise RequestError(f"{name} has unexpected keys {unexpected}")
    missing = sorted(keys - optional - set(value))
    if missing:
        raise RequestError(f"{name} is missing {missing}")
    return value


def _number(value: Any, name: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise RequestError(f"{name} must be a number")
    if not (math.isfinite(value) and low <= value <= high):
        raise RequestError(f"{name} must be within [{low}, {high}]")
    return float(value)


def _count(value: Any, name: str, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= high:
        raise RequestError(f"{name} must be an integer within [0, {high}]")
    return value


def _series(value: Any, name: str, low: float, high: float) -> tuple[float, ...]:
    if not isinstance(value, list) or len(value) > MAX_KEYSTROKES:
        raise RequestError(f"{name} must be a list of at most {MAX_KEYSTROKES} numbers")
    return tuple(_number(item, f"{name}[{index}]", low, high) for index, item in enumerate(value))


def _identifier(value: Any, name: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise RequestError(f"{name} is not a valid identifier")
    return value


def _field(value: Any, index: int) -> KeystrokeTiming:
    name = f"behaviour.fields[{index}]"
    raw = _object(value, name, FIELD_KEYS)
    hold = _series(raw["hold"], f"{name}.hold", 0.0, MAX_HOLD_SECONDS)
    down_down = _series(raw["down_down"], f"{name}.down_down", 0.0, MAX_INTERVAL_SECONDS)
    up_down = _series(raw["up_down"], f"{name}.up_down", -MAX_HOLD_SECONDS, MAX_INTERVAL_SECONDS)
    backspaces = _count(raw["backspaces"], f"{name}.backspaces", MAX_KEYSTROKES)
    corrections = _count(raw["corrections"], f"{name}.corrections", MAX_KEYSTROKES)
    pastes = _count(raw["pastes"], f"{name}.pastes", MAX_KEYSTROKES)

    try:
        timing = KeystrokeTiming(
            hold=hold,
            down_down=down_down,
            up_down=up_down,
            backspaces=backspaces,
            corrections=corrections,
            pastes=pastes,
        )
    except ValueError as error:
        raise RequestError(f"{name}: {error}") from error

    for position, (gap, held, flight) in enumerate(zip(down_down, hold, up_down, strict=False)):
        if abs(flight - (gap - held)) > INTERVAL_TOLERANCE_SECONDS:
            raise RequestError(f"{name}.up_down[{position}] disagrees with down_down and hold")
    return timing


def parse(body: str) -> CheckpointRequest:
    try:
        data = json.loads(body)
    except json.JSONDecodeError as error:
        raise RequestError("body must be a JSON object") from error
    raw = _object(data, "request", REQUEST_KEYS, OPTIONAL_REQUEST_KEYS)

    checkpoint = raw["checkpoint"]
    if not isinstance(checkpoint, str) or checkpoint not in CHECKPOINTS:
        raise RequestError(f"checkpoint must be one of {list(CHECKPOINTS)}")

    device = _object(raw["device"], "device", DEVICE_KEYS)
    if not isinstance(device["coarse_pointer"], bool):
        raise RequestError("device.coarse_pointer must be a boolean")

    behaviour = _object(raw["behaviour"], "behaviour", BEHAVIOUR_KEYS)
    fields = behaviour["fields"]
    if not isinstance(fields, list) or len(fields) > MAX_FIELDS:
        raise RequestError(f"behaviour.fields must be a list of at most {MAX_FIELDS} entries")

    transaction = None
    if "transaction" in raw:
        details = _object(raw["transaction"], "transaction", TRANSACTION_KEYS)
        transaction = TransactionRequest(
            payee_id=_identifier(details["payee_id"], "transaction.payee_id", _PAYEE_ID),
            amount=_number(details["amount"], "transaction.amount", MIN_AMOUNT, MAX_AMOUNT),
        )
    if checkpoint == "confirmation" and transaction is None:
        raise RequestError("the confirmation checkpoint requires a transaction")

    return CheckpointRequest(
        session_id=_identifier(raw["session_id"], "session_id", _IDENTIFIER),
        checkpoint=cast(Checkpoint, checkpoint),
        device=Device(
            device_id=_identifier(device["device_id"], "device.device_id", _IDENTIFIER),
            device_class=device_class(
                _count(device["max_touch_points"], "device.max_touch_points", MAX_TOUCH_POINTS),
                device["coarse_pointer"],
                _count(device["short_side_px"], "device.short_side_px", MAX_SCREEN_PX),
            ),
        ),
        fields=tuple(_field(value, index) for index, value in enumerate(fields)),
        transaction=transaction,
    )
