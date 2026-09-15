"""Tests for the simulator's measurement helpers: timing parsing, percentiles, detection summaries,
baseline seeding, X-Ray breakdown and cost arithmetic. No AWS, no network."""

from __future__ import annotations

import json

import pytest

from adaptation.store import decode_profile
from fraudcore.adaptation import AdaptationPolicy
from simulator import attacks, measure_cost, measure_latency, run_attacks, stack
from simulator.cmu import Repetition


class TestServerTiming:
    def test_every_stage_is_parsed(self) -> None:
        header = "handler;dur=14.2, read;dur=6.1, score;dur=1.0, workflow;dur=22.5"
        assert stack.parse_server_timing(header) == {
            "handler": 14.2,
            "read": 6.1,
            "score": 1.0,
            "workflow": 22.5,
        }

    def test_a_missing_header_is_empty(self) -> None:
        assert stack.parse_server_timing(None) == {}


class TestPercentile:
    def test_nearest_rank_is_an_observed_value(self) -> None:
        values = [5.0, 1.0, 4.0, 2.0, 3.0]
        assert stack.percentile(values, 0.5) == 3.0
        assert stack.percentile(values, 0.95) == 5.0
        assert stack.percentile([7.0], 0.95) == 7.0

    def test_no_values_is_an_error(self) -> None:
        with pytest.raises(ValueError):
            stack.percentile([], 0.5)


def _row(attack: str, checkpoint: str, action: str, risk: float | None = 50.0) -> dict:
    return {
        "attack": attack,
        "checkpoint": checkpoint,
        "action": action,
        "risk": risk,
        "constraints": "fail_open" if risk is None else "",
    }


def test_detection_is_summarised_from_confirmations_only() -> None:
    rows = [
        _row("takeover", "login", "allow"),
        _row("takeover", "confirmation", "block", 99.0),
        _row("takeover", "confirmation", "step_up", 60.0),
        _row("takeover", "confirmation", "allow", 10.0),
        _row("genuine", "confirmation", "monitor", None),
    ]
    summary = {entry["attack"]: entry for entry in run_attacks.summarise(rows)}
    takeover = summary["takeover"]
    assert takeover["sessions"] == 3
    assert takeover["detection_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert takeover["block_rate"] == pytest.approx(0.3333)
    assert takeover["allow_rate"] == pytest.approx(0.3333)
    assert takeover["mean_risk"] == pytest.approx(56.33)
    assert summary["genuine"]["fail_open"] == 1
    assert summary["genuine"]["mean_risk"] == ""


def _reps(subject: str) -> list[Repetition]:
    reps = []
    for session in range(1, 9):
        for rep in range(1, 51):
            hold = tuple(0.08 + 0.002 * ((k + rep) % 7) for k in range(11))
            down_down = tuple(0.2 + 0.01 * ((k * 3 + rep) % 5) for k in range(10))
            up_down = tuple(d - h for d, h in zip(down_down, hold, strict=False))
            reps.append(Repetition(subject, session, rep, hold, down_down, up_down))
    return reps


def test_the_seeded_baseline_is_what_the_live_system_reads() -> None:
    policy = AdaptationPolicy.load()
    items = run_attacks.baseline_items("uid-1", "s002", _reps("s002"), "sim-home-s002", 1e9, policy)
    by_key = {(item["PK"], item["SK"]): item for item in items}

    profile = decode_profile(by_key[("USER#uid-1", "PROFILE#desktop")])
    assert profile.version == 1
    assert by_key[("USER#uid-1", "PROFILE#desktop")]["n_sessions"] == 12
    payee = attacks.payee_id(attacks.known_payee("s002"))
    assert ("USER#uid-1", f"PAYEE#{payee}") in by_key
    assert ("AGG#uid-1", "WINDOW#30d") in by_key
    assert by_key[("USER#uid-1", "DEV#sim-home-s002")]["session_count"] == 30


def _segment(
    name: str, origin: str, start: float, end: float, subsegments: list | None = None
) -> dict:
    body = {"name": name, "origin": origin, "start_time": start, "end_time": end}
    if subsegments is not None:
        body["subsegments"] = subsegments
    return {"Document": json.dumps(body)}


def test_the_lambda_breakdown_reads_only_the_named_function() -> None:
    # The shape X-Ray returns for a cold scoring request: the trace also carries the archive
    # function's segments, which must not leak into the scoring breakdown.
    document = {
        "Segments": [
            _segment("bfd-scoring", "AWS::Lambda", 10.0, 11.017),
            _segment(
                "bfd-scoring",
                "AWS::Lambda::Function",
                10.3,
                10.9133,
                [
                    {"name": "Init", "start_time": 10.3, "end_time": 10.6082},
                    {"name": "Overhead", "start_time": 10.9128, "end_time": 10.9133},
                ],
            ),
            _segment(
                "bfd-archive",
                "AWS::Lambda::Function",
                12.0,
                12.0331,
                [{"name": "Overhead", "start_time": 12.0325, "end_time": 12.0331}],
            ),
        ]
    }
    assert measure_latency.lambda_breakdown(document, "bfd-scoring") == {
        "service": 1017.0,
        "function": 613.3,
        "init": 308.2,
        "overhead": 0.5,
    }


def test_a_warm_trace_has_no_init() -> None:
    document = {
        "Segments": [
            _segment(
                "bfd-scoring",
                "AWS::Lambda::Function",
                1.0,
                1.0572,
                [{"name": "Overhead", "start_time": 1.0567, "end_time": 1.0572}],
            )
        ]
    }
    assert "init" not in measure_latency.lambda_breakdown(document, "bfd-scoring")


def test_costs_are_usage_times_price_per_thousand_sessions() -> None:
    usage = {"lambda_requests": 2_000_000, "api_requests": 1_000_000}
    rows = {row["component"]: row for row in measure_cost.price_rows(usage, sessions=500)}
    assert rows["lambda_requests"]["cost_usd"] == pytest.approx(0.40)
    assert rows["api_requests"]["cost_usd"] == pytest.approx(1.00)
    assert rows["total"]["cost_usd"] == pytest.approx(1.40)
    assert rows["total"]["per_1000_sessions_usd"] == pytest.approx(2.80)


def test_cost_needs_at_least_one_session() -> None:
    with pytest.raises(ValueError):
        measure_cost.price_rows({}, sessions=0)
