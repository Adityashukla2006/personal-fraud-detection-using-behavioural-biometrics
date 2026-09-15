"""Aggregator Lambda: the slow plane's batch job (architecture section 5.4).

Reads released transfers and their confirmation identity scores back from the audit lake with one
Athena query, hands them to ``fraudcore.batch``, and writes the results where the fast path reads
them by key:

    AGG#<uid> / WINDOW#30d     the user's rolling amount, count and hour statistics
    AGG#<uid> / EDGE#<pid>     siphoning signals for a payee this user started paying recently
    PAYEE#<pid> / RISK         cross-user destination risk and the batch flag

Every decision stays in fraudcore; this module only runs the query and translates rows and items.

EventBridge Scheduler invokes it nightly with an empty event, which computes as of now. An ``as_of``
timestamp computes the state as it stood then, which is how the siphoning simulator replays weeks of
history in minutes. ``computed_at`` is always the real time of the run, because it is what the fast
path ages a signal by.

Every item carries a TTL a few nightly runs long. A user or payee that drops out of the lake's
window simply expires, with no delete pass to maintain.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from aggregator.query import transfers_query
from fraudcore.batch import LOOKBACK_DAYS, BatchResult, TransferRecord, compute

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

POLL_SECONDS = 1.0
QUERY_TIMEOUT_SECONDS = 240
ITEM_TTL_SECONDS = 7 * 86_400
AGGREGATE_WINDOW = "30d"


class QueryFailed(Exception):
    """The Athena query did not succeed."""


@dataclass(frozen=True)
class Dependencies:
    athena: Any
    table: Any
    workgroup: str
    database: str
    lake_table: str
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = field(default=time.monotonic)


def parse_as_of(event: Mapping[str, Any], clock: Callable[[], float]) -> datetime:
    value = event.get("as_of") if isinstance(event, Mapping) else None
    if value is None:
        return datetime.fromtimestamp(clock(), UTC)
    if not isinstance(value, str):
        raise ValueError("as_of must be an ISO 8601 string")
    moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def run_query(deps: Dependencies, sql: str) -> list[list[str | None]]:
    """Run one query to completion and return its rows, header excluded."""
    athena = deps.athena
    execution = athena.start_query_execution(QueryString=sql, WorkGroup=deps.workgroup)[
        "QueryExecutionId"
    ]
    deadline = deps.monotonic() + QUERY_TIMEOUT_SECONDS
    while True:
        status = athena.get_query_execution(QueryExecutionId=execution)["QueryExecution"]["Status"]
        state = status["State"]
        if state == "SUCCEEDED":
            break
        if state in ("FAILED", "CANCELLED"):
            raise QueryFailed(f"{state}: {status.get('StateChangeReason', '')}")
        if deps.monotonic() > deadline:
            athena.stop_query_execution(QueryExecutionId=execution)
            raise QueryFailed("timed out")
        deps.sleep(POLL_SECONDS)

    rows: list[list[str | None]] = []
    request: dict[str, Any] = {"QueryExecutionId": execution}
    first_page = True
    while True:
        page = athena.get_query_results(**request)
        data = page["ResultSet"]["Rows"]
        # Athena returns the column names as the first row of the first page only.
        if first_page:
            data, first_page = data[1:], False
        rows += [[cell.get("VarCharValue") for cell in row["Data"]] for row in data]
        if not page.get("NextToken"):
            return rows
        request["NextToken"] = page["NextToken"]


def records(rows: Sequence[Sequence[str | None]]) -> list[TransferRecord]:
    """Rows as transfer records. A malformed row is skipped and logged, never fatal."""
    parsed = []
    for row in rows:
        try:
            uid, payee_id, amount, at, identity = row
            parsed.append(
                TransferRecord(
                    uid=str(uid or ""),
                    payee_id=str(payee_id or ""),
                    amount=float(amount or "nan"),
                    at=float(at or "nan"),
                    identity_score=None if identity in (None, "") else float(identity),
                )
            )
        except (TypeError, ValueError):
            LOGGER.warning("skipping malformed row %r", row)
    return parsed


def _number(value: float) -> Decimal:
    return Decimal(repr(round(float(value), 6)))


def items(result: BatchResult, computed_at: float) -> list[dict[str, Any]]:
    stamp = int(computed_at)
    expiry = stamp + ITEM_TTL_SECONDS
    written: list[dict[str, Any]] = []

    for uid, aggregates in result.aggregates.items():
        written.append(
            {
                "PK": f"AGG#{uid}",
                "SK": f"WINDOW#{AGGREGATE_WINDOW}",
                "amount_p50": _number(aggregates.amount_p50),
                "amount_p95": _number(aggregates.amount_p95),
                "daily_count_p95": _number(aggregates.daily_count_p95),
                "hour_histogram": [_number(count) for count in aggregates.hour_histogram],
                "history_count": aggregates.history_count,
                "computed_at": stamp,
                "ttl": expiry,
            }
        )

    for edge in result.edges:
        item: dict[str, Any] = {
            "PK": f"AGG#{edge.uid}",
            "SK": f"EDGE#{edge.payee_id}",
            "siphon_score": _number(edge.siphon_score),
            "flagged": edge.flagged,
            "transfers": edge.transfers,
            "cumulative": _number(edge.cumulative),
            "volume": _number(edge.volume),
            "regularity": _number(edge.regularity),
            "band": _number(edge.band),
            "identity": _number(edge.identity),
            "first_seen": int(edge.first_seen),
            "computed_at": stamp,
            "ttl": expiry,
        }
        if edge.interval_cv is not None:
            item["interval_cv"] = _number(edge.interval_cv)
        if edge.identity_gap is not None:
            item["identity_gap"] = _number(edge.identity_gap)
        written.append(item)

    for payee in result.payees:
        written.append(
            {
                "PK": f"PAYEE#{payee.payee_id}",
                "SK": "RISK",
                "risk_score": _number(payee.risk_score),
                "flagged": payee.flagged,
                "distinct_senders_7d": payee.distinct_senders_7d,
                "distinct_senders_30d": payee.distinct_senders_30d,
                "new_sender_fraction": _number(payee.new_sender_fraction),
                "computed_at": stamp,
                "ttl": expiry,
            }
        )
    return written


def handle(event: Mapping[str, Any], deps: Dependencies) -> dict[str, Any]:
    as_of = parse_as_of(event, deps.clock)
    sql = transfers_query(
        deps.database, deps.lake_table, as_of - timedelta(days=LOOKBACK_DAYS), as_of
    )
    transfers = records(run_query(deps, sql))
    result = compute(transfers, as_of.timestamp())

    # PutItem one at a time: the role is granted PutItem by key prefix, not BatchWriteItem.
    written = items(result, deps.clock())
    for item in written:
        deps.table.put_item(Item=item)

    summary = {
        "as_of": as_of.isoformat(),
        "transfers": len(transfers),
        "users": len(result.aggregates),
        "edges": len(result.edges),
        "flagged_edges": sum(edge.flagged for edge in result.edges),
        "payees": len(result.payees),
        "flagged_payees": sum(payee.flagged for payee in result.payees),
        "items": len(written),
    }
    LOGGER.info("aggregated %s", summary)
    return summary


def _build() -> Dependencies:
    import boto3

    return Dependencies(
        athena=boto3.client("athena"),
        table=boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"]),
        workgroup=os.environ["ATHENA_WORKGROUP"],
        database=os.environ["GLUE_DATABASE"],
        lake_table=os.environ["GLUE_TABLE"],
    )


_dependencies: Dependencies | None = (
    _build() if os.environ.get("AWS_LAMBDA_FUNCTION_NAME") else None
)


def lambda_handler(event: Mapping[str, Any], context: Any) -> dict[str, Any]:
    global _dependencies
    if _dependencies is None:
        _dependencies = _build()
    return handle(event or {}, _dependencies)
