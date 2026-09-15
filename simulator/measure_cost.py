"""Phase 8 cost: measured usage during the attack run, priced, per thousand sessions.

Usage is read from CloudWatch for the run window recorded by run_attacks.py, plus a few minutes
after it for the asynchronous tail (archive, adaptation). State transitions have no metric, so they
are counted from the execution histories of the workflows the run started.

Prices are list prices for ap-south-1 with no free tier, held in one table so they can be checked
and changed in one place. VERIFY every one against the AWS pricing pages before the figure goes in
the report; they are recorded here from secondary knowledge, not fetched.

Deliberately excluded, and stated as excluded: Cognito (billed per monthly active user, not per
session), CloudFront (serving the static client), KMS requests (DynamoDB caches data keys and the
lake uses an S3 bucket key, so request counts are negligible at this volume), and the fixed monthly
cost of the two customer-managed keys.

Writes research/results/tables/phase8_cost.csv.

Usage, from the repository root::

    python simulator/measure_cost.py
"""

from __future__ import annotations

import csv
import json
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from simulator.stack import RESULTS, outputs  # noqa: E402

TAIL = timedelta(minutes=10)
FUNCTIONS = ("scoring", "transfers", "ledger", "archive", "adaptation", "console")

# (usage key, unit, USD per unit). VERIFY against current ap-south-1 pricing before reporting.
PRICES: tuple[tuple[str, str, float], ...] = (
    ("lambda_requests", "request", 0.20 / 1_000_000),
    ("lambda_gb_seconds", "GB-second (arm64)", 0.0000133334),
    ("api_requests", "HTTP API request", 1.00 / 1_000_000),
    ("state_transitions", "Standard state transition", 0.025 / 1000),
    ("dynamodb_write_units", "on-demand write request unit", 0.7115 / 1_000_000),
    ("dynamodb_read_units", "on-demand read request unit", 0.1423 / 1_000_000),
    ("events_published", "custom event", 1.00 / 1_000_000),
    ("s3_puts", "PUT request", 0.005 / 1000),
    ("sns_publishes", "publish", 0.50 / 1_000_000),
    ("log_gigabytes", "GB ingested", 0.67),
    ("xray_traces", "trace recorded", 5.00 / 1_000_000),
)


def price_rows(usage: Mapping[str, float], sessions: int) -> list[dict[str, Any]]:
    """Cost per component and per thousand sessions, with a total row."""
    if sessions <= 0:
        raise ValueError("sessions must be positive")
    rows = []
    for key, unit, price in PRICES:
        amount = float(usage.get(key, 0.0))
        cost = amount * price
        rows.append(
            {
                "component": key,
                "usage": round(amount, 4),
                "unit": unit,
                "unit_price_usd": price,
                "cost_usd": round(cost, 8),
                "per_1000_sessions_usd": round(cost / sessions * 1000, 6),
            }
        )
    total = sum(row["cost_usd"] for row in rows)
    rows.append(
        {
            "component": "total",
            "usage": sessions,
            "unit": "sessions",
            "unit_price_usd": "",
            "cost_usd": round(total, 8),
            "per_1000_sessions_usd": round(total / sessions * 1000, 6),
        }
    )
    return rows


def _metric_sum(
    cloudwatch: Any,
    namespace: str,
    metric: str,
    dimensions: list[dict[str, str]],
    start: datetime,
    end: datetime,
) -> float:
    response = cloudwatch.get_metric_statistics(
        Namespace=namespace,
        MetricName=metric,
        Dimensions=dimensions,
        StartTime=start,
        EndTime=end,
        Period=60,
        Statistics=["Sum"],
    )
    return float(sum(point["Sum"] for point in response.get("Datapoints", [])))


