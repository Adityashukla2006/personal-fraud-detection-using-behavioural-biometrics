"""Phase 8: attack traffic against the live endpoint, and the detection rate per attack class.

The attack classes are frozen in ``simulator/attacks.py``. This script only sends them.

Users. The first N CMU subjects become throwaway Cognito users; each is attacked by the next subject
in order. Each gets a seeded baseline: a desktop profile bootstrapped with fraudcore from pooled
day-1 typing, an enrolled device, account history, 30-day transaction aggregates and one verified
payee. That stands in for what adaptation and the aggregator would learn over months of ordinary
use, which neither can produce for a synthetic user. It is written out of band with admin
credentials, as the integration tests seed theirs, so the deployed permissions are untouched.

Sessions. Every session runs login, payee, amount and confirmation against the real API. A
confirmation decision of step-up or stronger counts as detected. Poisoning is also judged on its
goal: the victim's profile version must be unchanged afterwards, because the attacker never passes
a passkey step-up and so is never learned.

Outputs, under research/results/tables:

    phase8_detection.csv   one row per attack class
    phase8_sessions.csv    one row per checkpoint call, with latency and per-stage handler time
    phase8_poisoning.csv   profile integrity after the run
    phase8_run.json        run window and call counts, read by measure_cost.py

Cleanup removes the users and everything seeded or created for them, and stops the workflows still
waiting on step-ups or reviews that will never come. Audit-lake events stay, as an audit lake's
should.

Usage, from the repository root::

    python simulator/run_attacks.py [--users 10] [--sessions 5]
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import secrets
import sys
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (REPO_ROOT / "src", REPO_ROOT / "src" / "lambdas", REPO_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from fraudcore.adaptation import (  # noqa: E402
    PROFILE_FEATURES,
    AdaptationPolicy,
    BufferedSession,
    rebuild,
)
from fraudcore.features import KeystrokeTiming, vector  # noqa: E402
from fraudcore.session import pooled_extract  # noqa: E402
from simulator import attacks, cmu  # noqa: E402
from simulator.stack import RESULTS, Users, outputs, post  # noqa: E402

SEED = 20260915
POOL = len(attacks.CHECKPOINTS)
DAY = 86_400
ACTIONS = ("allow", "monitor", "step_up", "restrict", "block")
STAGES = ("handler", "read", "score", "write", "publish", "workflow")
SESSION_COLUMNS = [
    "attack", "victim", "session", "session_id", "checkpoint", "decision_id", "action", "risk",
    "confidence", "top_channel", "constraints", "transfer_status", "round_trip_ms",
    *[f"{stage}_ms" for stage in STAGES],
]


def _decimal(value: float) -> Decimal:
    return Decimal(repr(round(float(value), 9)))


def enrolment_sessions(reps: Sequence[cmu.Repetition]) -> list[tuple[float, ...]]:
    """Day-1 typing, pooled four fields at a time: the same pooling a live session gets."""
    enrolment = [r for r in reps if r.session == attacks.ENROLMENT_SESSION]
    groups = [enrolment[i : i + POOL] for i in range(0, len(enrolment) - POOL + 1, POOL)]
    return [
        tuple(
            vector(
                pooled_extract([KeystrokeTiming(r.hold, r.down_down, r.up_down) for r in group]),
                PROFILE_FEATURES,
            )
        )
        for group in groups
    ]


def baseline_items(
    uid: str,
    victim: str,
    reps: Sequence[cmu.Repetition],
    device_id: str,
    now: float,
    policy: AdaptationPolicy,
) -> list[dict[str, Any]]:
    buffer = [BufferedSession(features, 1.0) for features in enrolment_sessions(reps)]
    profile = rebuild(None, buffer, 1.0, now, policy).profile
    if profile is None:
        raise ValueError(f"{victim} has too little day-1 typing to bootstrap a profile")
    user = f"USER#{uid}"
    return [
        {
            "PK": user,
            "SK": "PROFILE#desktop",
            "features": list(PROFILE_FEATURES),
            "mu": [_decimal(v) for v in profile.centre],
            "sigma": [_decimal(v) for v in profile.scale],
            "anchor_mu": [_decimal(v) for v in profile.anchor_centre],
            "anchor_sigma": [_decimal(v) for v in profile.anchor_scale],
            "anchor_at": int(now),
            "saturations": 0,
            "n_sessions": len(buffer),
            "version": profile.version,
        },
        {
            "PK": user,
            "SK": f"DEV#{device_id}",
            "session_count": 30,
            "device_class": "desktop",
            "first_seen": int(now) - 200 * DAY,
        },
        {"PK": user, "SK": "ACCOUNT", "session_count": 40},
        {
            "PK": f"AGG#{uid}",
            "SK": "WINDOW#30d",
            "amount_p50": Decimal("1500"),
            "amount_p95": Decimal("6000"),
            "daily_count_p95": Decimal("3"),
            # Flat, so the hour of day the run happens to start never shows up as a signal.
            "hour_histogram": [Decimal("5")] * 24,
            "history_count": 60,
        },
        {
            "PK": user,
            "SK": f"PAYEE#{attacks.payee_id(attacks.known_payee(victim))}",
            "first_seen": int(now) - 200 * DAY,
            "verified_at": int(now) - 190 * DAY,
            "txn_count": 12,
        },
    ]


def run_session(
    plan: attacks.SessionPlan, index: int, token: str, api_url: str
) -> list[dict[str, Any]]:
    session_id = f"sim-{secrets.token_hex(16)}"
    rows = []
    for checkpoint in plan.checkpoints:
        reply = post(f"{api_url}/score", attacks.request_body(plan, checkpoint, session_id), token)
        if reply.status != 200:
            raise RuntimeError(f"{plan.attack} {checkpoint.name}: {reply.status} {reply.body}")
        body = reply.body
        row: dict[str, Any] = {
            "attack": plan.attack,
            "victim": plan.victim,
            "session": index,
            "session_id": session_id,
            "checkpoint": checkpoint.name,
            "decision_id": body["decision_id"],
            "action": body["action"],
            "risk": body["risk"],
            "confidence": body["confidence"],
            "top_channel": body["contributions"][0]["channel"] if body["contributions"] else "",
            "constraints": "|".join(body["constraints"]),
            "transfer_status": body.get("transfer", {}).get("status", ""),
            "round_trip_ms": round(reply.round_trip_ms, 1),
        }
        for stage in STAGES:
            row[f"{stage}_ms"] = reply.server_timing.get(stage, "")
        rows.append(row)
    return rows


def summarise(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Detection and action rates per attack class, from the confirmation decisions."""
    confirmations: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["checkpoint"] == "confirmation":
            confirmations[row["attack"]].append(row)

    table = []
    for attack in attacks.ATTACK_CLASSES:
        group = confirmations.get(attack, [])
        if not group:
            continue
        actions = [row["action"] for row in group]
        risks = [float(row["risk"]) for row in group if row["risk"] not in (None, "")]
        entry: dict[str, Any] = {
            "attack": attack,
            "sessions": len(group),
            "detection_rate": round(attacks.detection_rate(actions), 4),
        }
        for action in ACTIONS:
            entry[f"{action}_rate"] = round(actions.count(action) / len(group), 4)
        entry["mean_risk"] = round(sum(risks) / len(risks), 2) if risks else ""
        entry["fail_open"] = sum("fail_open" in str(row["constraints"]) for row in group)
        table.append(entry)
    return table


