"""Seed and clear demo scenarios on the dev stack, from this machine, with admin credentials.

The deployed system keeps its permissions exactly as designed: only the adaptation role writes
profiles, and only the aggregator writes payee risk. This script writes demo state out of band, the
same way the integration tests seed theirs, and marks every item it writes with ``demo_seed`` so
``clear`` removes exactly those items and nothing real.

    python simulator/demo_scenarios.py analyst         --email you@example.com
    python simulator/demo_scenarios.py takeover        --email you@example.com
    python simulator/demo_scenarios.py enrolled-device --email you@example.com
    python simulator/demo_scenarios.py flagged-payee   --account 123456789
    python simulator/demo_scenarios.py history         --email you@example.com
    python simulator/demo_scenarios.py clear           --email you@example.com [--account 123456789]

What each does to your next transfer, with the current fusion model:

    takeover         a typing profile far from anyone's typing: the behaviour channel alerts, and
                     the friction ceiling holds it at step-up
    flagged-payee    the aggregator's flag on a payee account: transfers to it are restricted and
                     wait for an analyst in the console; with takeover as well, they are blocked
    enrolled-device  marks your browser's device as enrolled, so a passkey step-up there earns full
                     trust and adaptation learns from it
    analyst          adds you to the analyst group; sign in to the console again afterwards
    history          two months of ordinary use, through the real pipeline: released transfers to
                     four regular payees in the ledger and the audit lake, their payee edges, an
                     established account and your browser enrolled. The deployed aggregator is run
                     over the lake, so the nightly run rebuilds the same aggregates rather than
                     erasing them. Real transfers released before payees were recorded are
                     backfilled. A large transfer to a new payee then stands out against it all
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
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
HISTORY_SESSIONS = 40
# Long enough that every regular payee was first paid before the 30-day new-payee window, and short
# enough that the seeded transfers leave room on the account view's 100-item ledger page.
HISTORY_DAYS = 60
IST_OFFSET_SECONDS = 19_800
# Ordinary payments happen between 09:00 and 21:00 IST.
HISTORY_HOURS_IST = range(9, 21)
# Where `clear` finds the lake objects and money a history seed added.
MANIFEST_SORT = "SEED#history"
# The ledger's opening balance, as fraudcore.policy defines it; this script avoids importing it.
OPENING_BALANCE = Decimal("100000")
BACKFILLED_FIELDS = ("first_seen", "last_paid_at", "txn_count", "cum_amount")


@dataclass(frozen=True)
class RegularPayee:
    name: str
    account: str
    amount: int
    every_days: int
    # Fraction the amount varies by; rent is fixed, groceries are not.
    spread: float


REGULAR_PAYEES = (
    RegularPayee("Sharma Properties", "501002345678", 18_000, 30, 0.0),
    RegularPayee("Maa", "302118765432", 5_000, 7, 0.0),
    RegularPayee("BESCOM Electricity", "110045678901", 1_400, 30, 0.25),
    RegularPayee("Fresh Mart", "920020012345", 900, 2, 0.4),
)


@dataclass(frozen=True)
class SeededTransfer:
    transfer_id: str
    payee: RegularPayee
    amount: Decimal
    at: int


def seeded_transfers(uid: str, now: float) -> list[SeededTransfer]:
    """Two months of ordinary payments, the same for the same user, so a re-seed matches."""
    rng = random.Random(uid)
    transfers = []
    for index, payee in enumerate(REGULAR_PAYEES):
        for days_ago in range(HISTORY_DAYS - index, 0, -payee.every_days):
            day_start = (int(now) // DAY - days_ago) * DAY
            seconds_ist = rng.choice(HISTORY_HOURS_IST) * 3600 + rng.randrange(3600)
            amount = payee.amount * (1 + rng.uniform(-payee.spread, payee.spread))
            transfers.append(
                SeededTransfer(
                    transfer_id=f"demo{rng.getrandbits(112):028x}",
                    payee=payee,
                    amount=Decimal(round(amount)),
                    at=day_start + seconds_ist - IST_OFFSET_SECONDS,
                )
            )
    return sorted(transfers, key=lambda transfer: transfer.at)


def ledger_items(uid: str, transfers: list[SeededTransfer]) -> list[dict[str, Any]]:
    """Released transfers, as the ledger leaves them, and the payee edges releasing them wrote.

    Every regular payee was verified with a passkey on its first payment, as a real one would be.
    """
    items: list[dict[str, Any]] = []
    edges: dict[str, dict[str, Any]] = {}
    for transfer in transfers:
        pid = payee_id(transfer.payee.account)
        items.append(
            {
                "PK": f"LEDGER#{uid}",
                "SK": f"TXN#{transfer.transfer_id}",
                "status": "released",
                "amount": transfer.amount,
                "payee_id": pid,
                "decision_id": transfer.transfer_id,
                "action": "allow",
                "created_at": transfer.at,
                "closed_at": transfer.at + 5,
                "demo_seed": "history",
            }
        )
        edge = edges.setdefault(
            pid,
            {
                "PK": f"LEDGER#{uid}",
                "SK": f"PAYEE#{pid}",
                "first_seen": transfer.at,
                "verified_at": transfer.at,
                "txn_count": 0,
                "cum_amount": Decimal(0),
                "demo_seed": "history",
            },
        )
        edge["txn_count"] += 1
        edge["cum_amount"] += transfer.amount
        edge["last_paid_at"] = transfer.at
    return items + list(edges.values())


def backfilled_edges(uid: str, ledger: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Payee edges for real transfers released before the ledger recorded payees.

    Recomputed from every real released transfer, so running it again changes nothing. Never
    verified: whether a passkey released them is not on the transfer item.
    """
    edges: dict[str, dict[str, Any]] = {}
    for item in ledger:
        if "demo_seed" in item or not item["SK"].startswith("TXN#"):
            continue
        if item.get("status") != "released":
            continue
        at = int(item.get("closed_at", item["created_at"]))
        edge = edges.setdefault(
            item["payee_id"],
            {
                "PK": f"LEDGER#{uid}",
                "SK": f"PAYEE#{item['payee_id']}",
                "first_seen": at,
                "last_paid_at": at,
                "txn_count": 0,
                "cum_amount": Decimal(0),
            },
        )
        edge["first_seen"] = min(edge["first_seen"], at)
        edge["last_paid_at"] = max(edge["last_paid_at"], at)
        edge["txn_count"] += 1
        edge["cum_amount"] += Decimal(str(item["amount"]))
    return list(edges.values())