def collect_usage(
    aws: Any, out: dict[str, Any], start: datetime, end: datetime
) -> dict[str, float]:
    cloudwatch = aws.client("cloudwatch")
    lambda_client = aws.client("lambda")
    usage: dict[str, float] = {"lambda_requests": 0.0, "lambda_gb_seconds": 0.0}

    for name in FUNCTIONS:
        function = f"bfd-{name}"
        dimension = [{"Name": "FunctionName", "Value": function}]
        invocations = _metric_sum(cloudwatch, "AWS/Lambda", "Invocations", dimension, start, end)
        duration_ms = _metric_sum(cloudwatch, "AWS/Lambda", "Duration", dimension, start, end)
        memory_mb = lambda_client.get_function_configuration(FunctionName=function)["MemorySize"]
        usage["lambda_requests"] += invocations
        usage["lambda_gb_seconds"] += duration_ms / 1000 * memory_mb / 1024
        usage[f"invocations_{name}"] = invocations
        if name == "archive":
            # One event on the bus, one object in the lake, per archive invocation.
            usage["events_published"] = invocations
            usage["s3_puts"] = invocations

    api_id = out["api_url"].split("//", 1)[1].split(".", 1)[0]
    usage["api_requests"] = _metric_sum(
        cloudwatch, "AWS/ApiGateway", "Count", [{"Name": "ApiId", "Value": api_id}], start, end
    )
    table = [{"Name": "TableName", "Value": out["table_name"]}]
    usage["dynamodb_read_units"] = _metric_sum(
        cloudwatch, "AWS/DynamoDB", "ConsumedReadCapacityUnits", table, start, end
    )
    usage["dynamodb_write_units"] = _metric_sum(
        cloudwatch, "AWS/DynamoDB", "ConsumedWriteCapacityUnits", table, start, end
    )
    topic = out["alerts_topic_arn"].rsplit(":", 1)[1]
    usage["sns_publishes"] = _metric_sum(
        cloudwatch,
        "AWS/SNS",
        "NumberOfMessagesPublished",
        [{"Name": "TopicName", "Value": topic}],
        start,
        end,
    )
    usage["log_gigabytes"] = sum(
        _metric_sum(
            cloudwatch,
            "AWS/Logs",
            "IncomingBytes",
            [{"Name": "LogGroupName", "Value": f"/aws/lambda/bfd-{name}"}],
            start,
            end,
        )
        for name in FUNCTIONS
    ) / 1024**3
    usage["xray_traces"] = usage["lambda_requests"]
    usage["state_transitions"] = count_transitions(aws, out["state_machine_arn"], start, end)
    return usage


def count_transitions(aws: Any, state_machine_arn: str, start: datetime, end: datetime) -> float:
    sfn = aws.client("stepfunctions")
    transitions = 0
    request: dict[str, Any] = {"stateMachineArn": state_machine_arn, "maxResults": 1000}
    while True:
        page = sfn.list_executions(**request)
        for execution in page.get("executions", []):
            if not start <= execution["startDate"] <= end:
                continue
            history: dict[str, Any] = {
                "executionArn": execution["executionArn"],
                "maxResults": 1000,
            }
            while True:
                events = sfn.get_execution_history(**history)
                transitions += sum(e["type"].endswith("StateEntered") for e in events["events"])
                if not events.get("nextToken"):
                    break
                history["nextToken"] = events["nextToken"]
        if not page.get("nextToken"):
            break
        request["nextToken"] = page["nextToken"]
    return float(transitions)


def main(argv: Sequence[str] | None = None) -> int:
    import boto3

    run = json.loads((RESULTS / "phase8_run.json").read_text(encoding="utf-8"))
    start = datetime.fromisoformat(run["started"]) - timedelta(minutes=1)
    end = datetime.fromisoformat(run["finished"]) + TAIL

    out = outputs()
    aws = boto3.Session(region_name=out["region"])
    usage = collect_usage(aws, out, start, end)
    rows = price_rows(usage, int(run["sessions"]))

    path = RESULTS / "phase8_cost.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(path.read_text(encoding="utf-8"))
    print({key: value for key, value in usage.items() if key.startswith("invocations_")})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
