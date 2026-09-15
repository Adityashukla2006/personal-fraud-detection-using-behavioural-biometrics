"""Tests for transfer status and server-verified passkey step-up, with fakes for AWS.

The security property: a held transfer is released only after Cognito accepts an answer to a
WebAuthn challenge for the signed-in user. Every failure path below must leave the task token
unused.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from botocore.exceptions import ClientError

from transfers import handler
from transfers.handler import Dependencies, handle

TRANSFER_ID = "a" * 32
OPTIONS = {"challenge": "abc", "rpId": "example.cloudfront.net"}


def _error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "Operation")  # type: ignore[arg-type]


class FakeStore:
    def __init__(self, transfer: dict[str, Any] | None, balance: Decimal | None = None) -> None:
        self.transfer = transfer
        self._balance = balance
        self.lookups: list[tuple[str, str]] = []

    def get(self, uid: str, transfer_id: str) -> dict[str, Any] | None:
        self.lookups.append((uid, transfer_id))
        return self.transfer

    def balance(self, uid: str) -> Decimal | None:
        return self._balance


class FakeCognito:
    def __init__(
        self,
        challenge: str = "WEB_AUTHN",
        result: dict[str, Any] | None = None,
        error: ClientError | None = None,
    ) -> None:
        self.challenge = challenge
        self.result = {"AuthenticationResult": {"RefreshToken": "r"}} if result is None else result
        self.error = error
        self.initiated: list[dict[str, Any]] = []
        self.responded: list[dict[str, Any]] = []
        self.revoked: list[str] = []

    def initiate_auth(self, **kwargs: Any) -> dict[str, Any]:
        self.initiated.append(kwargs)
        return {
            "ChallengeName": self.challenge,
            "Session": "cognito-session",
            "ChallengeParameters": {"CREDENTIAL_REQUEST_OPTIONS": json.dumps(OPTIONS)},
        }

    def respond_to_auth_challenge(self, **kwargs: Any) -> dict[str, Any]:
        self.responded.append(kwargs)
        if self.error:
            raise self.error
        return self.result

    def revoke_token(self, **kwargs: Any) -> None:
        self.revoked.append(kwargs["Token"])


class FakeSfn:
    def __init__(self, error: ClientError | None = None) -> None:
        self.error = error
        self.successes: list[dict[str, Any]] = []

    def send_task_success(self, **kwargs: Any) -> None:
        if self.error:
            raise self.error
        self.successes.append(kwargs)


def _held(status: str = "awaiting_step_up") -> dict[str, Any]:
    return {"status": status, "amount": Decimal("250.00"), "task_token": "task-token-1"}


def _event(route: str, body: Any = None, claims: dict[str, str] | None = None) -> dict[str, Any]:
    if claims is None:
        claims = {"sub": "user-1", "username": "user-1"}
    return {
        "routeKey": route,
        "pathParameters": {"transfer_id": TRANSFER_ID},
        "requestContext": {"authorizer": {"jwt": {"claims": claims}}},
        "body": None if body is None else json.dumps(body),
    }


START = "POST /transfers/{transfer_id}/stepup"
VERIFY = "POST /transfers/{transfer_id}/stepup/verify"
STATUS = "GET /transfers/{transfer_id}"
PROOF = {"session": "cognito-session", "credential": {"id": "cred", "response": {}}}


def _call(
    event: dict[str, Any],
    store: FakeStore,
    cognito: FakeCognito | None = None,
    sfn: FakeSfn | None = None,
) -> tuple[int, dict[str, Any]]:
    deps = Dependencies(
        store=store,
        cognito=cognito or FakeCognito(),
        sfn=sfn or FakeSfn(),
        client_id="client-1",
        clock=lambda: 1_767_225_600.0,
    )
    response = handle(event, deps)
    return response["statusCode"], json.loads(response["body"])


class TestAccess:
    def test_missing_claims_are_unauthenticated(self) -> None:
        assert _call(_event(STATUS, claims={}), FakeStore(_held()))[0] == 401

    def test_an_unknown_transfer_is_not_found(self) -> None:
        assert _call(_event(STATUS), FakeStore(None))[0] == 404

    def test_transfers_are_looked_up_in_the_callers_own_partition(self) -> None:
        store = FakeStore(_held())
        _call(_event(STATUS), store)
        assert store.lookups == [("user-1", TRANSFER_ID)]

    def test_a_malformed_transfer_id_is_not_found(self) -> None:
        event = _event(STATUS)
        event["pathParameters"]["transfer_id"] = "../../BALANCE"
        assert _call(event, FakeStore(_held()))[0] == 404


class TestStatus:
    def test_status_reports_amount_and_balance(self) -> None:
        status, body = _call(_event(STATUS), FakeStore(_held("released"), Decimal("99749.50")))
        assert status == 200
        assert body == {
            "transfer_id": TRANSFER_ID,
            "status": "released",
            "reason": None,
            "amount": 250.0,
            "balance": 99749.5,
        }
        assert "task_token" not in body


class TestStart:
    def test_start_returns_a_webauthn_challenge_for_the_signed_in_user(self) -> None:
        cognito = FakeCognito()
        status, body = _call(_event(START), FakeStore(_held()), cognito)
        assert status == 200
        assert body == {"session": "cognito-session", "options": OPTIONS}
        assert cognito.initiated[0]["AuthParameters"] == {
            "USERNAME": "user-1",
            "PREFERRED_CHALLENGE": "WEB_AUTHN",
        }

    def test_no_passkey_means_no_step_up(self) -> None:
        status, body = _call(_event(START), FakeStore(_held()), FakeCognito(challenge="PASSWORD"))
        assert status == 409
        assert "no passkey" in body["error"]

    def test_a_transfer_not_awaiting_step_up_cannot_start_one(self) -> None:
        status, body = _call(_event(START), FakeStore(_held("pending")))
        assert (status, body["status"]) == (409, "pending")


class TestVerify:
    def test_a_verified_passkey_releases_the_hold(self) -> None:
        cognito, sfn = FakeCognito(), FakeSfn()
        status, body = _call(_event(VERIFY, PROOF), FakeStore(_held()), cognito, sfn)

        assert (status, body) == (200, {"status": "verified"})
        assert cognito.responded[0]["ChallengeName"] == "WEB_AUTHN"
        assert cognito.responded[0]["ChallengeResponses"]["USERNAME"] == "user-1"
        assert sfn.successes[0]["taskToken"] == "task-token-1"
        assert json.loads(sfn.successes[0]["output"])["method"] == "passkey"
        assert cognito.revoked == ["r"]

    def test_a_rejected_passkey_never_releases(self) -> None:
        sfn = FakeSfn()
        cognito = FakeCognito(error=_error("NotAuthorizedException"))
        status, _ = _call(_event(VERIFY, PROOF), FakeStore(_held()), cognito, sfn)
        assert status == 403
        assert sfn.successes == []

    def test_an_answer_without_tokens_never_releases(self) -> None:
        sfn = FakeSfn()
        cognito = FakeCognito(result={"ChallengeName": "PASSWORD", "Session": "s"})
        status, _ = _call(_event(VERIFY, PROOF), FakeStore(_held()), cognito, sfn)
        assert status == 403
        assert sfn.successes == []

    def test_a_closed_hold_reports_the_window_closed(self) -> None:
        sfn = FakeSfn(error=_error("TaskTimedOut"))
        status, body = _call(_event(VERIFY, PROOF), FakeStore(_held()), FakeCognito(), sfn)
        assert status == 409
        assert "closed" in body["error"]

    def test_a_transfer_not_awaiting_step_up_is_not_verified(self) -> None:
        cognito = FakeCognito()
        status, _ = _call(_event(VERIFY, PROOF), FakeStore(_held("released")), cognito)
        assert status == 409
        assert cognito.responded == []

    def test_a_missing_credential_is_a_bad_request(self) -> None:
        status, _ = _call(_event(VERIFY, {"session": "s"}), FakeStore(_held()))
        assert status == 400

    def test_an_unexpected_failure_is_a_bad_gateway_not_a_release(self) -> None:
        sfn = FakeSfn()
        cognito = FakeCognito(error=_error("InternalErrorException"))
        status, _ = _call(_event(VERIFY, PROOF), FakeStore(_held()), cognito, sfn)
        assert status == 502
        assert sfn.successes == []


def test_a_session_too_malformed_to_send_is_a_failed_verification() -> None:
    # The SDK rejects it before any request, so no Cognito error code is involved.
    from botocore.exceptions import ParamValidationError

    sfn = FakeSfn()
    malformed = ParamValidationError(report="Session: shorter than 20")
    cognito = FakeCognito(error=malformed)  # type: ignore[arg-type]
    status, _ = _call(_event(VERIFY, PROOF), FakeStore(_held()), cognito, sfn)
    assert status == 403
    assert sfn.successes == []


def test_the_handler_module_exposes_the_lambda_entry_point() -> None:
    assert callable(handler.lambda_handler)
