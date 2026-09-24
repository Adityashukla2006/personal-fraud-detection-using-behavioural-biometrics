"""Tests for the poisoning experiment's building blocks, on synthetic sessions only."""

from __future__ import annotations

import dataclasses
import random

import numpy as np
import pandas as pd
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


def _frozen(value: float) -> poisoning.Threshold:
    return poisoning.Threshold(poisoning.FROZEN_THRESHOLD, np.zeros((1, WIDTH)), value)


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
        learner = poisoning.Ewma(profile, threshold=_frozen(1e9), tau_min=None)
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
            poisoning.make_learner("P9", profile, enrolment, _frozen(1.0), POLICY)


class TestThreshold:
    def _calibration(self) -> np.ndarray:
        return np.array([[i / 100] * WIDTH for i in range(1, 101)])

    def test_a_frozen_threshold_ignores_the_profile(self) -> None:
        threshold = poisoning.Threshold(
            poisoning.FROZEN_THRESHOLD, self._calibration(), initial=3.0
        )
        moved = poisoning.reference([9.0] * WIDTH, [9.0] * WIDTH)
        assert threshold.of(moved) == 3.0

    def test_a_recalculated_threshold_tracks_a_widening_profile(self) -> None:
        calibration = self._calibration()
        tight = poisoning.reference([0.0] * WIDTH, [1.0] * WIDTH)
        initial = poisoning.operating_threshold(tight, calibration)
        threshold = poisoning.Threshold(
            poisoning.RECALCULATED_THRESHOLD, calibration, initial=initial
        )
        assert threshold.of(tight) == pytest.approx(initial)
        # Doubling the dispersion halves every score, so the threshold must halve with it.
        widened = poisoning.reference([0.0] * WIDTH, [2.0] * WIDTH)
        assert threshold.of(widened) == pytest.approx(initial / 2)

    def test_the_target_rejection_rate_is_held_as_the_profile_moves(self) -> None:
        calibration = self._calibration()
        tight = poisoning.reference([0.0] * WIDTH, [1.0] * WIDTH)
        threshold = poisoning.Threshold(
            poisoning.RECALCULATED_THRESHOLD,
            calibration,
            initial=poisoning.operating_threshold(tight, calibration),
        )
        widened = poisoning.reference([0.0] * WIDTH, [2.0] * WIDTH)
        rejected = np.mean(
            [widened.score(row.tolist()) > threshold.of(widened) for row in calibration]
        )
        assert rejected == pytest.approx(poisoning.TARGET_FRR, abs=0.011)

    def test_an_unknown_mode_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown threshold mode"):
            poisoning.Threshold("guessed", self._calibration(), 1.0)


class TestSampling:
    def _sessions(self) -> dict[int, np.ndarray]:
        return {day: np.arange(50).reshape(50, 1) + 100 * day for day in range(1, 9)}

    def test_days_keep_their_recording_order(self) -> None:
        rows = poisoning.stream(self._sessions(), (3, 4, 5, 6), random.Random(1))
        days = rows.flatten() // 100
        assert list(days) == sorted(days)

    def test_repetitions_are_permuted_within_a_day(self) -> None:
        sessions = self._sessions()
        rows = poisoning.stream(sessions, (3, 4, 5, 6), random.Random(1)).flatten()
        assert list(rows) != sorted(rows)
        # A permutation, so the day still contributes each of its repetitions exactly once.
        assert sorted(rows[:50]) == sorted(sessions[3].flatten())

    def test_a_different_seed_samples_differently(self) -> None:
        sessions = self._sessions()
        first = poisoning.stream(sessions, (3, 4), random.Random(1))
        second = poisoning.stream(sessions, (3, 4), random.Random(2))
        assert not np.array_equal(first, second)

    def test_epochs_advance_the_clock_past_the_reanchor_period(self) -> None:
        span_days = poisoning.timestamp(poisoning.EPOCHS, 0) / poisoning.SECONDS_PER_DAY
        assert span_days > AdaptationPolicy.load().reanchor_days
        assert poisoning.timestamp(0, 0) == 0.0
        assert poisoning.timestamp(1, 0) > poisoning.timestamp(0, 9)


class TestBootstrap:
    def _runs(self, values: list[float]) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "victim": [f"v{i}" for i in range(len(values))],
                "attacker": ["a"] * len(values),
                "impersonation": values,
            }
        )

    def test_the_interval_brackets_the_mean(self) -> None:
        rng = np.random.default_rng(0)
        runs = self._runs(list(rng.uniform(0, 1, 60)))
        low, high = poisoning.bootstrap_ci(runs, "impersonation")
        assert low < runs["impersonation"].mean() < high

    def test_no_variation_gives_no_width(self) -> None:
        low, high = poisoning.bootstrap_ci(self._runs([0.4] * 20), "impersonation")
        assert low == pytest.approx(0.4) and high == pytest.approx(0.4)

    def test_seeds_of_one_pair_are_averaged_before_resampling(self) -> None:
        # Two seeds of one pair are one observation, so the interval has no width to find.
        runs = pd.DataFrame(
            {
                "victim": ["v", "v"],
                "attacker": ["a", "a"],
                "impersonation": [0.2, 0.8],
            }
        )
        low, high = poisoning.bootstrap_ci(runs, "impersonation")
        assert low == pytest.approx(0.5) and high == pytest.approx(0.5)