def context_items(uid: str, device_id: str, device_class: str) -> list[dict[str, Any]]:
    """An established account, and this browser enrolled on it."""
    return [
        {
            "PK": f"USER#{uid}",
            "SK": "ACCOUNT",
            "session_count": HISTORY_SESSIONS,
            "demo_seed": "history",
        },
        {**enrolled_device(uid, device_id, device_class), "demo_seed": "history"},
    ]


def payee_book_snippet() -> str:
    """Browser console code that names the regular payees: names never leave the browser."""
    book = {
        payee_id(payee.account): {"name": payee.name, "last4": payee.account[-4:]}
        for payee in REGULAR_PAYEES
    }
    return (
        "localStorage.setItem('bfd-payees', JSON.stringify({...JSON.parse("
        f"localStorage.getItem('bfd-payees') || '{{}}'), ...{json.dumps(book)}}}))"
    )


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

    for path in (REPO_ROOT / "src", REPO_ROOT):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    import boto3
    from boto3.dynamodb.conditions import Key

    out = outputs()
    aws = boto3.Session(region_name=out["region"])
    table = aws.resource("dynamodb").Table(out["table_name"])
    cognito = aws.client("cognito-idp")
    s3 = aws.client("s3")

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

    def partition(name: str) -> list[dict[str, Any]]:
        request: dict[str, Any] = {"KeyConditionExpression": Key("PK").eq(name)}
        items: list[dict[str, Any]] = []
        while True:
            page = table.query(**request)
            items += page["Items"]
            if "LastEvaluatedKey" not in page:
                return items
            request["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def archive(uid: str, transfers: list[SeededTransfer]) -> list[str]:
        """Each transfer's decision and completion events, written where the archiver writes."""
        from fraudcore.events import lake_key
        from simulator.siphoning import lake_events

        keys = []
        for transfer in transfers:
            events = lake_events(
                uid,
                transfer.transfer_id,
                payee_id(transfer.payee.account),
                float(transfer.amount),
                datetime.fromtimestamp(transfer.at, UTC),
                0.0,
            )
            for event in events:
                key = lake_key(event)
                body = json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n"
                s3.put_object(Bucket=out["lake_bucket"], Key=key, Body=body.encode("utf-8"))
                keys.append(key)
        return keys

    def undo_history(uid: str) -> bool:
        """Remove a previous history seed's lake objects, money and items. False if none."""
        manifest = table.get_item(
            Key={"PK": f"LEDGER#{uid}", "SK": MANIFEST_SORT}, ConsistentRead=True
        ).get("Item")
        if manifest is None:
            return False
        keys = list(manifest.get("lake_keys", []))
        for start in range(0, len(keys), 1000):
            s3.delete_objects(
                Bucket=out["lake_bucket"],
                Delete={"Objects": [{"Key": key} for key in keys[start : start + 1000]]},
            )
        table.update_item(
            Key={"PK": f"LEDGER#{uid}", "SK": "BALANCE"},
            UpdateExpression="ADD opening :t",
            ExpressionAttributeValues={":t": -manifest["amount"]},
        )
        with table.batch_writer() as batch:
            for item in partition(f"LEDGER#{uid}"):
                if item.get("demo_seed") == "history":
                    batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
        return True

    def run_aggregator() -> int:
        response = aws.client("lambda").invoke(
            FunctionName=out["aggregator_function_name"],
            Payload=json.dumps({"as_of": datetime.now(UTC).isoformat()}).encode(),
        )
        result = json.loads(response["Payload"].read() or b"{}")
        if response.get("FunctionError"):
            raise RuntimeError(f"aggregator failed: {result}")
        return int(result.get("users", 0))

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
        # A re-seed replaces the previous one rather than doubling the history.
        undo_history(uid)
        transfers = seeded_transfers(uid, time.time())
        lake_keys = archive(uid, transfers)
        seeds = [*ledger_items(uid, transfers), *context_items(uid, device_id, device_class)]
        with table.batch_writer() as batch:
            for item in seeds:
                batch.put_item(Item=item)
        backfilled = backfilled_edges(uid, partition(f"LEDGER#{uid}"))
        for edge in backfilled:
            # SET, not put: a real edge may already carry the ledger's verified_at.
            fields = {name: edge[name] for name in BACKFILLED_FIELDS}
            table.update_item(
                Key={"PK": edge["PK"], "SK": edge["SK"]},
                UpdateExpression="SET " + ", ".join(f"{name} = :{name}" for name in fields),
                ExpressionAttributeValues={f":{name}": value for name, value in fields.items()},
            )
        # Released money left the account, so the opening balance grows by it: the balance shown
        # today is unchanged and balance = opening - released still holds.
        total = sum((transfer.amount for transfer in transfers), Decimal(0))
        balance = {"PK": f"LEDGER#{uid}", "SK": "BALANCE"}
        table.update_item(
            Key=balance,
            UpdateExpression=(
                "SET balance = if_not_exists(balance, :o), opening = if_not_exists(opening, :o)"
            ),
            ExpressionAttributeValues={":o": OPENING_BALANCE},
        )
        table.update_item(
            Key=balance, UpdateExpression="ADD opening :t", ExpressionAttributeValues={":t": total}
        )
        table.put_item(
            Item={
                "PK": f"LEDGER#{uid}",
                "SK": MANIFEST_SORT,
                "lake_keys": lake_keys,
                "amount": total,
                "demo_seed": "history",
            }
        )
        users = run_aggregator()
        print(
            f"Seeded {len(transfers)} released transfers over {HISTORY_DAYS} days to "
            f"{len(REGULAR_PAYEES)} regular payees, backfilled {len(backfilled)} real payee(s), "
            f"enrolled device {device_id}, and re-ran the aggregator ({users} users)."
        )
        print("\nTo name the regular payees in your statement, paste this in the browser console:")
        print(payee_book_snippet())
    elif args.scenario == "flagged-payee":
        if not args.account:
            parser.error("flagged-payee needs --account")
        table.put_item(Item=flagged_payee(args.account))
        print(f"Flagged payee account {args.account}: transfers to it are restricted.")
    else:
        removed = 0
        if args.email:
            uid = subject()
            had_history = undo_history(uid)
            items = [
                item
                for name in (f"USER#{uid}", f"AGG#{uid}", f"LEDGER#{uid}")
                for item in partition(name)
            ]
            for item in demo_items(items):
                table.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
                removed += 1
            if had_history:
                # The aggregates were computed from the seeded lake history. The aggregator only
                # writes users it finds, so drop them first and let it rebuild from what is real.
                table.delete_item(Key={"PK": f"AGG#{uid}", "SK": "WINDOW#30d"})
                run_aggregator()
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
