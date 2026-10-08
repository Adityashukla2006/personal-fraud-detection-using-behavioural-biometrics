"""Seed and clear demo scenarios on the dev stack, from this machine, with admin credentials.

The deployed system keeps its permissions exactly as designed: only the adaptation role writes
profiles, and only the aggregator writes payee risk. This script writes demo state out of band, the
same way the integration tests seed theirs, and marks every item it writes with ``demo_seed`` so
``clear`` removes exactly those items and nothing real.

    python simulator/demo_scenarios.py analyst         --email you@example.com
    python simulator/demo_scenarios.py takeover        --email you@example.com
    python simulator/demo_scenarios.py enrolled-device --email you@example.com
    python simulator/demo_scenarios.py flagged-payee   --account 123456789
    python simulator/demo_scenarios.py history         --email you@example.com [--account 123456789]
    python simulator/demo_scenarios.py clear           --email you@example.com [--account 123456789]

What each does to your next transfer, with the current fusion model:

    takeover         a typing profile far from anyone's typing: the behaviour channel alerts, and
                     the friction ceiling holds it at step-up
    flagged-payee    the aggregator's flag on a payee account: transfers to it are restricted and
                     wait for an analyst in the console; with takeover as well, they are blocked
    enrolled-device  marks your browser's device as enrolled, so a passkey step-up there earns full
                     trust and adaptation learns from it
    analyst          adds you to the analyst group; sign in to the console again afterwards
    history          months of ordinary use: transaction aggregates, an established account, your
                     browser enrolled and, with --account, a long-standing verified payee. A large
                     transfer to a new payee then stands out against the history; the nightly
                     aggregator overwrites the aggregates with what the lake really holds
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


DAY = 86_400
HISTORY_TRANSFERS = 64
HISTORY_SESSIONS = 40
# Waking hours in India, 05:30 to 21:30 IST, in UTC: the histogram is UTC, like the aggregator's.
HISTORY_HOURS_UTC = range(0, 16)


def history_items(
    uid: str,
    device_id: str,
    device_class: str,
    payee_account: str | None = None,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """What months of ordinary use would have built: payments of about 2,000, by day."""
    now = time.time() if now is None else now
    per_hour = Decimal(HISTORY_TRANSFERS // len(HISTORY_HOURS_UTC))
    items: list[dict[str, Any]] = [
        {
            "PK": f"AGG#{uid}",
            "SK": "WINDOW#30d",
            "amount_p50": Decimal("2000"),
            "amount_p95": Decimal("8000"),
            "daily_count_p95": Decimal("2"),
            "hour_histogram": [
                per_hour if hour in HISTORY_HOURS_UTC else Decimal("0") for hour in range(24)
            ],
            "history_count": HISTORY_TRANSFERS,
            "computed_at": int(now),
            "demo_seed": "history",
        },
        {
            "PK": f"USER#{uid}",
            "SK": "ACCOUNT",
            "session_count": HISTORY_SESSIONS,
            "demo_seed": "history",
        },
        {**enrolled_device(uid, device_id, device_class), "demo_seed": "history"},
    ]
    if payee_account:
        items.append(
            {
                "PK": f"USER#{uid}",
                "SK": f"PAYEE#{payee_id(payee_account)}",
                "first_seen": int(now) - 200 * DAY,
                "verified_at": int(now) - 190 * DAY,
                "txn_count": 12,
                "demo_seed": "history",
            }
        )
    return items


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
        "scenario",
        choices=["analyst", "takeover", "enrolled-device", "flagged-payee", "history", "clear"],
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

    def latest_device(uid: str) -> tuple[str, str]:
        latest = table.query(
            IndexName="GSI1",
            KeyConditionExpression=Key("GSI1PK").eq(f"USER#{uid}"),
            ScanIndexForward=False,
            Limit=1,
        )["Items"]
        # The index projects every attribute, so the newest decision already carries its device.
        if not latest or "device_id" not in latest[0]:
            parser.error("sign in to the banking client once first, so the device is known")
        return latest[0]["device_id"], latest[0]["device_class"]

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
        device_id, device_class = latest_device(uid)
        table.put_item(Item=enrolled_device(uid, device_id, device_class))
        print(f"Marked device {device_id} as enrolled.")
    elif args.scenario == "history":
        uid = subject()
        device_id, device_class = latest_device(uid)
        seeds = history_items(uid, device_id, device_class, args.account)
        for item in seeds:
            table.put_item(Item=item)
        print(f"Seeded {len(seeds)} history item(s): device {device_id} is now enrolled.")
    elif args.scenario == "flagged-payee":
        if not args.account:
            parser.error("flagged-payee needs --account")
        table.put_item(Item=flagged_payee(args.account))
        print(f"Flagged payee account {args.account}: transfers to it are restricted.")
    else:
        removed = 0
        if args.email:
            uid = subject()
            items = [
                item
                for partition in (f"USER#{uid}", f"AGG#{uid}")
                for item in table.query(KeyConditionExpression=Key("PK").eq(partition))["Items"]
            ]
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