class TestArms:
    def test_the_primary_arm_is_the_deployed_path(self) -> None:
        primary = next(arm for arm in poisoning.ARMS if arm.name == poisoning.PRIMARY_ARM)
        assert primary.threshold == poisoning.RECALCULATED_THRESHOLD
        assert primary.regime == poisoning.STEPUP_ONLY
        assert primary.attacker_tau == poisoning.STEP_UP

    def test_the_legacy_arm_reproduces_the_first_protocol(self) -> None:
        legacy = next(arm for arm in poisoning.ARMS if arm.name == "legacy")
        assert legacy.threshold == poisoning.FROZEN_THRESHOLD
        assert legacy.regime == poisoning.SIGNIN
        assert legacy.attacker_tau == poisoning.HIJACKED

    def test_the_gated_arm_never_reaches_tau_min(self) -> None:
        assert poisoning.GATED_ARM.attacker_tau < AdaptationPolicy.load().tau_min


def _sessions(centre: float, seed: int) -> dict[int, np.ndarray]:
    return {day: _rows(centre, count=50, seed=seed + day) for day in range(1, 9)}


def _job(**overrides: object) -> poisoning.Job:
    base = poisoning.Job(
        victim="v",
        attacker="a",
        policy="P3",
        budget=0.5,
        scale_budget=0.5,
        multiplier=1.0,
        attacker_tau=poisoning.HIJACKED,
        victim_sessions=_sessions(0.0, 0),
        attacker_sessions=_sessions(1.0, 100),
        seed=1,
    )
    return dataclasses.replace(base, **overrides)  # type: ignore[arg-type]


def test_a_full_run_reports_every_metric() -> None:
    result = poisoning.run(_job(policy="P0"))
    assert set(result) >= {
        "impersonation",
        "false_rejection",
        "displacement",
        "trajectory",
        "arm",
        "threshold_mode",
        "regime",
        "seed",
        "threshold_ratio",
    }
    assert len(result["trajectory"]) == poisoning.EPOCHS
    assert result["displacement"] == 0.0


def test_a_static_profile_holds_its_threshold_whichever_mode_is_asked() -> None:
    frozen = poisoning.run(_job(policy="P0", threshold_mode=poisoning.FROZEN_THRESHOLD))
    recalculated = poisoning.run(_job(policy="P0", threshold_mode=poisoning.RECALCULATED_THRESHOLD))
    assert recalculated["threshold_ratio"] == pytest.approx(1.0)
    assert recalculated["impersonation"] == pytest.approx(frozen["impersonation"])


def test_recalculation_changes_the_threshold_once_the_profile_moves() -> None:
    frozen = poisoning.run(_job(policy="P1", threshold_mode=poisoning.FROZEN_THRESHOLD))
    recalculated = poisoning.run(_job(policy="P1", threshold_mode=poisoning.RECALCULATED_THRESHOLD))
    assert frozen["threshold_ratio"] == pytest.approx(1.0)
    assert recalculated["threshold_ratio"] != pytest.approx(1.0)


def _trust_seen(monkeypatch: pytest.MonkeyPatch, **overrides: object) -> list[float]:
    """Every trust value a run hands to its learner, by standing in for the P0 learner."""
    seen: list[float] = []

    class Recorder(poisoning.Static):
        def observe(self, sample: np.ndarray, tau: float, now: float = 0.0) -> None:
            seen.append(tau)

    monkeypatch.setattr(poisoning, "Static", Recorder)
    poisoning.run(_job(policy="P0", **overrides))
    assert seen, "the run observed nothing"
    return seen


def test_the_production_regime_admits_only_fully_trusted_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The deployed trigger is stepup.verified, so nothing below full trust ever reaches a profile.
    seen = _trust_seen(monkeypatch, regime=poisoning.STEPUP_ONLY, attacker_tau=poisoning.STEP_UP)
    assert set(seen) == {poisoning.STEP_UP}


def test_the_signin_regime_mixes_routine_sessions_with_step_ups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _trust_seen(monkeypatch, regime=poisoning.SIGNIN, attacker_tau=poisoning.HIJACKED)
    assert set(seen) == {poisoning.ROUTINE, poisoning.STEP_UP}


def test_a_different_seed_gives_a_different_run() -> None:
    # A seed decides both the step-up schedule and which repetition of a day lands on which login,
    # so it is a real source of variance and not a relabelling of the same run.
    first = poisoning.run(_job(policy="P3", seed=1))
    second = poisoning.run(_job(policy="P3", seed=2))
    assert first["displacement"] != second["displacement"]
