"""Measure warm scoring latency against the deployed dev stack.

Signs in a temporary user, warms the function, then sends sequential login checkpoints, each in a
fresh session so payload size is constant. Two numbers per request: the round trip seen from this
machine, which includes the internet path, and the handler's own time from its Server-Timing
header. The gap between them is API Gateway, Lambda invocation and network. A per-segment breakdown
comes from X-Ray in Phase 8.

Writes research/results/tables/phase4_latency.csv. Cleans up its user and every item it created.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import boto3

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "tests" / "integration"))

from phase_04_scoring import _payload, sign_in  # noqa: E402

RESULTS = REPO_ROOT / "research" / "results" / "tables" / "phase4_latency.csv"


def outputs() -> dict[str, Any]:
    terraform = os.environ.get("TERRAFORM", "terraform")
    result = subprocess.run(
        [terraform, f"-chdir={REPO_ROOT / 'infra' / 'envs' / 'dev'}", "output", "-json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return {name: entry["value"] for name, entry in json.loads(result.stdout).items()}


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile: an observed value, never an interpolation."""
    ordered = sorted(values)
    return ordered[max(0, math.ceil(fraction * len(ordered)) - 1)]


def score(url: str, body: dict[str, Any], token: str) -> tuple[float, float, str]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read())
        timing = response.headers.get("Server-Timing", "")
    round_trip = (time.perf_counter() - started) * 1000
    handler = float(re.search(r"dur=([\d.]+)", timing).group(1))
    return round_trip, handler, payload["decision_id"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--warmup", type=int, default=5)
    args = parser.parse_args()

    out = outputs()
    aws = boto3.Session(region_name=out["region"])
    cognito = aws.client("cognito-idp")
    table = aws.client("dynamodb")
    url = f"{out['api_url']}/score"

    email = f"test-latency-{secrets.token_hex(4)}@example.com"
    password = f"{secrets.token_urlsafe(18)}Aa1"
    cognito.admin_create_user(
        UserPoolId=out["user_pool_id"],
        Username=email,
        TemporaryPassword=password,
        UserAttributes=[
            {"Name": "email", "Value": email},
            {"Name": "email_verified", "Value": "true"},
        ],
        MessageAction="SUPPRESS",
    )
    created: list[str] = []
    round_trips: list[float] = []
    handlers: list[float] = []
    try:
        cognito.admin_set_user_password(
            UserPoolId=out["user_pool_id"], Username=email, Password=password, Permanent=True
        )
        token = sign_in(cognito, out["user_pool_client_id"], email, password)

        for index in range(args.warmup + args.samples):
            session = f"test-latency-{hashlib.sha256(f'{email}{index}'.encode()).hexdigest()[:24]}"
            round_trip, handler, decision_id = score(url, _payload(session), token)
            created += [f"SESS#{session}", f"DEC#{decision_id}"]
            if index >= args.warmup:
                round_trips.append(round_trip)
                handlers.append(handler)
    finally:
        cognito.admin_delete_user(UserPoolId=out["user_pool_id"], Username=email)
        for partition in created:
            table.delete_item(
                TableName=out["table_name"], Key={"PK": {"S": partition}, "SK": {"S": "META"}}
            )

    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["measure", "condition", "samples", "p50_ms", "p95_ms", "max_ms"])
        for name, values in (("round_trip_client", round_trips), ("handler", handlers)):
            writer.writerow(
                [
                    name,
                    "warm",
                    len(values),
                    f"{percentile(values, 0.50):.1f}",
                    f"{percentile(values, 0.95):.1f}",
                    f"{max(values):.1f}",
                ]
            )
    print(RESULTS.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
