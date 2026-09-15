"""Tests for session-level evidence pooling and the checkpoint scoring entry point."""

from __future__ import annotations

import pytest

from fraudcore import features, scoring
from fraudcore.features import (
    KeystrokeTiming,
    PayeeRisk,
    SessionContext,
    Transfer,
)
from fraudcore.fusion import FusionModel
from fraudcore.policy import Thresholds
from fraudcore.scoring import NO_EVIDENCE
from fraudcore.session import SessionEvidence, channel_scores, pooled_extract, score_session

SHORT = KeystrokeTiming(hold=(0.1, 0.2, 0.3), down_down=(0.5, 0.6), up_down=(0.4, 0.4))
LONG = KeystrokeTiming(
    hold=(0.1,) * 6,
    down_down=(0.3, 0.2, 0.3, 0.2, 0.3),
    up_down=(0.2, 0.1, 0.2, 0.1, 0.2),
)
HOME = SessionContext(None, None, None, account_sessions=0)


def _evidence(**overrides: object) -> SessionEvidence:
    base: dict[str, object] = {
        "fields": (SHORT,),
        "profile": None,
        "profile_sessions": 0,
        "context": HOME,
        "transfer": None,
        "aggregates": None,
        "edge": None,
        "risk": None,
    }
    base.update(overrides)
    return SessionEvidence(**base)  # type: ignore[arg-type]


class TestPooledExtract:
    def test_a_single_field_pools_to_its_own_features(self) -> None:
        assert pooled_extract([SHORT]) == pytest.approx(features.extract(SHORT))

    def test_fields_are_weighted_by_keystroke_count(self) -> None:
        # mean_hold: SHORT 0.2 over 3 keys, LONG 0.1 over 6 keys -> (3 * 0.2 + 6 * 0.1) / 9
        pooled = pooled_extract([SHORT, LONG])
        assert pooled["mean_hold"] == pytest.approx((3 * 0.2 + 6 * 0.1) / 9)

    def test_no_fields_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="at least one field"):
            pooled_extract([])


class TestSessionAutomation:
    def test_intervals_pool_within_fields_only(self) -> None:
        # Pooled intervals 0.5, 0.6, 0.5, 0.6: mean 0.55, sample std sqrt(4 * 0.05^2 / 3).
        cv = (4 * 0.05**2 / 3) ** 0.5 / 0.55
        result = scoring.session_automation_channel([SHORT, SHORT])
        assert result.score == pytest.approx((0.2 - cv) / 0.2)
        assert result.confidence == pytest.approx(4 / scoring.AUTOMATION_FULL_INTERVALS)

    def test_a_single_field_matches_the_single_field_channel(self) -> None:
        assert scoring.session_automation_channel([LONG]) == scoring.automation_channel(LONG)

    def test_no_fields_is_no_evidence(self) -> None:
        assert scoring.session_automation_channel([]) == NO_EVIDENCE

    def test_coefficient_of_variation_needs_two_values(self) -> None:
        with pytest.raises(ValueError, match="at least two"):
            features.coefficient_of_variation([1.0])


class TestChannelScores:
    def test_without_a_transfer_the_transaction_and_payee_channels_have_no_evidence(self) -> None:
        scores = channel_scores(_evidence())
        assert scores["transaction"] == NO_EVIDENCE
        assert scores["payee"] == NO_EVIDENCE

    def test_a_transfer_brings_the_payee_channel_in(self) -> None:
        scores = channel_scores(_evidence(transfer=Transfer(100.0, 12, 1)))
        # A never-seen, unverified payee with no global risk: 1 + 1 + 0 at half confidence.
        assert scores["payee"].score == 2.0
        assert scores["payee"].confidence == 0.5

    def test_no_fields_means_no_keystroke_evidence(self) -> None:
        scores = channel_scores(_evidence(fields=()))
        assert scores["behaviour"] == NO_EVIDENCE
        assert scores["automation"] == NO_EVIDENCE


class TestScoreSession:
    MODEL = FusionModel.load()
    THRESHOLDS = Thresholds.load()

    def test_the_decision_carries_every_output(self) -> None:
        scored = score_session(_evidence(), self.MODEL, self.THRESHOLDS, "login")
        assert set(scored.scores) == set(scoring.CHANNELS)
        assert len(scored.decision.contributions) == 3
        assert scored.decision.risk == scored.fusion.risk

    def test_a_flagged_payee_does_not_corroborate_a_session_without_a_transfer(self) -> None:
        # Without a transfer the payee channel is silent, so a batch flag on some payee has no
        # decision to corroborate and cannot lift the action past step-up.
        flagged = PayeeRisk(risk_score=1.0, hours_since_computed=0.0, flagged=True)
        scored = score_session(
            _evidence(risk=flagged), self.MODEL, self.THRESHOLDS, "confirmation"
        )
        assert scored.decision.action not in {"restrict", "block"}
