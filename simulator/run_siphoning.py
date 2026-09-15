"""Phase 9: low-and-slow siphoning against the deployed batch layer, replayed day by day.

The class is frozen in ``simulator/siphoning.py``; this script only runs it. For each simulated day
it writes that day's transfers to the audit lake, invokes the deployed aggregator as of the end of
the day, reads back what the aggregator wrote, and scores a live confirmation of the next transfer
through the real endpoint.

Two detection moments are reported per user, because they answer different questions:

    flag day       the aggregator flags the user's payments to the payee. The batch layer's own
                   verdict, with money lost before detection the total sent by then.
    restrict day   a live confirmation to that payee is restricted or blocked. Typing alone can
                   never go past step-up (the friction ceiling), so a restriction needs a
                   non-behavioural channel. The batch evidence is one such channel, but payee
                   novelty is another, so the controls, paying a new payee of their own, measure
                   how much of the live friction comes from the new payee alone.

Step-up on the live probe is recorded too. A siphoning attacker's own typing can trigger it from
day one, which is the behavioural channel working, not the batch layer. Every probe types a sample
no other probe or replayed transfer uses, so the replay detector never fires on the simulator.

Outputs, under research/results/tables:

    phase9_siphoning.csv       one row per user: detection days, transfers and money lost before
    phase9_siphoning_days.csv  one row per user per day: signals and the live decision
    phase9_siphoning_run.json  run window and totals

Cleanup removes the users, everything seeded, written or computed for them, the replayed lake
objects and the probes' own lake events, and stops workflows still waiting.

Usage, from the repository root::

    python simulator/run_siphoning.py [--victims 5] [--controls 3]
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import secrets
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (REPO_ROOT / "src", REPO_ROOT / "src" / "lambdas", REPO_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from fraudcore.adaptation import AdaptationPolicy  # noqa: E402
from fraudcore.events import lake_key  # noqa: E402
from fraudcore.scoring import ReferenceProfile, behaviour_channel  # noqa: E402
from fraudcore.session import pooled_extract  # noqa: E402
from simulator import attacks, cmu, run_attacks, siphoning  # noqa: E402
from simulator.stack import RESULTS, Users, outputs, post  # noqa: E402

VICTIM, CONTROL = "victim", "control"
FIELDS_PER_CONFIRMATION = 4
LAKE_SETTLE_SECONDS = 30
DETAIL_TYPES = ("decision.scored", "transfer.completed", "stepup.verified")
USER_COLUMNS = [
    "subject", "role", "transfers_sent", "money_sent", "flag_day", "transfers_before_flag",
    "money_before_flag", "restrict_day", "money_before_restrict", "step_up_day",
    "final_siphon_score", "final_identity",
]
DAY_COLUMNS = [
    "day", "subject", "role", "transfers_sent", "money_sent", "siphon_score", "volume",
    "regularity", "band", "identity", "edge_flagged", "payee_risk", "payee_flagged",
    "probe_action", "probe_risk", "probe_payee_contribution", "probe_constraints",
    "decision_id", "session_id", "transfer_status",
]


def profile_from(item: dict[str, Any]) -> ReferenceProfile:
    return ReferenceProfile(
        tuple(str(name) for name in item["features"]),
        tuple(float(value) for value in item["mu"]),
        tuple(float(value) for value in item["sigma"]),
        "mad",
        0,
    )


def typing_groups(reps: Sequence[cmu.Repetition]) -> list[list[cmu.Repetition]]:
    later = [r for r in reps if r.session in attacks.LATER_SESSIONS]
    size = FIELDS_PER_CONFIRMATION
    return [later[i : i + size] for i in range(0, len(later) - size + 1, size)]


def identity_score(
    profile: ReferenceProfile, group: Sequence[cmu.Repetition], sessions: int
) -> float:
    fields = [run_attacks.cmu_field(r) for r in group]
    keystrokes = sum(len(field.hold) for field in fields)
    return behaviour_channel(profile, pooled_extract(fields), keystrokes, sessions).score


def invoke_aggregator(client: Any, name: str, as_of: datetime) -> dict[str, Any]:
    response = client.invoke(
        FunctionName=name,
        InvocationType="RequestResponse",
        Payload=json.dumps({"as_of": as_of.isoformat()}).encode(),
    )
    payload = json.loads(response["Payload"].read() or b"{}")
    if response.get("FunctionError"):
        raise RuntimeError(f"aggregator failed: {payload}")
    return payload


def _first(rows: Sequence[dict[str, Any]], test: Any) -> dict[str, Any] | None:
    return next((row for row in rows if test(row)), None)


def summarise(day_rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    subjects = sorted({row["subject"] for row in day_rows})
    summary = []
    for subject in subjects:
        rows = sorted((r for r in day_rows if r["subject"] == subject), key=lambda r: r["day"])
        last = rows[-1]
        flag = _first(rows, lambda r: r["edge_flagged"])
        restrict = _first(rows, lambda r: r["probe_action"] in ("restrict", "block"))
        step_up = _first(rows, lambda r: r["probe_action"] in attacks.DETECTED_ACTIONS)
        summary.append(
            {
                "subject": subject,
                "role": last["role"],
                "transfers_sent": last["transfers_sent"],
                "money_sent": last["money_sent"],
                "flag_day": flag["day"] if flag else "",
                "transfers_before_flag": flag["transfers_sent"] if flag else "",
                "money_before_flag": flag["money_sent"] if flag else "",
                "restrict_day": restrict["day"] if restrict else "",
                "money_before_restrict": restrict["money_sent"] if restrict else "",
                "step_up_day": step_up["day"] if step_up else "",
                "final_siphon_score": last["siphon_score"],
                "final_identity": last["identity"],
            }
        )
    return summary


def _write_csv(path: Path, rows: Sequence[dict[str, Any]], columns: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _delete_lake_objects(s3: Any, bucket: str, keys: Sequence[str]) -> None:
    for offset in range(0, len(keys), 1000):
        chunk = [{"Key": key} for key in keys[offset : offset + 1000]]
        if chunk:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": chunk, "Quiet": True})


def cleanup(
    aws: Any,
    out: dict[str, Any],
    accounts: dict[str, dict[str, str]],
    payee_accounts: set[str],
    lake_keys: list[str],
    day_rows: list[dict[str, Any]],
) -> None:
    table = aws.resource("dynamodb").Table(out["table_name"])
    s3 = aws.client("s3")
    sfn = aws.client("stepfunctions")
    execution_base = out["state_machine_arn"].replace(":stateMachine:", ":execution:")

    for row in day_rows:
        if row["transfer_status"] in ("awaiting_step_up", "under_review"):
            try:
                sfn.stop_execution(
                    executionArn=f"{execution_base}:{row['decision_id']}",
                    cause="simulator cleanup",
                )
            except sfn.exceptions.ExecutionDoesNotExist:
                continue

    with table.batch_writer() as batch:
        for row in day_rows:
            batch.delete_item(Key={"PK": f"DEC#{row['decision_id']}", "SK": "META"})
            batch.delete_item(Key={"PK": f"SESS#{row['session_id']}", "SK": "META"})
        for account in payee_accounts:
            batch.delete_item(Key={"PK": f"PAYEE#{attacks.payee_id(account)}", "SK": "RISK"})
    for account in accounts.values():
        for prefix in run_attacks.PARTITIONS:
            run_attacks._delete_partition(table, f"{prefix}{account['sub']}")

    _delete_lake_objects(s3, out["lake_bucket"], lake_keys)
    if day_rows:
        # The probes' own events are archived asynchronously; give them time to land first.
        time.sleep(LAKE_SETTLE_SECONDS)
        today = datetime.now(UTC).date()
        probe_keys = []
        for row in day_rows:
            for detail_type in DETAIL_TYPES:
                for day in (today, today - timedelta(days=1)):
                    prefix = f"events/type={detail_type}/dt={day:%Y-%m-%d}/{row['decision_id']}."
                    listing = s3.list_objects_v2(Bucket=out["lake_bucket"], Prefix=prefix)
                    probe_keys += [entry["Key"] for entry in listing.get("Contents", [])]
        _delete_lake_objects(s3, out["lake_bucket"], probe_keys)


def main(argv: Sequence[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser(description="Low-and-slow siphoning, replayed day by day.")
    parser.add_argument("--victims", type=int, default=5)
    parser.add_argument("--controls", type=int, default=3)
    parser.add_argument("--days", type=int, default=siphoning.ATTACK_DAYS)
    parser.add_argument("--output-dir", type=Path, default=RESULTS)
    args = parser.parse_args(argv)

    import boto3
    from botocore.config import Config

    out = outputs()
    aws = boto3.Session(region_name=out["region"])
    table = aws.resource("dynamodb").Table(out["table_name"])
    s3 = aws.client("s3")
    lambda_client = aws.client(
        "lambda", config=Config(read_timeout=330, retries={"max_attempts": 0})
    )
    data = cmu.load()
    subjects = list(data)
    victims = subjects[: args.victims]
    controls = subjects[args.victims : args.victims + args.controls]
    spare = subjects[args.victims + args.controls :]
    attackers = {victim: spare[index % len(spare)] for index, victim in enumerate(victims)}
    roles = {**{v: VICTIM for v in victims}, **{c: CONTROL for c in controls}}
    policy = AdaptationPolicy.load()
    rng = random.Random(siphoning.SEED)

    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    window_start = today - timedelta(days=siphoning.ATTACK_DAYS)

    def moment(day: float) -> datetime:
        return window_start + timedelta(days=day)

    users = Users(aws, out)
    accounts: dict[str, dict[str, str]] = {}
    payee_accounts: set[str] = set()
    lake_keys: list[str] = []
    day_rows: list[dict[str, Any]] = []
    aggregator_runs: list[dict[str, Any]] = []

    def archive(uid: str, transfer: siphoning.ScheduledTransfer, identity: float) -> None:
        payee_accounts.add(transfer.account)
        events = siphoning.lake_events(
            uid,
            secrets.token_hex(16),
            attacks.payee_id(transfer.account),
            transfer.amount,
            moment(transfer.day),
            identity,
        )
        for event in events:
            key = lake_key(event)
            body = json.dumps(event, separators=(",", ":"), sort_keys=True) + "\n"
            s3.put_object(
                Bucket=out["lake_bucket"],
                Key=key,
                Body=body.encode("utf-8"),
                ContentType="application/json",
            )
            lake_keys.append(key)

    started = datetime.now(UTC)
    try:
        profiles: dict[str, tuple[ReferenceProfile, int]] = {}
        for subject in roles:
            account = users.create(f"sim-siphon-{subject}")
            account["device_id"] = f"sim-home-{subject}"
            accounts[subject] = account
            seeded = run_attacks.baseline_items(
                account["sub"], subject, data[subject], account["device_id"],
                started.timestamp(), policy,
            )
            with table.batch_writer() as batch:
                for item in seeded:
                    batch.put_item(Item=item)
            profile_item = next(item for item in seeded if item["SK"] == "PROFILE#desktop")
            profiles[subject] = (profile_from(profile_item), int(profile_item["n_sessions"]))

            owner = typing_groups(data[subject])
            for index, transfer in enumerate(siphoning.genuine_history(subject, rng)):
                archive(account["sub"], transfer, identity_score(
                    profiles[subject][0], owner[index % len(owner)], profiles[subject][1]
                ))

        schedules = {v: siphoning.siphon_schedule() for v in victims}
        schedules |= {c: siphoning.control_schedule(c) for c in controls}
        typists = {v: typing_groups(data[attackers[v]]) for v in victims}
        typists |= {c: typing_groups(data[c]) for c in controls}
        sent: dict[str, list[float]] = {subject: [] for subject in roles}
        url = f"{out['api_url']}/score"

        for day in range(args.days):
            for subject in roles:
                profile, sessions = profiles[subject]
                for index, transfer in enumerate(schedules[subject]):
                    if int(transfer.day) == day:
                        group = typists[subject][index % len(typists[subject])]
                        archive(
                            accounts[subject]["sub"],
                            transfer,
                            identity_score(profile, group, sessions),
                        )
                        sent[subject].append(transfer.amount)

            run = invoke_aggregator(lambda_client, out["aggregator_function_name"], moment(day + 1))
            aggregator_runs.append(run)

            for subject, role in roles.items():
                account = accounts[subject]
                target = schedules[subject][0].account
                payee = attacks.payee_id(target)
                edge = table.get_item(
                    Key={"PK": f"AGG#{account['sub']}", "SK": f"EDGE#{payee}"},
                    ConsistentRead=True,
                ).get("Item", {})
                risk = table.get_item(
                    Key={"PK": f"PAYEE#{payee}", "SK": "RISK"}, ConsistentRead=True
                ).get("Item", {})

                groups = typists[subject]
                group = groups[siphoning.probe_typing_index(day, len(groups))]
                session_id = f"sim-{secrets.token_hex(16)}"
                body = {
                    "session_id": session_id,
                    "checkpoint": "confirmation",
                    "device": {
                        "device_id": account["device_id"],
                        "max_touch_points": 0,
                        "coarse_pointer": False,
                        "short_side_px": 1080,
                    },
                    "behaviour": {"fields": [cmu.field(r) for r in group]},
                    "transaction": {"payee_id": payee, "amount": siphoning.SIPHON_AMOUNT},
                }
                reply = post(url, body, account["token"])
                if reply.status != 200:
                    raise RuntimeError(f"probe for {subject}: {reply.status} {reply.body}")
                decision = reply.body
                contributions = {c["channel"]: c["value"] for c in decision["contributions"]}
                day_rows.append(
                    {
                        "day": day + 1,
                        "subject": subject,
                        "role": role,
                        "transfers_sent": len(sent[subject]),
                        "money_sent": round(sum(sent[subject]), 2),
                        "siphon_score": float(edge.get("siphon_score", 0)),
                        "volume": float(edge.get("volume", 0)),
                        "regularity": float(edge.get("regularity", 0)),
                        "band": float(edge.get("band", 0)),
                        "identity": float(edge.get("identity", 0)),
                        "edge_flagged": bool(edge.get("flagged", False)),
                        "payee_risk": float(risk.get("risk_score", 0)),
                        "payee_flagged": bool(risk.get("flagged", False)),
                        "probe_action": decision["action"],
                        "probe_risk": decision["risk"],
                        "probe_payee_contribution": contributions.get("payee", ""),
                        "probe_constraints": "|".join(decision["constraints"]),
                        "decision_id": decision["decision_id"],
                        "session_id": session_id,
                        "transfer_status": decision.get("transfer", {}).get("status", ""),
                    }
                )
            flagged = sum(row["edge_flagged"] for row in day_rows if row["day"] == day + 1)
            print(f"day {day + 1}: {run.get('transfers')} transfers in lake, {flagged} flagged")
        finished = datetime.now(UTC)
    finally:
        try:
            cleanup(aws, out, accounts, payee_accounts, lake_keys, day_rows)
        finally:
            users.close()

    summary = summarise(day_rows)
    victims_flagged = [r for r in summary if r["role"] == VICTIM and r["flag_day"] != ""]
    controls_flagged = [r for r in summary if r["role"] == CONTROL and r["flag_day"] != ""]
    victims_restricted = [r for r in summary if r["role"] == VICTIM and r["restrict_day"] != ""]
    controls_restricted = [r for r in summary if r["role"] == CONTROL and r["restrict_day"] != ""]
    result = {
        "started": started.isoformat(),
        "finished": finished.isoformat(),
        "window_start": window_start.isoformat(),
        "days": args.days,
        "victims": len(victims),
        "controls": len(controls),
        "victims_flagged": len(victims_flagged),
        "controls_flagged": len(controls_flagged),
        "victims_restricted": len(victims_restricted),
        "controls_restricted": len(controls_restricted),
        "aggregator_runs": len(aggregator_runs),
        "probe_calls": len(day_rows),
        "lake_objects_replayed": len(lake_keys),
        "seed": siphoning.SEED,
    }

    _write_csv(args.output_dir / "phase9_siphoning.csv", summary, USER_COLUMNS)
    _write_csv(args.output_dir / "phase9_siphoning_days.csv", day_rows, DAY_COLUMNS)
    (args.output_dir / "phase9_siphoning_run.json").write_text(
        json.dumps(result, indent=2) + "\n", "utf-8"
    )
    for row in summary:
        print(row)
    print(result)
    return {"run": result, "summary": summary, "emails": [a["email"] for a in accounts.values()]}


if __name__ == "__main__":
    main()
