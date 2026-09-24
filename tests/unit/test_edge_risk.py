"""Tests for the batch siphoning signal reaching the payee channel and the corroboration rule."""

from __future__ import annotations

import pytest

from fraudcore.features import (
    EdgeRisk,
    PayeeEdge,
    PayeeRisk,
    SessionContext,
    Transfer,
    payee_features,
)
from fraudcore.scoring import BATCH_PAYEE_WEIGHT, PAYEE_RISK_STALE_HOURS, payee_channel
from fraudcore.session import SessionEvidence, batch_flag

EDGE = PayeeEdge(days_since_first_seen=10.0, verified=True)


def test_an_edge_risk_is_validated() -> None:
    with pytest.raises(ValueError):
        EdgeRisk(siphon_score=1.5, hours_since_computed=0.0)
    with pytest.raises(ValueError):
        EdgeRisk(siphon_score=0.5, hours_since_computed=-1.0)


def test_siphoning_is_a_payee_feature() -> None:
    assert payee_features(EDGE, None)["siphoning"] == 0.0
    assert payee_features(EDGE, None, EdgeRisk(0.8, 1.0))["siphoning"] == pytest.approx(0.8)


def test_a_fresh_flagged_siphon_raises_the_payee_channel_by_its_weighted_score() -> None:
    quiet = payee_channel(EDGE, None).score
    raised = payee_channel(EDGE, None, EdgeRisk(0.8, 0.0, flagged=True)).score
    assert raised == pytest.approx(quiet + 0.8 * BATCH_PAYEE_WEIGHT)


def test_an_unflagged_siphon_score_adds_nothing() -> None:
    # Volume and regularity without identity describe a tutor's fees as well as a siphon.
    quiet = payee_channel(EDGE, None).score
    assert payee_channel(EDGE, None, EdgeRisk(0.6, 0.0)).score == pytest.approx(quiet)


def test_global_risk_and_siphoning_count_once() -> None:
    quiet = payee_channel(EDGE, None).score
    risk = PayeeRisk(risk_score=0.5, hours_since_computed=0.0)
    both = payee_channel(EDGE, risk, EdgeRisk(0.8, 0.0, flagged=True)).score
    assert both == pytest.approx(quiet + 0.8 * BATCH_PAYEE_WEIGHT)


def test_a_stale_siphoning_signal_counts_for_nothing() -> None:
    quiet = payee_channel(EDGE, None).score
    stale = EdgeRisk(0.8, PAYEE_RISK_STALE_HOURS, flagged=True)
    assert payee_channel(EDGE, None, stale).score == pytest.approx(quiet)


def _evidence(
    transfer: bool = True, risk: PayeeRisk | None = None, edge_risk: EdgeRisk | None = None
) -> SessionEvidence:
    return SessionEvidence(
        fields=(),
        profile=None,
        profile_sessions=0,
        context=SessionContext(None, None, None, 0),
        transfer=Transfer(900.0, 10, 1) if transfer else None,
        aggregates=None,
        edge=EDGE,
        risk=risk,
        edge_risk=edge_risk,
    )


class TestBatchFlag:
    def test_a_flagged_edge_corroborates_a_transfer_to_that_payee(self) -> None:
        assert batch_flag(_evidence(edge_risk=EdgeRisk(0.9, 1.0, flagged=True)))

    def test_a_flagged_payee_still_corroborates(self) -> None:
        assert batch_flag(_evidence(risk=PayeeRisk(0.9, 1.0, flagged=True)))

    def test_an_unflagged_score_is_not_corroboration(self) -> None:
        assert not batch_flag(_evidence(edge_risk=EdgeRisk(0.9, 1.0)))

    def test_nothing_corroborates_without_a_transfer(self) -> None:
        assert not batch_flag(_evidence(transfer=False, edge_risk=EdgeRisk(0.9, 1.0, flagged=True)))
