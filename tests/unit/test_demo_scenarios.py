"""Tests for the demo scenario seeds: they must match the live system's shapes exactly."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from fraudcore.adaptation import PROFILE_FEATURES
from fraudcore.batch import TransferRecord, compute
from fraudcore.features import (
    ENROLLED_DEVICE_SESSIONS,
    DeviceRecord,
    KeystrokeTiming,
    PayeeEdge,
    SessionContext,
    Transfer,
    transaction_features,
)
from fraudcore.fusion import FusionModel, load_models
from fraudcore.policy import OPENING_BALANCE, Thresholds
from fraudcore.scoring import TRANSACTION_FULL_HISTORY
from fraudcore.session import SessionEvidence, score_session
from simulator import demo_scenarios

REPO_ROOT = Path(__file__).resolve().parents[2]
# 2026-01-01T06:30:00Z, noon in India.
NOW = 1_767_249_000.0
DAY = 86_400


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
        *demo_scenarios.ledger_items("uid-1", demo_scenarios.seeded_transfers("uid-1", NOW)),
        *demo_scenarios.context_items("uid-1", "device-1", "desktop"),
    ]
    real = {"PK": "USER#uid-1", "SK": "PROFILE#desktop", "version": 7}
    assert all("demo_seed" in seed for seed in seeds)
    assert demo_scenarios.demo_items([*seeds, real]) == seeds


def test_the_flagged_payee_is_fresh_and_flagged() -> None:
    item = demo_scenarios.flagged_payee("123456789")
    assert (item["SK"], item["flagged"]) == ("RISK", True)
    assert json.loads(json.dumps(float(item["risk_score"]))) == 1.0


def _records(uid: str = "uid-1") -> list[TransferRecord]:
    # What the aggregator's Athena query returns for the seeded lake events.
    return [
        TransferRecord(
            uid=uid,
            payee_id=demo_scenarios.payee_id(transfer.payee.account),
            amount=float(transfer.amount),
            at=float(transfer.at),
            identity_score=0.0,
        )
        for transfer in demo_scenarios.seeded_transfers(uid, NOW)
    ]


class TestSeededTransfers:
    def test_the_same_user_gets_the_same_history(self) -> None:
        first = demo_scenarios.seeded_transfers("uid-1", NOW)
        assert first == demo_scenarios.seeded_transfers("uid-1", NOW)
        assert first != demo_scenarios.seeded_transfers("uid-2", NOW)

    def test_every_transfer_is_in_the_past_by_day_in_india(self) -> None:
        for transfer in demo_scenarios.seeded_transfers("uid-1", NOW):
            assert NOW - demo_scenarios.HISTORY_DAYS * DAY - DAY < transfer.at < NOW
            hour_ist = (transfer.at + demo_scenarios.IST_OFFSET_SECONDS) // 3600 % 24
            assert hour_ist in demo_scenarios.HISTORY_HOURS_IST

    def test_ids_are_valid_lake_subjects_and_leave_room_on_the_ledger_page(self) -> None:
        transfers = demo_scenarios.seeded_transfers("uid-1", NOW)
        assert len({t.transfer_id for t in transfers}) == len(transfers)
        assert all(re.fullmatch(r"demo[0-9a-f]{28}", t.transfer_id) for t in transfers)
        # The account view reads 100 ledger items; at least half stay free for real transfers.
        assert len(transfers) <= 50


class TestWhatTheAggregatorComputes:
    def test_the_transaction_channel_reaches_full_confidence(self) -> None:
        aggregates = compute(_records(), NOW).aggregates["uid-1"]
        assert aggregates.history_count >= TRANSACTION_FULL_HISTORY

    def test_every_regular_payee_is_established(self) -> None:
        edges = compute(_records(), NOW).edges
        assert len(edges) == len(demo_scenarios.REGULAR_PAYEES)
        assert all(NOW - edge.first_seen > 30 * DAY for edge in edges)
        assert all(edge.volume == edge.regularity == 0.0 for edge in edges)

    def test_an_ordinary_payment_fits_and_a_large_one_stands_out(self) -> None:
        aggregates = compute(_records(), NOW).aggregates["uid-1"]
        ordinary = transaction_features(Transfer(amount=900, hour=6, transfers_24h=1), aggregates)
        fishy = transaction_features(Transfer(amount=200_000, hour=6, transfers_24h=1), aggregates)
        assert ordinary["amount_z"] <= 0.0
        # Many times the gap between this user's median and p95 payment.
        assert fishy["amount_z"] > 10.0


class TestTheDemoItEnables:
    """With the deployed model: what the seeded account does at confirmation, at any hour."""

    MODEL = FusionModel.from_dict(load_models())
    THRESHOLDS = Thresholds.load()
    HOLD = (0.09, 0.11, 0.08, 0.10, 0.12, 0.09, 0.10, 0.11, 0.08, 0.10, 0.09)
    DOWN = (0.21, 0.35, 0.18, 0.42, 0.27, 0.31, 0.24, 0.39, 0.22, 0.33)
    FIELD = KeystrokeTiming(
        HOLD, DOWN, tuple(round(d - h, 4) for d, h in zip(DOWN, HOLD, strict=False))
    )

    def _action(self, amount: float, hour: int, edge: PayeeEdge | None) -> str:
        evidence = SessionEvidence(
            fields=(self.FIELD,) * 4,
            profile=None,
            profile_sessions=0,
            # The seeded account and enrolled device.
            context=SessionContext(DeviceRecord(demo_scenarios.ENROLLED_SESSIONS), None, None, 40),
            transfer=Transfer(amount, hour, 1),
            aggregates=compute(_records(), NOW).aggregates["uid-1"],
            edge=edge,
            risk=None,
        )
        return score_session(evidence, self.MODEL, self.THRESHOLDS, "confirmation").decision.action

    @pytest.mark.parametrize("hour", range(24))
    def test_an_ordinary_payment_to_a_regular_payee_goes_through(self, hour: int) -> None:
        regular = PayeeEdge(days_since_first_seen=55.0, verified=True)
        assert self._action(900, hour, regular) in {"allow", "monitor"}

    @pytest.mark.parametrize("hour", range(24))
    def test_a_large_payment_to_a_new_payee_is_held_for_review(self, hour: int) -> None:
        assert self._action(200_000, hour, None) == "restrict"


class TestLedgerItems:
    def test_one_released_transfer_per_seeded_transfer(self) -> None:
        transfers = demo_scenarios.seeded_transfers("uid-1", NOW)
        items = demo_scenarios.ledger_items("uid-1", transfers)
        txns = [item for item in items if item["SK"].startswith("TXN#")]
        assert len(txns) == len(transfers)
        assert all(item["status"] == "released" for item in txns)
        assert all(item["PK"] == "LEDGER#uid-1" for item in items)

    def test_edges_add_up_to_the_transfers(self) -> None:
        transfers = demo_scenarios.seeded_transfers("uid-1", NOW)
        edges = [
            item
            for item in demo_scenarios.ledger_items("uid-1", transfers)
            if item["SK"].startswith("PAYEE#")
        ]
        assert len(edges) == len(demo_scenarios.REGULAR_PAYEES)
        assert sum(edge["txn_count"] for edge in edges) == len(transfers)
        assert sum(edge["cum_amount"] for edge in edges) == sum(t.amount for t in transfers)
        for edge in edges:
            assert edge["first_seen"] <= edge["last_paid_at"]
            # Regular payees were verified with a passkey, as the scoring path checks.
            assert "verified_at" in edge


class TestBackfill:
    TXN = {"PK": "LEDGER#uid-1", "payee_id": "p" * 64, "status": "released"}

    def test_real_released_transfers_become_one_edge_per_payee(self) -> None:
        ledger = [
            {**self.TXN, "SK": "TXN#a", "amount": Decimal(100), "created_at": 10, "closed_at": 20},
            {**self.TXN, "SK": "TXN#b", "amount": Decimal("50.5"), "created_at": 5, "closed_at": 8},
        ]
        (edge,) = demo_scenarios.backfilled_edges("uid-1", ledger)
        assert edge["SK"] == f"PAYEE#{'p' * 64}"
        assert (edge["first_seen"], edge["last_paid_at"]) == (8, 20)
        assert (edge["txn_count"], edge["cum_amount"]) == (2, Decimal("150.5"))
        # Nothing on a transfer item says a passkey released it.
        assert "verified_at" not in edge
        assert set(demo_scenarios.BACKFILLED_FIELDS) <= set(edge)

    def test_seeded_unreleased_and_non_transfer_items_are_skipped(self) -> None:
        ledger = [
            {**self.TXN, "SK": "TXN#seed", "amount": 1, "created_at": 1, "demo_seed": "history"},
            {**self.TXN, "SK": "TXN#held", "amount": 1, "created_at": 1, "status": "cancelled"},
            {"PK": "LEDGER#uid-1", "SK": "BALANCE", "balance": Decimal("1")},
        ]
        assert demo_scenarios.backfilled_edges("uid-1", ledger) == []


def test_the_opening_balance_matches_the_ledger() -> None:
    assert demo_scenarios.OPENING_BALANCE == OPENING_BALANCE


def test_context_makes_this_browser_enrolled() -> None:
    account, device = demo_scenarios.context_items("uid-1", "device-1", "desktop")
    assert (account["SK"], device["SK"]) == ("ACCOUNT", "DEV#device-1")
    assert device["session_count"] >= ENROLLED_DEVICE_SESSIONS


@pytest.mark.skipif(shutil.which("node") is None, reason="node is needed to run the snippet")
def test_the_payee_book_snippet_names_every_regular_payee() -> None:
    script = (
        "const data = {'bfd-payees': JSON.stringify({kept: {name: 'Real', last4: '0000'}})};"
        "globalThis.localStorage = {getItem: (k) => data[k] ?? null,"
        " setItem: (k, v) => { data[k] = v; }};"
        + demo_scenarios.payee_book_snippet()
        + ";process.stdout.write(data['bfd-payees']);"
    )
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=True, timeout=30
    )
    book = json.loads(result.stdout)
    assert book["kept"]["name"] == "Real"
    for payee in demo_scenarios.REGULAR_PAYEES:
        entry = book[demo_scenarios.payee_id(payee.account)]
        assert entry == {"name": payee.name, "last4": payee.account[-4:]}
