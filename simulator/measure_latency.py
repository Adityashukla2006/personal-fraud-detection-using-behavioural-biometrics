"""Phase 8 latency: warm and cold, end to end and broken down by stage.

Three sources, because no single one sees everything:

    this machine     the round trip of every call, including the internet path to ap-south-1
    Server-Timing    the handler's own stages: read, score, write, publish, workflow
    X-Ray            the Lambda service's view of the scoring function: its init on a cold start,
                     the function's own run, the runtime overhead, and the service total

HTTP APIs do not support X-Ray, so API Gateway's share is reported as the round trip minus the
handler, labelled as API Gateway plus network. On a cold request that difference also contains the
function's init, which X-Ray reports separately.

One X-Ray trace covers the whole request, including the ledger, archive and workflow it fans out
to, so only segments named for the scoring function are read.

Warm requests follow a warm-up, at the deployed memory size. Cold starts are forced: changing the
function's description makes Lambda retire its execution environments, so the next request starts
a fresh one. They are measured at each memory size in ``--cold-memory``, because Lambda allocates
CPU in proportion to memory and init is CPU-bound; each condition is labelled ``cold_<MB>mb``. The
description and memory size are restored at the end.

Writes research/results/tables/phase8_latency.csv and cleans up its user and every item it created.

Usage, from the repository root::

    python simulator/measure_latency.py [--warm 100] [--confirmations 30] [--cold 10]
                                        [--cold-memory 512 1024]
"""

from __future__ import annotations

