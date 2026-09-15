"""Tests for the aggregator Lambda: the Athena round trip, row parsing and the items it writes."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from aggregator.handler import Dependencies, QueryFailed, handle, records, run_query
from aggregator.query import transfers_query

AS_OF = datetime(2026, 9, 15, tzinfo=UTC)
NOW = AS_OF.timestamp()
DAY = 86_400
HEADER = ["uid", "payee_id", "amount", "at", "score"]


def _page(rows: list[list[str | None]], token: str | None = None, header: bool = True) -> dict:
    data = ([HEADER] if header else []) + rows
    page: dict[str, Any] = {
        "ResultSet": {
            "Rows": [
                {"Data": [{} if value is None else {"VarCharValue": value} for value in row]}
                for row in data
            ]
        }
    }
    if token:
        page["NextToken"] = token
    return page


class FakeAthena:
    def __init__(self, states: list[str], pages: list[dict]) -> None:
        self.states = list(states)
        self.pages = list(pages)
        self.sql = ""
        self.stopped = False

    def start_query_execution(self, **kwargs: Any) -> dict:
        self.sql = kwargs["QueryString"]
        assert kwargs["WorkGroup"] == "bfd-batch"
        return {"QueryExecutionId": "q1"}

    def get_query_execution(self, **kwargs: Any) -> dict:
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return {"QueryExecution": {"Status": {"State": state, "StateChangeReason": "bad"}}}

    def get_query_results(self, **kwargs: Any) -> dict:
        return self.pages.pop(0)

    def stop_query_execution(self, **kwargs: Any) -> None:
        self.stopped = True


class FakeTable:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def put_item(self, Item: dict[str, Any]) -> None:  # noqa: N803
        self.items.append(Item)


def _deps(athena: FakeAthena, table: FakeTable | None = None, **overrides: Any) -> Dependencies:
    options: dict[str, Any] = {
        "athena": athena,
        "table": table or FakeTable(),
        "workgroup": "bfd-batch",
        "database": "bfd_lake",
        "lake_table": "events",
        "clock": lambda: NOW + 3600,
        "sleep": lambda seconds: None,
    }
    options.update(overrides)
    return Dependencies(**options)


class TestRunQuery:
    def test_rows_from_every_page_without_the_header(self) -> None:
        pages = [
            _page([["u", "p", "10.0", "1.0", "5.0"]], token="next"),
            _page([["u", "p", "20.0", "2.0", None]], header=False),
        ]
        athena = FakeAthena(["QUEUED", "RUNNING", "SUCCEEDED"], pages)
        rows = run_query(_deps(athena), "SELECT 1")
        assert rows == [["u", "p", "10.0", "1.0", "5.0"], ["u", "p", "20.0", "2.0", None]]

    def test_a_failed_query_raises(self) -> None:
        with pytest.raises(QueryFailed, match="FAILED"):
            run_query(_deps(FakeAthena(["FAILED"], [])), "SELECT 1")

    def test_a_query_past_its_deadline_is_stopped(self) -> None:
        ticks = iter([0.0, 1_000.0])
        athena = FakeAthena(["RUNNING"], [])
        with pytest.raises(QueryFailed, match="timed out"):
            run_query(_deps(athena, monotonic=lambda: next(ticks)), "SELECT 1")
        assert athena.stopped


def test_malformed_rows_are_skipped() -> None:
    parsed = records(
        [
            ["u", "p", "900.0", "1788000000.0", ""],
            ["u", "p", None, "1788000000.0", "3.0"],
            ["u", "p", "-5", "1788000000.0", "3.0"],
            ["u", "p", "900.0", "1788000000.0", "12.5"],
        ]
    )
    assert [record.identity_score for record in parsed] == [None, 12.5]


class TestQuery:
    def test_the_scan_is_bounded_by_partition_dates(self) -> None:
        sql = transfers_query("bfd_lake", "events", datetime(2026, 6, 17), datetime(2026, 9, 15))
        assert "dt BETWEEN '2026-06-17' AND '2026-09-15'" in sql
        assert '"bfd_lake"."events"' in sql
        assert "detail.status = 'released'" in sql

    @pytest.mark.parametrize("name", ["bfd-lake", "events; DROP", "", "Events"])
    def test_catalog_names_are_validated(self, name: str) -> None:
        with pytest.raises(ValueError):
            transfers_query(name, "events", AS_OF, AS_OF)


def _siphon_rows() -> list[list[str | None]]:
    rows = []
    for k in range(8):
        rows.append(["victim", f"known{k % 3}", "1500.0", str(NOW - (60 - 5 * k) * DAY), "20.0"])
    for k in range(10):
        rows.append(["victim", "mule", "900.0", str(NOW - (18 - 2 * k) * DAY), "40.0"])
    return rows


def test_a_run_writes_aggregates_edge_signals_and_payee_risk() -> None:
    table = FakeTable()
    athena = FakeAthena(["SUCCEEDED"], [_page(_siphon_rows())])
    summary = handle({"as_of": "2026-09-15T00:00:00Z"}, _deps(athena, table))

    assert "dt BETWEEN '2026-06-17' AND '2026-09-15'" in athena.sql
    by_key = {(item["PK"], item["SK"]): item for item in table.items}

    window = by_key[("AGG#victim", "WINDOW#30d")]
    assert window["history_count"] == 11
    assert isinstance(window["amount_p95"], Decimal)
    assert len(window["hour_histogram"]) == 24

    edge = by_key[("AGG#victim", "EDGE#mule")]
    assert edge["flagged"] is True
    assert edge["siphon_score"] == Decimal("1.0")
    # computed_at is when the run happened, not the as_of it replayed.
    assert edge["computed_at"] == int(NOW + 3600)
    assert edge["ttl"] > edge["computed_at"]

    assert by_key[("PAYEE#mule", "RISK")]["flagged"] is True
    assert summary["flagged_edges"] == 1
    assert summary["items"] == len(table.items)


def test_no_as_of_computes_as_of_the_clock() -> None:
    athena = FakeAthena(["SUCCEEDED"], [_page([])])
    summary = handle({}, _deps(athena, clock=lambda: NOW))
    assert summary["as_of"] == AS_OF.isoformat()
    assert summary["items"] == 0
