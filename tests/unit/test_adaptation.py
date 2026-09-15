"""Tests for poisoning-resistant adaptation.

Known answers, worked by hand, for the geometric median, the weighted median, displacement and
projection. Boundaries CLAUDE.md section 6 names explicitly: trust exactly at tau_min, a buffer at
capacity, and a displacement exactly at the budget.
"""

from __future__ import annotations

import dataclasses
import math

import pytest

from fraudcore import adaptation
from fraudcore.adaptation import (
    AdaptationPolicy,
    BufferedSession,
    Profile,
    TrustEvidence,
    admit,
    bounded,
    displacement,
    project,
    rebuild,
    scale_displacement,
    trust,
    weighted_geometric_median,
    weighted_median,
    weighted_scale,
)
from fraudcore.features import ENROLLED_DEVICE_SESSIONS, STABILITY_WINDOW_DAYS
from fraudcore.fusion import FusionModel
from fraudcore.scoring import DISPERSION_FLOOR, ChannelScore

POLICY = AdaptationPolicy(
    budget=1.0, scale_budget=1.0, cold_start_sessions=3, buffer_capacity=10, reanchor_days=30
)

CLEAN = TrustEvidence(
    verification="stepup",
    device_sessions=ENROLLED_DEVICE_SESSIONS,
    days_since_credential_change=None,
    days_since_contact_change=None,
    non_behavioural_alert=False,
    action="step_up",
)


class TestTrust:
    def test_a_step_up_on_an_enrolled_stable_device_is_full_trust(self) -> None:
        assert trust(CLEAN) == 1.0

    def test_the_gates_multiply(self) -> None:
        # passkey sign-in 0.6 * young device 0.5 * stable 1.0 * one alert 0.5
        evidence = dataclasses.replace(
            CLEAN,
            verification="passkey_signin",
            device_sessions=3,
            non_behavioural_alert=True,
        )
        assert trust(evidence) == pytest.approx(0.15)

    def test_any_zero_vetoes(self) -> None:
        assert trust(dataclasses.replace(CLEAN, device_sessions=None)) == 0.0
        assert trust(dataclasses.replace(CLEAN, action="block")) == 0.0
        assert trust(dataclasses.replace(CLEAN, days_since_credential_change=1.0)) == 0.0

    def test_the_device_enrolment_boundary(self) -> None:
        assert adaptation.c_device(ENROLLED_DEVICE_SESSIONS) == 1.0
        assert adaptation.c_device(ENROLLED_DEVICE_SESSIONS - 1) == 0.5
        assert adaptation.c_device(0) == 0.5

    def test_a_change_exactly_at_the_stability_window_no_longer_vetoes(self) -> None:
        assert adaptation.c_stability(STABILITY_WINDOW_DAYS, None) == 1.0
        assert adaptation.c_stability(STABILITY_WINDOW_DAYS - 1e-6, None) == 0.0
        assert adaptation.c_stability(None, STABILITY_WINDOW_DAYS - 1e-6) == 0.0

    def test_an_unknown_verification_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown verification"):
            adaptation.c_verify("sms")

    def test_only_non_behavioural_channels_can_raise_an_alert(self) -> None:
        model = FusionModel.load()
        # A behaviour score far beyond any threshold must not count: that is the independence rule.
        loud_behaviour = {
            "behaviour": ChannelScore(1e6, 1.0),
            "automation": ChannelScore(1e6, 1.0),
        }
        assert not adaptation.non_behavioural_alert(loud_behaviour, model)
        assert adaptation.non_behavioural_alert({"payee": ChannelScore(1e6, 1.0)}, model)


class TestAdmission:
    def test_trust_exactly_at_tau_min_is_admitted(self) -> None:
        # A step-up on a young device: 1.0 * 0.5 * 1.0 * 1.0 lands exactly on the default tau_min.
        tau = trust(dataclasses.replace(CLEAN, device_sessions=1))
        assert tau == POLICY.tau_min
        assert admit(tau, POLICY)

    def test_trust_just_below_tau_min_is_discarded(self) -> None:
        assert not admit(POLICY.tau_min - 1e-9, POLICY)

    def test_a_buffer_at_capacity_keeps_everything(self) -> None:
        buffer = [BufferedSession((float(i),), 1.0) for i in range(POLICY.buffer_capacity)]
        assert len(bounded(buffer, POLICY.buffer_capacity)) == POLICY.buffer_capacity

    def test_a_buffer_one_over_capacity_evicts_the_oldest(self) -> None:
        buffer = [BufferedSession((float(i),), 1.0) for i in range(POLICY.buffer_capacity + 1)]
        kept = bounded(buffer, POLICY.buffer_capacity)
        assert len(kept) == POLICY.buffer_capacity
        assert kept[0].features == (1.0,)


