"""Tests for the demo scenario seeds: they must match the live system's shapes exactly."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from fraudcore.adaptation import PROFILE_FEATURES
from fraudcore.features import (
    ENROLLED_DEVICE_SESSIONS,
    Transfer,
    UserAggregates,
    transaction_features,
)
from simulator import demo_scenarios

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_the_seeded_profile_uses_the_live_feature_set() -> None:
    # A seed over a different feature set would be rejected by adaptation's decoder.
    assert demo_scenarios.BENCHMARK_FEATURE_NAMES == PROFILE_FEATURES
    item = demo_scenarios.takeover_profile("uid-1", "desktop")
    assert item["SK"] == "PROFILE#desktop"
    assert len(item["mu"]) == len(item["sigma"]) == len(PROFILE_FEATURES)
    assert item["demo_seed"] == "takeover"


def test_payee_ids_match_the_browser_hash() -> None:
    assert demo_scenarios.payee_id(" 123456789 ") == hashlib.sha256(b"123456789").hexdigest()


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to run the client hash")
def test_the_browser_computes_the_same_payee_id() -> None:
    script = (
        "const bytes = new TextEncoder().encode(' 123456789 '.trim());"
        "const digest = await crypto.subtle.digest('SHA-256', bytes);"
        "process.stdout.write(Buffer.from(digest).toString('hex'));"
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert result.stdout == demo_scenarios.payee_id("123456789")


def test_every_seed_is_marked_and_clear_selects_only_marked_items() -> None:
    seeds = [
        demo_scenarios.takeover_profile("uid-1", "mobile"),
        demo_scenarios.enrolled_device("uid-1", "device-1", "desktop"),
        demo_scenarios.flagged_payee("123456789"),
        *demo_scenarios.history_items("uid-1", "device-1", "desktop", "123456789"),
    ]
    real = {"PK": "USER#uid-1", "SK": "PROFILE#desktop", "version": 7}
    assert all("demo_seed" in seed for seed in seeds)
    assert demo_scenarios.demo_items([*seeds, real]) == seeds


def test_the_flagged_payee_is_fresh_and_flagged() -> None:
    item = demo_scenarios.flagged_payee("123456789")
    assert (item["SK"], item["flagged"]) == ("RISK", True)
    assert json.loads(json.dumps(float(item["risk_score"]))) == 1.0


def _aggregates(item: dict) -> UserAggregates:
    # Decoded exactly as the scoring store decodes AGG#<uid> / WINDOW#30d.
    return UserAggregates(
        amount_p50=float(item["amount_p50"]),
        amount_p95=float(item["amount_p95"]),
        daily_count_p95=float(item["daily_count_p95"]),
        hour_histogram=tuple(float(count) for count in item["hour_histogram"]),
        history_count=int(item["history_count"]),
    )


def test_the_seeded_history_is_what_scoring_reads() -> None:
    items = demo_scenarios.history_items(
        "uid-1", "device-1", "desktop", "123456789", now=1_000_000_000
    )
    keys = {(item["PK"], item["SK"]) for item in items}
    assert keys == {
        ("AGG#uid-1", "WINDOW#30d"),
        ("USER#uid-1", "ACCOUNT"),
        ("USER#uid-1", "DEV#device-1"),
        ("LEDGER#uid-1", f"PAYEE#{demo_scenarios.payee_id('123456789')}"),
    }
    by_sort = {item["SK"]: item for item in items}
    assert by_sort["DEV#device-1"]["session_count"] >= ENROLLED_DEVICE_SESSIONS
    payee = by_sort[f"PAYEE#{demo_scenarios.payee_id('123456789')}"]
    assert "verified_at" in payee and payee["first_seen"] < payee["verified_at"]
    aggregates = _aggregates(by_sort["WINDOW#30d"])
    assert (
        sum(aggregates.hour_histogram)
        == aggregates.history_count
        == demo_scenarios.HISTORY_TRANSFERS
    )


def test_history_without_an_account_seeds_no_payee() -> None:
    items = demo_scenarios.history_items("uid-1", "device-1", "desktop")
    assert not any(item["SK"].startswith("PAYEE#") for item in items)


def test_an_ordinary_transfer_fits_the_history_and_a_large_night_one_does_not() -> None:
    (aggregates_item,) = [
        item
        for item in demo_scenarios.history_items("uid-1", "d", "desktop")
        if item["PK"].startswith("AGG#")
    ]
    aggregates = _aggregates(aggregates_item)
    # 06:30 UTC is noon in India, inside the seeded day; 20:30 UTC is 02:00 IST, never used.
    ordinary = transaction_features(Transfer(amount=2000, hour=6, transfers_24h=1), aggregates)
    assert ordinary == {"amount_z": 0.0, "hour_rarity": 0.0, "velocity": 0.5}
    # Hand-computed: (200000 - 2000) / (8000 - 2000) = 33; rarity 1 - (0 + 1) / (4 + 1) = 4/5.
    fishy = transaction_features(Transfer(amount=200_000, hour=20, transfers_24h=1), aggregates)
    assert fishy["amount_z"] == pytest.approx(33.0)
    assert fishy["hour_rarity"] == pytest.approx(4 / 5)
