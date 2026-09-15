"""Tests for the frozen siphoning class: its schedules, replayed history and lake events."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime

import pytest

from fraudcore.events import lake_key
from simulator import siphoning


def test_the_siphon_is_ten_clockwork_transfers_to_one_mule() -> None:
    schedule = siphoning.siphon_schedule()
    assert [transfer.day for transfer in schedule] == pytest.approx(
        [day + siphoning.SIPHON_HOUR / 24 for day in range(0, 20, 2)]
    )
    assert {transfer.account for transfer in schedule} == {siphoning.MULE_ACCOUNT}
    assert {transfer.amount for transfer in schedule} == {siphoning.SIPHON_AMOUNT}


def test_the_control_follows_the_siphons_schedule_to_its_own_payee() -> None:
    control = siphoning.control_schedule("s010")
    siphon = siphoning.siphon_schedule()
    assert [(t.day, t.amount) for t in control] == [(t.day, t.amount) for t in siphon]
    assert {t.account for t in control} == {"TUTOR-s010"}


def test_history_is_two_transfers_a_week_to_established_payees() -> None:
    history = siphoning.genuine_history("s002", random.Random(1))
    assert len(history) == 18
    assert all(-siphoning.HISTORY_DAYS <= t.day < 0 for t in history)
    assert {t.account for t in history} <= set(siphoning.known_accounts("s002"))
    assert history == sorted(history, key=lambda t: t.day)
    # Nine weeks before the window, so every known payee is established when the siphon starts.
    assert history[0].day < -30


def test_lake_events_are_archivable_and_carry_no_typing() -> None:
    at = datetime(2026, 9, 1, 10, tzinfo=UTC)
    decision, completed = siphoning.lake_events("uid-1", "t" * 32, "p" * 64, 900.0, at, 12.5)

    assert lake_key(decision).startswith("events/type=decision.scored/dt=2026-09-01/tttt")
    assert lake_key(completed).startswith("events/type=transfer.completed/dt=2026-09-01/tttt")
    assert decision["detail"]["scores"]["behaviour"]["score"] == 12.5
    assert completed["detail"]["status"] == "released"
    assert completed["detail"]["amount"] == 900.0
    for timing_key in ("hold", "down_down", "up_down", "fields"):
        assert f'"{timing_key}"' not in json.dumps([decision, completed])
