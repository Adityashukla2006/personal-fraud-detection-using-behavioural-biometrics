"""Tests for the slow plane's siphoning and destination features."""

from __future__ import annotations

import pytest

from fraudcore.batch import (
    SIPHON_WEIGHTS,
    TransferRecord,
    compute,
    edge_signals,
    payee_signals,
    quantile,
    user_aggregates,
)

DAY = 86_400
NOW = 1_788_000_000.0


def rec(
    uid: str, payee: str, amount: float, days_ago: float, identity: float | None = None
) -> TransferRecord:
    return TransferRecord(uid, payee, amount, NOW - days_ago * DAY, identity)


class TestQuantile:
    def test_linear_interpolation_between_ranks(self) -> None:
        assert quantile([1.0, 2.0, 3.0, 4.0], 0.5) == pytest.approx(2.5)
        assert quantile([0.0, 10.0], 0.95) == pytest.approx(9.5)
        assert quantile([7.0], 0.95) == 7.0

    def test_no_values_or_a_bad_q_is_an_error(self) -> None:
        with pytest.raises(ValueError):
            quantile([], 0.5)
        with pytest.raises(ValueError):
            quantile([1.0], 1.5)


class TestUserAggregates:
    def test_only_the_last_30_days_count(self) -> None:
        records = [rec("u", "p", 100.0, 1), rec("u", "p", 300.0, 2), rec("u", "p", 999.0, 31)]
        aggregates = user_aggregates(records, NOW)
        assert aggregates is not None
        assert aggregates.history_count == 2
        assert aggregates.amount_p50 == pytest.approx(200.0)
        assert sum(aggregates.hour_histogram) == 2

    def test_the_daily_count_counts_quiet_days(self) -> None:
        daily = [rec("u", "p", 50.0, day + 0.5) for day in range(30)]
        assert user_aggregates(daily, NOW).daily_count_p95 == pytest.approx(1.0)
        burst = [rec("u", "p", 50.0, 0.5) for _ in range(5)]
        # 29 empty days and one day of five: the 95th percentile day is still an empty one.
        assert user_aggregates(burst, NOW).daily_count_p95 == pytest.approx(0.0)

    def test_no_recent_transfers_is_no_aggregate(self) -> None:
        assert user_aggregates([rec("u", "p", 50.0, 45)], NOW) is None


def _siphoned(identity_on_payee: float, identity_elsewhere: float) -> list[TransferRecord]:
    # Eight ordinary transfers to established payees, then ten clockwork transfers to a new one.
    history = [rec("u", f"known{k % 3}", 1500.0, 60 - 5 * k, identity_elsewhere) for k in range(8)]
    siphon = [rec("u", "mule", 900.0, 18 - 2 * k, identity_on_payee) for k in range(10)]
    return history + siphon


def _edge(records: list[TransferRecord], payee: str = "mule"):
    edges = edge_signals(records, NOW, user_aggregates(records, NOW))
    return next(edge for edge in edges if edge.payee_id == payee)


class TestEdgeSignals:
    def test_a_regular_new_payee_typed_by_someone_else_is_flagged(self) -> None:
        edge = _edge(_siphoned(identity_on_payee=40.0, identity_elsewhere=20.0))
        assert edge.transfers == 10
        assert edge.cumulative == pytest.approx(9000.0)
        assert edge.interval_cv == pytest.approx(0.0)
        assert (edge.volume, edge.regularity, edge.band, edge.identity) == pytest.approx(
            (1.0, 1.0, 1.0, 1.0)
        )
        assert edge.siphon_score == pytest.approx(sum(SIPHON_WEIGHTS.values()))
        assert edge.flagged

    def test_the_same_pattern_typed_by_the_owner_raises_the_score_but_is_not_flagged(self) -> None:
        edge = _edge(_siphoned(identity_on_payee=20.0, identity_elsewhere=20.0))
        assert edge.identity == 0.0
        assert edge.siphon_score == pytest.approx(0.6)
        assert not edge.flagged

    def test_ordinary_variation_in_the_owners_typing_is_not_identity_evidence(self) -> None:
        # 20% worse is inside the floor of ordinary day-to-day variation.
        edge = _edge(_siphoned(identity_on_payee=24.0, identity_elsewhere=20.0))
        assert edge.identity == 0.0
        assert not edge.flagged

    def test_an_established_payee_is_not_an_edge(self) -> None:
        records = _siphoned(40.0, 20.0)
        payees = {edge.payee_id for edge in edge_signals(records, NOW, None)}
        assert payees == {"mule"}

    def test_regularity_and_band_need_three_transfers(self) -> None:
        records = [rec("u", "new", 900.0, 4), rec("u", "new", 900.0, 2)]
        edge = _edge(records, "new")
        assert edge.interval_cv is None
        assert (edge.regularity, edge.band) == (0.0, 0.0)
        assert not edge.flagged

    def test_identity_needs_samples_on_both_sides(self) -> None:
        records = [rec("u", "new", 900.0, 6 - 2 * k, 40.0) for k in range(3)]
        assert _edge(records, "new").identity_gap is None

    def test_records_from_two_users_are_rejected(self) -> None:
        with pytest.raises(ValueError):
            edge_signals([rec("a", "p", 1.0, 1), rec("b", "p", 1.0, 1)], NOW, None)


class TestPayeeSignals:
    def test_several_new_senders_flag_a_mule(self) -> None:
        records = [rec(f"u{i}", "mule", 900.0, 5) for i in range(3)]
        [payee] = payee_signals(records, NOW, [])
        assert (payee.distinct_senders_30d, payee.new_senders_30d) == (3, 3)
        assert payee.fan_in == pytest.approx(0.5)
        assert payee.risk_score == pytest.approx(0.5)
        assert payee.flagged

    def test_one_sender_is_not_fan_in(self) -> None:
        [payee] = payee_signals([rec("u", "shop", 900.0, 5)], NOW, [])
        assert payee.risk_score == 0.0
        assert not payee.flagged

    def test_a_flagged_edge_flags_its_payee(self) -> None:
        records = _siphoned(40.0, 20.0)
        result = compute(records, NOW)
        mule = next(payee for payee in result.payees if payee.payee_id == "mule")
        assert mule.flagged_edges == 1 and mule.flagged
        assert mule.risk_score == pytest.approx(1.0)


def test_compute_ignores_future_and_expired_transfers() -> None:
    records = [rec("u", "p", 100.0, -1), rec("u", "p", 100.0, 100), rec("u", "p", 100.0, 3)]
    result = compute(records, NOW)
    assert result.aggregates["u"].history_count == 1


def test_a_record_with_a_bad_amount_is_rejected() -> None:
    with pytest.raises(ValueError):
        TransferRecord("u", "p", 0.0, NOW)
    with pytest.raises(ValueError):
        TransferRecord("u", "p", 5.0, NOW, identity_score=-1.0)