import argparse
import csv
import json
import secrets
import sys
import time
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (REPO_ROOT / "src", REPO_ROOT / "src" / "lambdas", REPO_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from simulator import attacks, cmu  # noqa: E402
from simulator.stack import RESULTS, Users, outputs, percentile, post  # noqa: E402

WARMUP = 5
TRACE_WAIT_SECONDS = 120
HANDLER_STAGES = ("handler", "read", "score", "write", "publish", "workflow")


def _milliseconds(node: dict[str, Any]) -> float:
    return round((node["end_time"] - node["start_time"]) * 1000, 1)


def lambda_breakdown(document: dict[str, Any], function: str) -> dict[str, float]:
    """One trace's view of ``function``, in milliseconds.

    ``service`` is the Lambda service segment (origin ``AWS::Lambda``): the whole invocation as the
    service saw it. ``function`` is the function segment (origin ``AWS::Lambda::Function``), and its
    ``Init`` and ``Overhead`` subsegments give ``init``, present only on a cold start, and
    ``overhead``. Segments belonging to any other function in the trace are ignored.
    """
    breakdown: dict[str, float] = {}
    for segment in document.get("Segments", []):
        body = json.loads(segment["Document"])
        if body.get("name") != function or "end_time" not in body:
            continue
        if body.get("origin") == "AWS::Lambda":
            breakdown["service"] = _milliseconds(body)
        elif body.get("origin") == "AWS::Lambda::Function":
            breakdown["function"] = _milliseconds(body)
            for sub in body.get("subsegments", []):
                if sub.get("name") in ("Init", "Overhead") and "end_time" in sub:
                    breakdown[sub["name"].lower()] = _milliseconds(sub)
    return breakdown


def trace_start(document: dict[str, Any], function: str) -> float | None:
    """When ``function``'s earliest segment in the trace started, as an epoch second."""
    starts = [
        body["start_time"]
        for body in (json.loads(segment["Document"]) for segment in document.get("Segments", []))
        if body.get("name") == function and "start_time" in body
    ]
    return min(starts) if starts else None


def summarise(samples: dict[tuple[str, str], list[float]]) -> list[dict[str, Any]]:
    rows = []
    for (condition, measure), values in sorted(samples.items()):
        if values:
            rows.append(
                {
                    "condition": condition,
                    "measure": measure,
                    "samples": len(values),
                    "p50_ms": round(percentile(values, 0.50), 1),
                    "p95_ms": round(percentile(values, 0.95), 1),
                    "max_ms": round(max(values), 1),
                }
            )
    return rows


def record(samples: dict[tuple[str, str], list[float]], condition: str, reply: Any) -> None:
    samples[(condition, "round_trip")].append(reply.round_trip_ms)
    handler = reply.server_timing.get("handler")
    if handler is not None:
        samples[(condition, "api_gateway_and_network")].append(reply.round_trip_ms - handler)
    for stage in HANDLER_STAGES:
        if stage in reply.server_timing:
            samples[(condition, stage)].append(reply.server_timing[stage])


def traces(aws: Any, function: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    xray = aws.client("xray")
    ids: list[str] = []
    request: dict[str, Any] = {
        "StartTime": start,
        "EndTime": end,
        "FilterExpression": f'service("{function}")',
    }
    while True:
        page = xray.get_trace_summaries(**request)
        ids += [summary["Id"] for summary in page.get("TraceSummaries", [])]
        if not page.get("NextToken"):
            break
        request["NextToken"] = page["NextToken"]
    documents = []
    for offset in range(0, len(ids), 5):
        documents += xray.batch_get_traces(TraceIds=ids[offset : offset + 5])["Traces"]
    return documents


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Warm and cold latency, by stage.")
    parser.add_argument("--warm", type=int, default=100)
    parser.add_argument("--confirmations", type=int, default=30)
    parser.add_argument("--cold", type=int, default=10)
    parser.add_argument("--cold-memory", type=int, nargs="*", default=[512, 1024])
    args = parser.parse_args(argv)

    import boto3
    from boto3.dynamodb.conditions import Key

    out = outputs()
    aws = boto3.Session(region_name=out["region"])
    lambda_client = aws.client("lambda")
    table = aws.resource("dynamodb").Table(out["table_name"])
    function = out["scoring_function_name"]
    url = f"{out['api_url']}/score"
    _, reps = next(iter(cmu.load().items()))
    human = [cmu.field(rep) for rep in reps]
    known = {"payee_id": attacks.payee_id("latency-probe"), "amount": 25.0}

    users = Users(aws, out)
    samples: dict[tuple[str, str], list[float]] = defaultdict(list)
    windows: list[tuple[str, datetime, datetime]] = []
    created: list[tuple[str, str]] = []
    account: dict[str, str] = {}
    configuration = lambda_client.get_function_configuration(FunctionName=function)
    original_description = configuration.get("Description", "")
    original_memory = configuration["MemorySize"]

    def settle() -> None:
        lambda_client.get_waiter("function_updated_v2").wait(FunctionName=function)

    def call(checkpoint: str, index: int) -> Any:
        session = f"sim-{secrets.token_hex(16)}"
        body: dict[str, Any] = {
            "session_id": session,
            "checkpoint": checkpoint,
            "device": {
                "device_id": "sim-latency-probe",
                "max_touch_points": 0,
                "coarse_pointer": False,
                "short_side_px": 1080,
            },
            "behaviour": {"fields": [human[index % len(human)]]},
        }
        if checkpoint == "confirmation":
            body["transaction"] = known
        reply = post(url, body, account["token"])
        if reply.status != 200:
            raise RuntimeError(f"{checkpoint} returned {reply.status}: {reply.body}")
        created.extend([(f"SESS#{session}", "META"), (f"DEC#{reply.body['decision_id']}", "META")])
        return reply

    try:
        account.update(users.create("sim-latency"))
        began = datetime.now(UTC)
        for index in range(WARMUP):
            call("login", index)
        for index in range(args.warm):
            record(samples, "warm", call("login", index))
        for index in range(args.confirmations):
            record(samples, "warm_confirmation", call("confirmation", index))
        windows.append(("warm", began, datetime.now(UTC)))

        for memory in args.cold_memory:
            lambda_client.update_function_configuration(FunctionName=function, MemorySize=memory)
            settle()
            condition = f"cold_{memory}mb"
            began = datetime.now(UTC)
            for index in range(args.cold):
                lambda_client.update_function_configuration(
                    FunctionName=function,
                    Description=f"cold start probe {memory}mb {index} {time.time():.0f}",
                )
                settle()
                record(samples, condition, call("login", index))
            windows.append((condition, began, datetime.now(UTC)))
    finally:
        lambda_client.update_function_configuration(
            FunctionName=function, Description=original_description, MemorySize=original_memory
        )
        settle()
        users.close()
        with table.batch_writer() as batch:
            for partition, sort in created:
                batch.delete_item(Key={"PK": partition, "SK": sort})
        if account:
            for prefix in ("USER#", "LEDGER#", "REPLAY#"):
                condition_key = Key("PK").eq(f"{prefix}{account['sub']}")
                for item in table.query(KeyConditionExpression=condition_key)["Items"]:
                    table.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})

    # X-Ray indexes traces a little after the requests; wait until the count stops growing.
    margin = timedelta(seconds=5)
    first, last = windows[0][1], windows[-1][2]
    deadline = time.monotonic() + TRACE_WAIT_SECONDS
    documents: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        time.sleep(15)
        latest = traces(aws, function, first - margin, last + margin)
        if documents and len(latest) == len(documents):
            break
        documents = latest

    for document in documents:
        breakdown = lambda_breakdown(document, function)
        started = trace_start(document, function)
        if started is None or not breakdown:
            continue
        condition = next(
            (
                name
                for name, low, high in windows
                if low.timestamp() - 5 <= started <= high.timestamp() + 5
            ),
            None,
        )
        if condition is None:
            continue
        # Inside a cold window only the probe requests are cold; anything without init is not.
        if condition.startswith("cold") and "init" not in breakdown:
            continue
        for name, value in breakdown.items():
            samples[(condition, f"lambda_{name}")].append(value)

    rows = summarise(samples)
    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / "phase8_latency.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(path.read_text(encoding="utf-8"))
    print(f"{len(documents)} X-Ray traces; windows: " + ", ".join(
        f"{name} {low:%H:%M:%S}-{high:%H:%M:%S}" for name, low, high in windows
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
