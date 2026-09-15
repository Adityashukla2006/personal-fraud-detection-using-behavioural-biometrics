"""Phase 8 smoke: the attack simulator runs every class end to end against the deployed stack,
records a decision for every checkpoint, leaves every victim's profile unchanged, and cleans up.

One user and one session per class, so the full run's measurement is not repeated here; the full
run is ``python simulator/run_attacks.py``.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

import boto3
import pytest

from simulator import attacks, run_attacks

pytestmark = pytest.mark.integration


def test_every_attack_class_runs_end_to_end_and_cleans_up(
    aws: boto3.Session, outputs: dict[str, Any], tmp_path: Path
) -> None:
    result = run_attacks.main(["--users", "1", "--sessions", "1", "--output-dir", str(tmp_path)])

    detection = list(csv.DictReader((tmp_path / "phase8_detection.csv").open(encoding="utf-8")))
    assert [row["attack"] for row in detection] == list(attacks.ATTACK_CLASSES)

    sessions = list(csv.DictReader((tmp_path / "phase8_sessions.csv").open(encoding="utf-8")))
    assert len(sessions) == len(attacks.ATTACK_CLASSES) * len(attacks.CHECKPOINTS)
    assert all(row["handler_ms"] for row in sessions)
    # A healthy stack never fails open: every decision had its state.
    assert not any("fail_open" in row["constraints"] for row in sessions)

    assert result["run"]["profiles_changed"] == 0

    cognito = aws.client("cognito-idp")
    for email in result["emails"]:
        with pytest.raises(cognito.exceptions.UserNotFoundException):
            cognito.admin_get_user(UserPoolId=outputs["user_pool_id"], Username=email)
