"""Shared plumbing for the simulator scripts: stack outputs, throwaway users and timed API calls.

Everything named here comes from ``terraform output``, never from hardcoded values, and every user
created carries the ``test-`` prefix so cleanup is unambiguous.
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS = REPO_ROOT / "research" / "results" / "tables"
TEST_PREFIX = "test-"

_TIMING = re.compile(r"\s*([A-Za-z_]+);dur=([\d.]+)")
_THROTTLED_RETRIES = 3


def outputs() -> dict[str, Any]:
    terraform = os.environ.get("TERRAFORM", "terraform")
    result = subprocess.run(
        [terraform, f"-chdir={REPO_ROOT / 'infra' / 'envs' / 'dev'}", "output", "-json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return {name: entry["value"] for name, entry in json.loads(result.stdout).items()}


def percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile: always an observed value, never an interpolation."""
    if not values:
        raise ValueError("percentile of no values")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def parse_server_timing(header: str | None) -> dict[str, float]:
    """``handler;dur=12.3, read;dur=4.1`` as ``{"handler": 12.3, "read": 4.1}``."""
    timings: dict[str, float] = {}
    for part in (header or "").split(","):
        match = _TIMING.match(part)
        if match:
            timings[match.group(1)] = float(match.group(2))
    return timings


@dataclass(frozen=True)
class Reply:
    status: int
    body: dict[str, Any]
    server_timing: dict[str, float]
    round_trip_ms: float


def post(url: str, body: Any, token: str) -> Reply:
    """POST with timing. A throttled request is retried after a pause, and its time excluded."""
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
    for attempt in range(_THROTTLED_RETRIES + 1):
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw, status = response.read(), response.status
                timing = response.headers.get("Server-Timing")
        except urllib.error.HTTPError as error:
            raw, status, timing = error.read(), error.code, error.headers.get("Server-Timing")
        elapsed = (time.perf_counter() - started) * 1000
        if status == 429 and attempt < _THROTTLED_RETRIES:
            time.sleep(1.0 + attempt)
            continue
        return Reply(status, json.loads(raw or b"{}"), parse_server_timing(timing), elapsed)
    raise AssertionError("unreachable")


class Users:
    """Throwaway Cognito users, deleted on ``close``."""

    def __init__(self, aws: Any, out: dict[str, Any]) -> None:
        self.cognito = aws.client("cognito-idp")
        self.out = out
        self.created: list[str] = []

    def create(self, label: str) -> dict[str, str]:
        email = f"{TEST_PREFIX}{label}-{secrets.token_hex(4)}@example.com"
        password = f"{secrets.token_urlsafe(18)}Aa1"
        pool = self.out["user_pool_id"]
        created = self.cognito.admin_create_user(
            UserPoolId=pool,
            Username=email,
            TemporaryPassword=password,
            UserAttributes=[
                {"Name": "email", "Value": email},
                {"Name": "email_verified", "Value": "true"},
            ],
            MessageAction="SUPPRESS",
        )
        self.created.append(email)
        self.cognito.admin_set_user_password(
            UserPoolId=pool, Username=email, Password=password, Permanent=True
        )
        sub = next(a["Value"] for a in created["User"]["Attributes"] if a["Name"] == "sub")
        return {"email": email, "sub": sub, "token": self.sign_in(email, password)}

    def sign_in(self, email: str, password: str) -> str:
        client_id = self.out["user_pool_client_id"]
        result = self.cognito.initiate_auth(
            AuthFlow="USER_AUTH",
            ClientId=client_id,
            AuthParameters={
                "USERNAME": email,
                "PREFERRED_CHALLENGE": "PASSWORD",
                "PASSWORD": password,
            },
        )
        if "AuthenticationResult" not in result:
            result = self.cognito.respond_to_auth_challenge(
                ClientId=client_id,
                ChallengeName="SELECT_CHALLENGE",
                Session=result["Session"],
                ChallengeResponses={"USERNAME": email, "ANSWER": "PASSWORD", "PASSWORD": password},
            )
        return result["AuthenticationResult"]["AccessToken"]

    def close(self) -> None:
        for email in self.created:
            try:
                self.cognito.admin_delete_user(UserPoolId=self.out["user_pool_id"], Username=email)
            except self.cognito.exceptions.UserNotFoundException:
                continue
        self.created.clear()
