"""Transfer status, and the passkey step-up that releases a held transfer.

Step-up is verified here, server-side, never taken on the browser's word. This function asks Cognito
for a WebAuthn challenge for the signed-in user and accepts only an answer to that challenge. A
password, a refreshed token or a replayed response cannot satisfy it. Only after Cognito accepts the
passkey does this function hand the workflow its task token, and the token is what releases the
transfer. The device-bound authenticator is the trust anchor (architecture sections 5.3 and 7.3),
so it is the only thing that can open the hold.

Routes, all behind the JWT authorizer:

    GET  /transfers/{transfer_id}                 status, amount, balance
    POST /transfers/{transfer_id}/stepup          start: returns a WebAuthn challenge
    POST /transfers/{transfer_id}/stepup/verify   finish: verifies the passkey, releases the hold

A transfer is looked up in the caller's own ledger partition, so another user's transfer id simply
does not exist from here.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from botocore.exceptions import ClientError, ParamValidationError

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

AWAITING_STEP_UP = "awaiting_step_up"
_TRANSFER_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")

# Cognito's answers to a credential that does not verify, or a session that is no longer valid.
_FAILED_VERIFICATION = frozenset(
    {
        "NotAuthorizedException",
        "CodeMismatchException",
        "ExpiredCodeException",
        "InvalidParameterException",
    }
)
# The workflow's hold has already ended: timed out, released or cancelled.
_CLOSED_HOLD = frozenset({"TaskTimedOut", "TaskDoesNotExist", "InvalidToken"})


class TransferStore(Protocol):
    def get(self, uid: str, transfer_id: str) -> dict[str, Any] | None: ...

    def balance(self, uid: str) -> Decimal | None: ...


class LedgerReader:
    def __init__(self, table: Any) -> None:
        self._table = table

    def get(self, uid: str, transfer_id: str) -> dict[str, Any] | None:
        return self._table.get_item(
            Key={"PK": f"LEDGER#{uid}", "SK": f"TXN#{transfer_id}"}, ConsistentRead=True
        ).get("Item")

    def balance(self, uid: str) -> Decimal | None:
        item = self._table.get_item(
            Key={"PK": f"LEDGER#{uid}", "SK": "BALANCE"}, ConsistentRead=True
        ).get("Item")
        return None if item is None else item["balance"]


@dataclass(frozen=True)
class Dependencies:
    store: TransferStore
    cognito: Any
    sfn: Any
    client_id: str
    clock: Callable[[], float] = time.time


def _code(error: ClientError) -> str:
    return str(error.response.get("Error", {}).get("Code", ""))


def _response(status: int, body: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def handle(event: Mapping[str, Any], deps: Dependencies) -> dict[str, Any]:
    claims = event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})
    uid, username = claims.get("sub"), claims.get("username")
    if not (isinstance(uid, str) and uid and isinstance(username, str) and username):
        return _response(401, {"error": "unauthenticated"})

    transfer_id = (event.get("pathParameters") or {}).get("transfer_id", "")
    if not (isinstance(transfer_id, str) and _TRANSFER_ID.fullmatch(transfer_id)):
        return _response(404, {"error": "transfer not found"})

    try:
        transfer = deps.store.get(uid, transfer_id)
        if transfer is None:
            return _response(404, {"error": "transfer not found"})

        route = event.get("routeKey")
        if route == "GET /transfers/{transfer_id}":
            return _status(uid, transfer_id, transfer, deps)
        if route == "POST /transfers/{transfer_id}/stepup":
            return _start(username, transfer, deps)
        if route == "POST /transfers/{transfer_id}/stepup/verify":
            return _verify(event, username, transfer, deps)
        return _response(404, {"error": "route not found"})
    except Exception:
        LOGGER.exception("transfer request failed")
        return _response(502, {"error": "transfer service unavailable"})


def _status(
    uid: str, transfer_id: str, transfer: Mapping[str, Any], deps: Dependencies
) -> dict[str, Any]:
    balance = deps.store.balance(uid)
    return _response(
        200,
        {
            "transfer_id": transfer_id,
            "status": transfer["status"],
            "reason": transfer.get("reason"),
            "amount": float(transfer["amount"]),
            "balance": None if balance is None else float(balance),
        },
    )


def _not_awaiting(transfer: Mapping[str, Any]) -> dict[str, Any]:
    return _response(
        409, {"error": "transfer is not awaiting step-up", "status": transfer["status"]}
    )


def _failed_verification() -> dict[str, Any]:
    return _response(403, {"error": "passkey verification failed"})


def _start(username: str, transfer: Mapping[str, Any], deps: Dependencies) -> dict[str, Any]:
    if transfer["status"] != AWAITING_STEP_UP:
        return _not_awaiting(transfer)

    challenge = deps.cognito.initiate_auth(
        AuthFlow="USER_AUTH",
        ClientId=deps.client_id,
        AuthParameters={"USERNAME": username, "PREFERRED_CHALLENGE": "WEB_AUTHN"},
    )
    # Anything other than a WebAuthn challenge -- a password prompt, a choice of factors -- means
    # there is no passkey to verify, and nothing else is acceptable here.
    if challenge.get("ChallengeName") != "WEB_AUTHN":
        return _response(409, {"error": "no passkey is registered for this account"})

    return _response(
        200,
        {
            "session": challenge["Session"],
            "options": json.loads(challenge["ChallengeParameters"]["CREDENTIAL_REQUEST_OPTIONS"]),
        },
    )


def _verify(
    event: Mapping[str, Any], username: str, transfer: Mapping[str, Any], deps: Dependencies
) -> dict[str, Any]:
    try:
        body = json.loads(event.get("body") or "")
    except json.JSONDecodeError:
        body = None
    if not (
        isinstance(body, dict)
        and isinstance(body.get("session"), str)
        and isinstance(body.get("credential"), dict)
    ):
        return _response(400, {"error": "session and credential are required"})

    if transfer["status"] != AWAITING_STEP_UP or not transfer.get("task_token"):
        return _not_awaiting(transfer)

    try:
        result = deps.cognito.respond_to_auth_challenge(
            ClientId=deps.client_id,
            ChallengeName="WEB_AUTHN",
            Session=body["session"],
            ChallengeResponses={
                "USERNAME": username,
                "CREDENTIAL": json.dumps(body["credential"]),
            },
        )
    except ParamValidationError:
        # A session or credential too malformed for the SDK to even send is a failed verification,
        # not an outage.
        LOGGER.warning("step-up verification failed: malformed session or credential")
        return _failed_verification()
    except ClientError as error:
        if _code(error) in _FAILED_VERIFICATION:
            LOGGER.warning("step-up verification failed: %s", _code(error))
            return _failed_verification()
        raise

    tokens = result.get("AuthenticationResult")
    if not tokens:
        return _failed_verification()
    _revoke(tokens, deps)

    try:
        deps.sfn.send_task_success(
            taskToken=transfer["task_token"],
            output=json.dumps(
                {"verified": True, "method": "passkey", "verified_at": int(deps.clock())}
            ),
        )
    except ClientError as error:
        if _code(error) in _CLOSED_HOLD:
            return _response(409, {"error": "the step-up window has closed"})
        raise
    return _response(200, {"status": "verified"})


def _revoke(tokens: Mapping[str, Any], deps: Dependencies) -> None:
    # Verification mints a full token set as a side effect. It is proof, not a login, so the
    # refresh token is revoked rather than left valid.
    refresh = tokens.get("RefreshToken")
    if not refresh:
        return
    try:
        deps.cognito.revoke_token(Token=refresh, ClientId=deps.client_id)
    except ClientError:
        LOGGER.exception("could not revoke step-up refresh token")


_dependencies: Dependencies | None = None


def lambda_handler(event: Mapping[str, Any], context: Any) -> dict[str, Any]:
    global _dependencies
    if _dependencies is None:
        import boto3

        _dependencies = Dependencies(
            store=LedgerReader(boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])),
            cognito=boto3.client("cognito-idp"),
            sfn=boto3.client("stepfunctions"),
            client_id=os.environ["COGNITO_CLIENT_ID"],
        )
    return handle(event, _dependencies)
