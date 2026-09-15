"""Tests for the poisoning experiment's building blocks, on synthetic sessions only."""

from __future__ import annotations

import dataclasses

import numpy as np
import pytest

from fraudcore.adaptation import AdaptationPolicy, displacement
from research import poisoning

WIDTH = len(poisoning.FEATURES)
POLICY = AdaptationPolicy(
    budget=0.5, scale_budget=0.5, buffer_capacity=40, rebuild_every=2, cold_start_sessions=5
)


def _rows(centre: float, count: int = 20, spread: float = 0.01, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return centre + spread * rng.standard_normal((count, WIDTH))


def test_the_simulated_trust_levels() -> None:
    assert pytest.approx(0.6) == poisoning.ROUTINE
    assert pytest.approx(1.0) == poisoning.STEP_UP
    assert pytest.approx(0.6) == poisoning.HIJACKED
    assert pytest.approx(0.0) == poisoning.PASSWORD_ONLY


class TestEngineer:
    def test_a_far_session_lands_exactly_on_the_margin(self) -> None:
        profile = poisoning.reference([0.0] * WIDTH, [1.0] * WIDTH)
        crafted = poisoning.engineer(np.full(WIDTH, 5.0), profile, threshold=9.0, margin=0.9)
        assert profile.score(crafted.tolist()) == pytest.approx(8.1)

    def test_a_session_already_inside_is_left_alone(self) -> None:
        profile = poisoning.reference([0.0] * WIDTH, [1.0] * WIDTH)
        inside = np.full(WIDTH, 0.1)
        assert poisoning.engineer(inside, profile, threshold=100.0) is inside


def test_the_operating_threshold_rejects_the_target_fraction() -> None:
    profile = poisoning.reference([0.0] * WIDTH, [1.0] * WIDTH)
    rows = np.array([[i / 100] * WIDTH for i in range(1, 101)])
    threshold = poisoning.operating_threshold(profile, rows)
    rejected = np.mean([profile.score(r.tolist()) > threshold for r in rows])
    assert rejected == pytest.approx(poisoning.TARGET_FRR, abs=0.011)


class TestCalibration:
    def test_both_budgets_come_from_consecutive_day_drift(self) -> None:
        # One subject whose sessions step by 0.1 per day, with a spread that doubles each day.
        sessions = {
            "s": {day: _rows(0.1 * day, spread=0.01 * 2**day, seed=day) for day in range(1, 4)}
        }
        budget, scale_budget, centre_drift, scale_drift = poisoning.calibrate_budgets(
            sessions, POLICY
        )
        assert len(centre_drift) == len(scale_drift) == 2
        assert budget == pytest.approx(max(centre_drift), rel=0.1)
        # Doubling spread is a relative scale change of about one per day.
        assert scale_budget == pytest.approx(1.0, rel=0.35)

    def test_the_scale_budget_is_not_the_centre_budget(self) -> None:
        # Large centre drift with a constant spread: the scale budget must stay small.
        sessions = {"s": {day: _rows(1.0 * day, seed=day) for day in range(1, 4)}}
        budget, scale_budget, _, _ = poisoning.calibrate_budgets(sessions, POLICY)
        assert budget > 10 * scale_budget


class TestLearners:
    def _setup(self) -> tuple[poisoning.Profile, np.ndarray]:
        enrolment = _rows(0.0, seed=1)
        return poisoning.enrol(enrolment, POLICY), enrolment

    def test_the_static_profile_never_moves(self) -> None:
        profile, _ = self._setup()
        learner = poisoning.Static(profile)
        before = learner.reference().centre
        learner.observe(np.full(WIDTH, 50.0), 1.0)
        assert learner.reference().centre == before

    def test_naive_ewma_absorbs_engineered_attacks(self) -> None:
        profile, _ = self._setup()
        learner = poisoning.Ewma(profile, threshold=1e9, tau_min=None)
        for _ in range(50):
            learner.observe(np.full(WIDTH, 1.0), poisoning.HIJACKED)
        assert np.mean(learner.reference().centre) > 0.5

    def test_trust_gated_ewma_ignores_low_trust(self) -> None:
        profile, _ = self._setup()
        learner = poisoning.Ewma(profile, threshold=None, tau_min=POLICY.tau_min)
        learner.observe(np.full(WIDTH, 1.0), poisoning.PASSWORD_ONLY)
        assert learner.reference().centre == pytest.approx(profile.centre)

    def test_the_proposed_policy_stays_within_budget_without_step_ups(self) -> None:
        profile, enrolment = self._setup()
        learner = poisoning.Proposed(profile, enrolment, POLICY)
        for _ in range(200):
            learner.observe(np.full(WIDTH, 5.0), poisoning.HIJACKED)
        final = learner.reference()
        assert displacement(final.centre, profile.centre, profile.scale) <= POLICY.budget + 1e-9
        assert learner.saturations > 0

    def test_an_unknown_policy_is_rejected(self) -> None:
        profile, enrolment = self._setup()
        with pytest.raises(ValueError, match="unknown policy"):
            poisoning.make_learner("P9", profile, enrolment, 1.0, POLICY)


def test_a_full_run_reports_every_metric() -> None:
    def sessions(centre: float, seed: int) -> dict[int, np.ndarray]:
        return {day: _rows(centre, count=50, seed=seed + day) for day in range(1, 9)}

    job = poisoning.Job(
        victim="v",
        attacker="a",
        policy="P3",
        budget=0.5,
        scale_budget=0.5,
        multiplier=1.0,
        attacker_tau=poisoning.HIJACKED,
        victim_sessions=sessions(0.0, 0),
        attacker_sessions=sessions(1.0, 100),
        seed=1,
    )
    result = poisoning.run(dataclasses.replace(job, policy="P0"))
    assert set(result) >= {"impersonation", "false_rejection", "displacement", "trajectory"}
    assert len(result["trajectory"]) == poisoning.EPOCHS
    assert result["displacement"] == 0.0
