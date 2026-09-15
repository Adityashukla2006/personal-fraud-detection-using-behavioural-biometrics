"""Tests for fitting the fusion model: the estimator, the thresholds, and that offline sessions are
assembled the way the live store assembles them."""

from __future__ import annotations

import random

import numpy as np
import pytest

from fraudcore.adaptation import AdaptationPolicy
from fraudcore.policy import Thresholds
from fraudcore.scoring import CHANNELS, ChannelScore
from research import fit_fusion
from simulator import attacks
from simulator.cmu import Repetition


class TestFit:
    def test_a_separating_channel_gets_the_weight_and_noise_gets_little(self) -> None:
        rng = np.random.default_rng(0)
        n = 400
        y = np.array([0] * n + [1] * n, dtype=float)
        x = rng.normal(0, 1, size=(2 * n, 5))
        x[:, 0] += y * 3.0  # only the first channel carries signal
        intercept, weights = fit_fusion.fit(x, y)
        assert weights[0] > 1.0
        assert weights[0] > 5 * max(weights[1:])

    def test_weights_are_never_negative(self) -> None:
        rng = np.random.default_rng(1)
        n = 300
        y = np.array([0] * n + [1] * n, dtype=float)
        x = rng.normal(0, 1, size=(2 * n, 5))
        x[:, 2] -= y * 3.0  # an anti-correlated channel would want a negative weight
        _, weights = fit_fusion.fit(x, y)
        assert (weights >= 0).all()
        assert weights[2] == pytest.approx(0.0, abs=1e-6)

    def test_one_class_cannot_be_fitted(self) -> None:
        with pytest.raises(ValueError, match="both classes"):
            fit_fusion.fit(np.zeros((4, 5)), np.zeros(4))


class TestThresholds:
    def test_thresholds_sit_at_genuine_percentiles(self) -> None:
        risks = np.linspace(0, 50, 1001)
        thresholds = fit_fusion.thresholds_from(risks)
        assert thresholds.step_up == pytest.approx(47.5, abs=0.1)
        assert thresholds.monitor == pytest.approx(40.0, abs=0.1)

    def test_a_degenerate_genuine_distribution_still_yields_valid_thresholds(self) -> None:
        thresholds = fit_fusion.thresholds_from(np.zeros(100))
        assert isinstance(thresholds, Thresholds)
        assert 0 < thresholds.monitor < thresholds.step_up < thresholds.restrict < thresholds.block

    def test_near_certain_genuine_risk_is_capped_below_100(self) -> None:
        thresholds = fit_fusion.thresholds_from(np.full(100, 99.99))
        assert thresholds.block <= 99.9


def test_standardisation_uses_genuine_sessions_and_floors_the_scale() -> None:
    rows = [
        {"label": 0, "scores": {"behaviour": ChannelScore(4.0, 1.0)}},
        {"label": 0, "scores": {"behaviour": ChannelScore(6.0, 1.0)}},
        {"label": 1, "scores": {"behaviour": ChannelScore(1000.0, 1.0)}},
    ]
    standard = fit_fusion.standardisation(rows)
    assert standard["behaviour"] == pytest.approx((5.0, 1.0))
    assert standard["payee"] == (0.0, fit_fusion.SCALE_FLOOR)
    assert set(standard) == set(CHANNELS)


def _reps(subject: str, scale: float) -> list[Repetition]:
    """Human-like typing: irregular enough to pass the regularity test, and never repeating a
    rhythm, so only a true copy of an earlier repetition matches the replay history."""
    rng = random.Random(f"{subject}-{scale}")
    reps = []
    for session in range(1, 9):
        for rep in range(1, 51):
            hold = tuple(round(max(0.03, rng.gauss(0.09, 0.02)) * scale, 4) for _ in range(11))
            down_down = tuple(
                round(max(0.05, rng.gauss(0.25, 0.08)) * scale, 4) for _ in range(10)
            )
            up_down = tuple(round(d - h, 4) for d, h in zip(down_down, hold, strict=False))
            reps.append(Repetition(subject, session, rep, hold, down_down, up_down))
    return reps


class TestOfflineSessions:
    VICTIM = _reps("s020", 1.0)
    ATTACKER = _reps("s021", 1.7)

    def _evidence(self, attack: str) -> tuple[attacks.SessionPlan, object]:
        state = fit_fusion.baseline(self.VICTIM, AdaptationPolicy.load())
        plan = attacks.plan_session(
            attack, "s020", self.VICTIM, self.ATTACKER, 0, "sim-home-s020", random.Random(1)
        )
        return plan, fit_fusion.evidence(plan, state)

    def test_genuine_sessions_see_the_enrolled_device_and_known_payee(self) -> None:
        _, evidence = self._evidence("genuine")
        assert evidence.context.device is not None
        assert evidence.edge is not None and evidence.edge.verified

    def test_takeover_sessions_see_a_new_device_and_a_new_payee(self) -> None:
        _, evidence = self._evidence("takeover")
        assert evidence.context.device is None
        assert evidence.edge is None
        assert evidence.transfer is not None
        assert evidence.transfer.amount == attacks.TAKEOVER_AMOUNT

    def test_a_replay_of_enrolment_typing_is_found_in_the_seeded_history(self) -> None:
        from fraudcore.session import channel_scores

        _, replay = self._evidence("replay")
        _, genuine = self._evidence("genuine")
        assert channel_scores(replay)["automation"].score == pytest.approx(1.0)
        assert channel_scores(genuine)["automation"].score < 1.0
