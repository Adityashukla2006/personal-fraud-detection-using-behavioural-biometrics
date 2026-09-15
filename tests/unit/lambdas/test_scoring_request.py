"""Tests for checkpoint request parsing.

The exact key sets are CLAUDE.md section 2 guards: no key identity, typed content, account number
or amount may enter the behavioural payload, and a client that starts sending one must be refused.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from scoring import request as request_module
from scoring.request import RequestError, parse

PAYEE = "a" * 64


def _field() -> dict[str, Any]:
    hold = [0.09, 0.11, 0.08, 0.10]
    down_down = [0.21, 0.35, 0.18]
    return {
        "hold": hold,
        "down_down": down_down,
        "up_down": [round(dd - h, 4) for dd, h in zip(down_down, hold, strict=False)],
        "backspaces": 1,
        "corrections": 1,
        "pastes": 0,
    }


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "session_id": "session-0001",
        "checkpoint": "login",
        "device": {
            "device_id": "device-0001",
            "max_touch_points": 0,
            "coarse_pointer": False,
            "short_side_px": 1080,
        },
        "behaviour": {"fields": [_field()]},
    }
    payload.update(overrides)
    return payload


def _parse(payload: dict[str, Any]) -> request_module.CheckpointRequest:
    return parse(json.dumps(payload))


class TestValidRequests:
    def test_a_login_checkpoint_parses_into_fraudcore_types(self) -> None:
        parsed = _parse(_payload())
        assert parsed.checkpoint == "login"
        assert parsed.device.device_class == "desktop"
        assert parsed.fields[0].hold == (0.09, 0.11, 0.08, 0.10)
        assert parsed.transaction is None

    def test_a_confirmation_carries_its_transaction(self) -> None:
        payload = _payload(
            checkpoint="confirmation", transaction={"payee_id": PAYEE, "amount": 250}
        )
        assert _parse(payload).transaction == request_module.TransactionRequest(PAYEE, 250.0)

    def test_the_maximum_field_count_is_accepted(self) -> None:
        payload = _payload(behaviour={"fields": [_field()] * request_module.MAX_FIELDS})
        assert len(_parse(payload).fields) == request_module.MAX_FIELDS


class TestNoContentInTheBehaviouralPayload:
    def test_the_behavioural_key_sets_are_pinned(self) -> None:
        behaviour_keys, field_keys = request_module.BEHAVIOUR_KEYS, request_module.FIELD_KEYS
        assert behaviour_keys == {"fields"}
        assert field_keys == {"hold", "down_down", "up_down", "backspaces", "corrections", "pastes"}

    @pytest.mark.parametrize(
        "content_key", ["key", "keys", "code", "char", "text", "value", "account", "amount"]
    )
    def test_a_field_carrying_content_is_rejected(self, content_key: str) -> None:
        field = _field() | {content_key: "h"}
        with pytest.raises(RequestError, match="unexpected keys"):
            _parse(_payload(behaviour={"fields": [field]}))

    def test_content_beside_the_fields_is_rejected(self) -> None:
        with pytest.raises(RequestError, match="unexpected keys"):
            _parse(_payload(behaviour={"fields": [_field()], "text": "hunter2"}))

    def test_a_raw_account_number_is_not_a_payee_id(self) -> None:
        payload = _payload(
            checkpoint="confirmation",
            transaction={"payee_id": "4111111111111111", "amount": 10},
        )
        with pytest.raises(RequestError, match="payee_id"):
            _parse(payload)


class TestPlausibility:
    def _with_field(self, **changes: Any) -> dict[str, Any]:
        field = _field() | changes
        return _payload(behaviour={"fields": [field]})

    def test_a_hold_exactly_at_the_maximum_is_accepted(self) -> None:
        hold = [0.09, 0.11, 0.08, request_module.MAX_HOLD_SECONDS]
        _parse(self._with_field(hold=hold))

    def test_a_hold_above_the_maximum_is_rejected(self) -> None:
        hold = [0.09, 0.11, 0.08, request_module.MAX_HOLD_SECONDS + 0.001]
        with pytest.raises(RequestError, match="hold"):
            _parse(self._with_field(hold=hold))

    def test_a_negative_interval_is_rejected(self) -> None:
        with pytest.raises(RequestError, match="down_down"):
            _parse(self._with_field(down_down=[0.21, -0.1, 0.18]))

    def test_intervals_within_rounding_tolerance_are_accepted(self) -> None:
        field = _field()
        field["up_down"][0] += request_module.INTERVAL_TOLERANCE_SECONDS * 0.9
        _parse(_payload(behaviour={"fields": [field]}))

    def test_intervals_that_disagree_are_rejected(self) -> None:
        field = _field()
        field["up_down"][0] += 0.05
        with pytest.raises(RequestError, match="disagrees"):
            _parse(_payload(behaviour={"fields": [field]}))

    def test_two_keystrokes_are_too_few(self) -> None:
        field = {"hold": [0.1, 0.1], "down_down": [0.2], "up_down": [0.1]}
        with pytest.raises(RequestError, match="at least 3"):
            _parse(self._with_field(**field))


class TestShape:
    def test_one_field_too_many_is_rejected(self) -> None:
        payload = _payload(behaviour={"fields": [_field()] * (request_module.MAX_FIELDS + 1)})
        with pytest.raises(RequestError, match="at most"):
            _parse(payload)

    def test_confirmation_without_a_transaction_is_rejected(self) -> None:
        with pytest.raises(RequestError, match="requires a transaction"):
            _parse(_payload(checkpoint="confirmation"))

    def test_an_unknown_checkpoint_is_rejected(self) -> None:
        with pytest.raises(RequestError, match="checkpoint"):
            _parse(_payload(checkpoint="checkout"))

    def test_a_boolean_is_not_a_number(self) -> None:
        payload = _payload()
        payload = copy.deepcopy(payload)
        payload["device"]["max_touch_points"] = True
        with pytest.raises(RequestError, match="max_touch_points"):
            _parse(payload)

    def test_malformed_json_is_rejected(self) -> None:
        with pytest.raises(RequestError, match="JSON"):
            parse("{not json")

    def test_a_missing_key_is_named(self) -> None:
        payload = _payload()
        del payload["device"]
        with pytest.raises(RequestError, match="device"):
            _parse(payload)