class TestWeightedMedian:
    @pytest.mark.parametrize(
        ("values", "weights", "expected"),
        [
            ([3.0, 1.0, 2.0], [1.0, 1.0, 1.0], 2.0),
            # Weight 3 of a total 5 reaches half on its own.
            ([1.0, 2.0, 3.0], [3.0, 1.0, 1.0], 1.0),
            # Even split: the lower value is where cumulative weight first reaches half.
            ([1.0, 2.0], [1.0, 1.0], 1.0),
        ],
    )
    def test_known_answers(
        self, values: list[float], weights: list[float], expected: float
    ) -> None:
        assert weighted_median(values, weights) == expected

    def test_non_positive_weights_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            weighted_median([1.0, 2.0], [1.0, 0.0])


class TestGeometricMedian:
    def test_in_one_dimension_it_is_the_median_not_the_mean(self) -> None:
        # Points 0, 1, 10: mean 3.67, median 1.
        assert weighted_geometric_median([(0.0,), (1.0,), (10.0,)], [1.0, 1.0, 1.0]) == (
            pytest.approx(1.0, abs=1e-4),
        )

    def test_the_centre_of_a_square(self) -> None:
        corners = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
        assert weighted_geometric_median(corners, [1.0] * 4) == pytest.approx((0.5, 0.5))

    def test_collinear_points_meet_the_guard_on_the_middle_point(self) -> None:
        # The weighted mean starts exactly on (1, 0), a data point, which plain Weiszfeld cannot
        # divide through. The others' pulls cancel, so the guard stops there.
        points = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)]
        assert weighted_geometric_median(points, [1.0] * 3) == pytest.approx((1.0, 0.0))

    def test_a_majority_weight_point_is_the_median(self) -> None:
        # Weight 3 of 5 on (0, 0): no pull from the minority can move the estimate off it.
        points = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)]
        median = weighted_geometric_median(points, [3.0, 1.0, 1.0])
        assert median == pytest.approx((0.0, 0.0), abs=1e-4)

    def test_a_minority_of_extreme_outliers_moves_it_a_bounded_amount(self) -> None:
        genuine = [(0.0, 0.0), (0.1, 0.0), (0.0, 0.1)]
        outliers = [(1000.0, 1000.0), (1e6, 1e6)]
        median = weighted_geometric_median(genuine + outliers, [1.0] * 5)
        # A mean would sit near (2e5, 2e5); the median stays within a unit of the genuine cluster.
        assert math.dist(median, (0.03, 0.03)) < 1.0

    def test_mismatched_inputs_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="same non-zero width"):
            weighted_geometric_median([(0.0,), (1.0, 2.0)], [1.0, 1.0])
        with pytest.raises(ValueError, match="one weight per point"):
            weighted_geometric_median([(0.0,)], [1.0, 1.0])


class TestScale:
    def test_scale_is_the_rescaled_weighted_median_absolute_deviation(self) -> None:
        points = [(0.0,), (2.0,), (4.0,)]
        # |deviations| from centre 2: 2, 0, 2 -> median 2
        expected = 2.0 * adaptation.MEDIAN_TO_MEAN_ABSOLUTE_DEVIATION
        assert weighted_scale(points, [1.0] * 3, (2.0,)) == (pytest.approx(expected),)

    def test_a_constant_feature_is_floored(self) -> None:
        assert weighted_scale([(5.0,), (5.0,)], [1.0, 1.0], (5.0,)) == (DISPERSION_FLOOR,)


class TestDisplacement:
    def test_displacement_matches_the_hand_computed_value(self) -> None:
        # (|2 - 1| / 0.5 + |10 - 10| / 2) / 2 = 1.0
        assert displacement((2.0, 10.0), (1.0, 10.0), (0.5, 2.0)) == pytest.approx(1.0)

    def test_scale_displacement_is_mean_relative_change(self) -> None:
        # (|2 - 1| / 1 + |3 - 3| / 3) / 2 = 0.5
        assert scale_displacement((2.0, 3.0), (1.0, 3.0)) == pytest.approx(0.5)

    def test_a_displacement_exactly_at_the_budget_is_accepted_unchanged(self) -> None:
        accepted, bound = project((3.0,), (1.0,), distance=1.0, budget=1.0)
        assert (accepted, bound) == ((3.0,), False)

    def test_a_displacement_twice_the_budget_is_projected_halfway(self) -> None:
        accepted, bound = project((3.0,), (1.0,), distance=2.0, budget=1.0)
        assert accepted == pytest.approx((2.0,))
        assert bound


def _profile(centre: float = 0.0, scale: float = 1.0, **changes: object) -> Profile:
    base = Profile((centre,), (scale,), (centre,), (scale,), 0.0, None, 0, 1)
    return dataclasses.replace(base, **changes)