def _write_csv(path: Path, rows: Sequence[dict[str, Any]], columns: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _delete_partition(table: Any, partition: str) -> None:
    from boto3.dynamodb.conditions import Key

    request: dict[str, Any] = {"KeyConditionExpression": Key("PK").eq(partition)}
    with table.batch_writer() as batch:
        while True:
            page = table.query(**request)
            for item in page.get("Items", []):
                batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
            if "LastEvaluatedKey" not in page:
                break
            request["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def cleanup(
    aws: Any, out: dict[str, Any], accounts: dict[str, dict[str, str]], rows: list[dict[str, Any]]
) -> None:
    table = aws.resource("dynamodb").Table(out["table_name"])
    sfn = aws.client("stepfunctions")
    execution_base = out["state_machine_arn"].replace(":stateMachine:", ":execution:")

    for row in rows:
        if row["checkpoint"] == "confirmation" and row["transfer_status"] in (
            "awaiting_step_up",
            "under_review",
        ):
            try:
                sfn.stop_execution(
                    executionArn=f"{execution_base}:{row['decision_id']}",
                    cause="simulator cleanup",
                )
            except sfn.exceptions.ExecutionDoesNotExist:
                continue

    with table.batch_writer() as batch:
        for row in rows:
            batch.delete_item(Key={"PK": f"DEC#{row['decision_id']}", "SK": "META"})
        for session_id in {row["session_id"] for row in rows}:
            batch.delete_item(Key={"PK": f"SESS#{session_id}", "SK": "META"})
    for account in accounts.values():
        for prefix in ("USER#", "AGG#", "LEDGER#"):
            _delete_partition(table, f"{prefix}{account['sub']}")


def main(argv: Sequence[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Attack traffic against the live endpoint.")
    parser.add_argument("--users", type=int, default=10)
    parser.add_argument("--sessions", type=int, default=5, help="sessions per class per user")
    parser.add_argument("--output-dir", type=Path, default=RESULTS)
    args = parser.parse_args(argv)

    import boto3

    out = outputs()
    aws = boto3.Session(region_name=out["region"])
    table = aws.resource("dynamodb").Table(out["table_name"])
    data = cmu.load()
    everyone = list(data)
    victims = everyone[: args.users]
    policy = AdaptationPolicy.load()
    rng = random.Random(SEED)

    users = Users(aws, out)
    accounts: dict[str, dict[str, str]] = {}
    rows: list[dict[str, Any]] = []
    started = datetime.now(UTC)
    try:
        now = started.timestamp()
        for victim in victims:
            account = users.create(f"sim-{victim}")
            account["device_id"] = f"sim-home-{victim}"
            accounts[victim] = account
            with table.batch_writer() as batch:
                for item in baseline_items(
                    account["sub"], victim, data[victim], account["device_id"], now, policy
                ):
                    batch.put_item(Item=item)

        for victim in victims:
            attacker = everyone[(everyone.index(victim) + 1) % len(everyone)]
            account = accounts[victim]
            for attack in attacks.ATTACK_CLASSES:
                for index in range(args.sessions):
                    plan = attacks.plan_session(
                        attack, victim, data[victim], data[attacker], index,
                        account["device_id"], rng,
                    )
                    rows += run_session(plan, index, account["token"], out["api_url"])
            print(f"{victim}: {len(rows)} calls so far")
        finished = datetime.now(UTC)

        integrity = []
        for victim, account in accounts.items():
            profile = table.get_item(
                Key={"PK": f"USER#{account['sub']}", "SK": "PROFILE#desktop"}, ConsistentRead=True
            ).get("Item", {})
            integrity.append(
                {
                    "victim": victim,
                    "profile_version_after": int(profile.get("version", 0)),
                    "profile_changed": int(profile.get("version", 0)) != 1,
                }
            )
    finally:
        try:
            cleanup(aws, out, accounts, rows)
        finally:
            users.close()

    detection = summarise(rows)
    confirmations = sum(row["checkpoint"] == "confirmation" for row in rows)
    run = {
        "started": started.isoformat(),
        "finished": finished.isoformat(),
        "users": len(victims),
        "sessions_per_class": args.sessions,
        "sessions": len({row["session_id"] for row in rows}),
        "api_calls": len(rows),
        "confirmations": confirmations,
        "profiles_changed": sum(entry["profile_changed"] for entry in integrity),
        "seed": SEED,
    }

    _write_csv(args.output_dir / "phase8_detection.csv", detection, list(detection[0]))
    _write_csv(args.output_dir / "phase8_sessions.csv", rows, SESSION_COLUMNS)
    _write_csv(
        args.output_dir / "phase8_poisoning.csv",
        integrity,
        ["victim", "profile_version_after", "profile_changed"],
    )
    (args.output_dir / "phase8_run.json").write_text(json.dumps(run, indent=2) + "\n", "utf-8")

    for entry in detection:
        print(entry)
    print(run)
    return {"run": run, "detection": detection, "emails": [a["email"] for a in accounts.values()]}


if __name__ == "__main__":
    main()
