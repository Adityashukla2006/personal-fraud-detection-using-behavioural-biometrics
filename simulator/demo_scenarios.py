"""Seed and clear demo scenarios on the dev stack, from this machine, with admin credentials.

The deployed system keeps its permissions exactly as designed: only the adaptation role writes
profiles, and only the aggregator writes payee risk. This script writes demo state out of band, the
same way the integration tests seed theirs, and marks every item it writes with ``demo_seed`` so
``clear`` removes exactly those items and nothing real.

    python simulator/demo_scenarios.py analyst         --email you@example.com
    python simulator/demo_scenarios.py takeover        --email you@example.com
    python simulator/demo_scenarios.py enrolled-device --email you@example.com
    python simulator/demo_scenarios.py flagged-payee   --account 123456789
    python simulator/demo_scenarios.py clear           --email you@example.com [--account 123456789]

What each does to your next transfer, with the current fusion model:

    takeover         a typing profile far from anyone's typing: the behaviour channel alerts, and
                     the friction ceiling holds it at step-up
    flagged-payee    the aggregator's flag on a payee account: transfers to it are restricted and
                     wait for an analyst in the console; with takeover as well, they are blocked
    enrolled-device  marks your browser's device as enrolled, so a passkey step-up there earns full
                     trust and adaptation learns from it
    analyst          adds you to the analyst group; sign in to the console again afterwards
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_FEATURE_NAMES = (
    "mean_hold",
    "std_hold",
    "mean_flight",
    "std_flight",
    "burst_count",
    "pause_count",
    "longest_pause",
    "entry_duration",
    "typing_speed",
)
ENROLLED_SESSIONS = 12


def payee_id(account: str) -> str:
    """Exactly what the browser sends: SHA-256 of the trimmed account number, hex."""
    return hashlib.sha256(account.strip().encode("utf-8")).hexdigest()


def takeover_profile(uid: str, device_class: str) -> dict[str, Any]:
    width = len(BENCHMARK_FEATURE_NAMES)
    return {
        "PK": f"USER#{uid}",
        "SK": f"PROFILE#{device_class}",
        "features": list(BENCHMARK_FEATURE_NAMES),
        "mu": [Decimal("5")] * width,
        "sigma": [Decimal("0.01")] * width,
        "anchor_mu": [Decimal("5")] * width,
        "anchor_sigma": [Decimal("0.01")] * width,
        "anchor_at": int(time.time()),
        "saturations": 0,
        "n_sessions": 20,
        "version": 1,
        "demo_seed": "takeover",
    }


def enrolled_device(uid: str, device_id: str, device_class: str) -> dict[str, Any]:
    return {
        "PK": f"USER#{uid}",
        "SK": f"DEV#{device_id}",
        "session_count": ENROLLED_SESSIONS,
        "device_class": device_class,
        "demo_seed": "enrolled-device",
    }


def flagged_payee(account: str) -> dict[str, Any]:
    return {
        "PK": f"PAYEE#{payee_id(account)}",
        "SK": "RISK",
        "risk_score": Decimal("1"),
        "computed_at": int(time.time()),
        "flagged": True,
        "demo_seed": "flagged-payee",
    }


def demo_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only items this script marked. Anything else in the partition is real and left alone."""
    return [item for item in items if "demo_seed" in item]


def outputs() -> dict[str, Any]:
    terraform = os.environ.get("TERRAFORM", "terraform")
    result = subprocess.run(
        [terraform, f"-chdir={REPO_ROOT / 'infra' / 'envs' / 'dev'}", "output", "-json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return {name: entry["value"] for name, entry in json.loads(result.stdout).items()}


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed or clear demo scenarios on the dev stack.")
    parser.add_argument(
        "scenario", choices=["analyst", "takeover", "enrolled-device", "flagged-payee", "clear"]
    )
    parser.add_argument("--email")
    parser.add_argument("--account", help="payee account number, as typed in the banking client")
    parser.add_argument(
        "--device-class", default="desktop", choices=["desktop", "mobile", "tablet"]
    )
    args = parser.parse_args()

    import boto3
    from boto3.dynamodb.conditions import Key

    out = outputs()
    aws = boto3.Session(region_name=out["region"])
    table = aws.resource("dynamodb").Table(out["table_name"])
    cognito = aws.client("cognito-idp")

    def subject() -> str:
        if not args.email:
            parser.error(f"{args.scenario} needs --email")
        user = cognito.admin_get_user(UserPoolId=out["user_pool_id"], Username=args.email)
        return next(a["Value"] for a in user["UserAttributes"] if a["Name"] == "sub")

    if args.scenario == "analyst":
        if not args.email:
            parser.error("analyst needs --email")
        cognito.admin_add_user_to_group(
            UserPoolId=out["user_pool_id"], Username=args.email, GroupName=out["analyst_group"]
        )
        print(f"{args.email} is now an analyst. Sign in to {out['console_url']} again.")
    elif args.scenario == "takeover":
        table.put_item(Item=takeover_profile(subject(), args.device_class))
        print(f"Seeded a mismatched {args.device_class} profile: the next transfer steps up.")
    elif args.scenario == "enrolled-device":
        uid = subject()
        latest = table.query(
            IndexName="GSI1",
            KeyConditionExpression=Key("GSI1PK").eq(f"USER#{uid}"),
            ScanIndexForward=False,
            Limit=1,
        )["Items"]
        # The index projects every attribute, so the newest decision already carries its device.
        if not latest or "device_id" not in latest[0]:
            parser.error("sign in to the banking client once first, so the device is known")
        decision = latest[0]
        table.put_item(Item=enrolled_device(uid, decision["device_id"], decision["device_class"]))
        print(f"Marked device {decision['device_id']} as enrolled.")
    elif args.scenario == "flagged-payee":
        if not args.account:
            parser.error("flagged-payee needs --account")
        table.put_item(Item=flagged_payee(args.account))
        print(f"Flagged payee account {args.account}: transfers to it are restricted.")
    else:
        removed = 0
        if args.email:
            uid = subject()
            items = table.query(KeyConditionExpression=Key("PK").eq(f"USER#{uid}"))["Items"]
            for item in demo_items(items):
                table.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
                removed += 1
        if args.account:
            key = {"PK": f"PAYEE#{payee_id(args.account)}", "SK": "RISK"}
            item = table.get_item(Key=key).get("Item")
            if item and demo_items([item]):
                table.delete_item(Key=key)
                removed += 1
        print(f"Removed {removed} demo item(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