def _buffer(values: list[float], weight: float = 1.0) -> list[BufferedSession]:
    return [BufferedSession((value,), weight) for value in values]


class TestRebuild:
    def test_fewer_sessions_than_cold_start_build_nothing(self) -> None:
        result = rebuild(None, _buffer([1.0, 2.0]), tau=1.0, now=0.0, policy=POLICY)
        assert (result.outcome, result.profile) == ("cold_start", None)

    def test_cold_start_bootstraps_an_anchored_profile(self) -> None:
        result = rebuild(None, _buffer([1.0, 2.0, 3.0]), tau=0.6, now=5.0, policy=POLICY)
        assert result.outcome == "bootstrapped"
        assert result.profile is not None
        assert result.profile.centre == pytest.approx((2.0,), abs=1e-4)
        assert result.profile.anchor_centre == result.profile.centre
        assert (result.profile.anchored_at, result.profile.version) == (5.0, 1)

    def test_a_far_candidate_is_projected_to_exactly_the_budget(self) -> None:
        # Buffer centred at 10 against an anchor at 0 with scale 1: displacement 10, budget 1.
        result = rebuild(_profile(), _buffer([10.0] * 5), tau=0.6, now=1.0, policy=POLICY)
        assert result.saturated
        assert result.candidate_displacement == pytest.approx(10.0)
        assert result.profile is not None
        assert displacement(result.profile.centre, (0.0,), (1.0,)) == pytest.approx(1.0)
        assert result.profile.saturations == 1
        assert result.profile.last_saturated_at == 1.0

    def test_scale_inflation_is_bounded_by_the_scale_budget(self) -> None:
        # Centre unchanged, but a spread of +-50 would inflate the scale fifty-fold.
        result = rebuild(_profile(), _buffer([-50.0, 0.0, 50.0]), tau=0.6, now=1.0, policy=POLICY)
        assert result.saturated
        assert result.profile is not None
        assert scale_displacement(result.profile.scale, (1.0,)) == pytest.approx(
            POLICY.scale_budget
        )

    def test_the_two_budgets_bind_independently(self) -> None:
        # Loose centre budget, tight scale budget: the centre moves freely, the scale does not.
        policy = dataclasses.replace(POLICY, budget=1000.0, scale_budget=0.1)
        result = rebuild(_profile(), _buffer([-40.0, 10.0, 60.0]), tau=0.6, now=1.0, policy=policy)
        assert result.profile is not None
        assert result.profile.centre == pytest.approx((10.0,), abs=1e-3)
        assert scale_displacement(result.profile.scale, (1.0,)) == pytest.approx(0.1)

    def test_sustained_poisoning_without_step_ups_stays_within_one_budget(self) -> None:
        profile = _profile()
        for _ in range(25):
            result = rebuild(profile, _buffer([100.0] * 10), tau=0.6, now=1.0, policy=POLICY)
            assert result.profile is not None
            profile = result.profile
        assert not result.reanchored
        assert displacement(profile.centre, (0.0,), (1.0,)) <= POLICY.budget + 1e-9

    def test_full_trust_re_anchors_after_projection(self) -> None:
        result = rebuild(_profile(), _buffer([10.0] * 5), tau=1.0, now=2.0, policy=POLICY)
        assert result.reanchored
        assert result.profile is not None
        assert result.profile.anchor_centre == result.profile.centre
        assert result.profile.anchored_at == 2.0

    def test_a_quiet_period_exactly_reanchor_days_long_re_anchors(self) -> None:
        now = POLICY.reanchor_days * adaptation.SECONDS_PER_DAY
        result = rebuild(_profile(), _buffer([0.1] * 5), tau=0.6, now=now, policy=POLICY)
        assert (result.saturated, result.reanchored) == (False, True)

    def test_a_saturated_rebuild_never_re_anchors_on_time_alone(self) -> None:
        now = 10 * POLICY.reanchor_days * adaptation.SECONDS_PER_DAY
        result = rebuild(_profile(), _buffer([10.0] * 5), tau=0.6, now=now, policy=POLICY)
        assert (result.saturated, result.reanchored) == (True, False)


class TestPolicy:
    def test_the_shipped_policy_loads(self) -> None:
        policy = AdaptationPolicy.load()
        assert policy.tau_min == 0.5
        assert policy.budget > 0
        assert policy.scale_budget > 0

    @pytest.mark.parametrize(
        "changes",
        [
            {"budget": 0.0},
            {"scale_budget": 0.0},
            {"tau_min": 0.0},
            {"cold_start_sessions": 11},
            {"rebuild_every": 0},
        ],
    )
    def test_invalid_policies_are_rejected(self, changes: dict[str, float]) -> None:
        with pytest.raises(ValueError):
            dataclasses.replace(POLICY, **changes)
