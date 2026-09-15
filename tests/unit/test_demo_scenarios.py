"""Tests for the demo scenario seeds: they must match the live system's shapes exactly."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from fraudcore.adaptation import PROFILE_FEATURES
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
    ]
    real = {"PK": "USER#uid-1", "SK": "PROFILE#desktop", "version": 7}
    assert all("demo_seed" in seed for seed in seeds)
    assert demo_scenarios.demo_items([*seeds, real]) == seeds


def test_the_flagged_payee_is_fresh_and_flagged() -> None:
    item = demo_scenarios.flagged_payee("123456789")
    assert (item["SK"], item["flagged"]) == ("RISK", True)
    assert json.loads(json.dumps(float(item["risk_score"]))) == 1.0
