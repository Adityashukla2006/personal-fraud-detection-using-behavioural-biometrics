"""Tests for the pure-Python scaled Manhattan detector.

The arithmetic here is checked against values worked out by hand rather than against a snapshot of
its own output. A snapshot only proves the code has not changed; it would happily lock in a wrong
answer. Since every downstream number in this project -- the Equal Error Rate, the risk score, the
poisoning displacement -- is a function of this distance, a wrong answer here is invisible and
propagates everywhere.
"""

from __future__ import annotations

import pytest

from fraudcore import scoring
from fraudcore.features import KeystrokeTiming, timing_shingles
from fraudcore.scoring import (
    DISPERSION_FLOOR,
    NO_EVIDENCE,
    ChannelScore,
    DeviceProfiles,
    ReferenceProfile,
)

# Chosen so that every statistic is exact in decimal and can be verified by hand.
#
#   feature "spread"  values 1, 3, 5   ->  centre 3.0
#                                          MAD    (2 + 0 + 2) / 3 = 4/3
#                                          std    sqrt((4 + 0 + 4) / 2) = 2.0
#   feature "flat"    values 10 each   ->  centre 10.0
#                                          MAD    0, floored to DISPERSION_FLOOR
NAMES = ["spread", "flat"]
ENROLMENT = [[1.0, 10.0], [3.0, 10.0], [5.0, 10.0]]


@pytest.fixture
def profile() -> ReferenceProfile:
    return ReferenceProfile.fit(ENROLMENT, NAMES)


class TestFit:
    def test_centre_is_the_per_feature_mean(self, profile: ReferenceProfile) -> None:
        assert profile.centre == (3.0, 10.0)

    def test_mad_dispersion_matches_the_hand_computed_value(
        self, profile: ReferenceProfile
    ) -> None:
        assert profile.dispersion[0] == pytest.approx(4 / 3)

    def test_std_dispersion_uses_the_sample_denominator(self) -> None:
        # sqrt(8 / 2) = 2.0, not sqrt(8 / 3). The population form would understate spread and
        # inflate every score computed against this profile.
        std_profile = ReferenceProfile.fit(ENROLMENT, NAMES, scaling="std")
        assert std_profile.dispersion[0] == pytest.approx(2.0)

    def test_a_constant_feature_is_floored_and_counted(self, profile: ReferenceProfile) -> None:
        assert profile.dispersion[1] == DISPERSION_FLOOR
        assert profile.floored_features == 1

    def test_two_repetitions_is_the_minimum(self) -> None:
        # The boundary: one repetition has no dispersion to estimate, two is the least that does.
        ReferenceProfile.fit(ENROLMENT[:2], NAMES)
        with pytest.raises(ValueError, match="at least two"):
            ReferenceProfile.fit(ENROLMENT[:1], NAMES)

    def test_a_ragged_row_names_the_row(self) -> None:
        with pytest.raises(ValueError, match="row 1"):
            ReferenceProfile.fit([[1.0, 2.0], [3.0]], NAMES)

    def test_an_unknown_scaling_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown scaling"):
            ReferenceProfile.fit(ENROLMENT, NAMES, scaling="median")  # type: ignore[arg-type]


class TestScore:
    def test_score_matches_the_hand_computed_value(self, profile: ReferenceProfile) -> None:
        # spread: |6 - 3| / (4/3)          = 2.25
        # flat:   |10.0005 - 10| / 1e-4    = 5.0
        assert profile.score([6.0, 10.0005]) == pytest.approx(7.25)

    def test_the_enrolment_centre_scores_zero(self, profile: ReferenceProfile) -> None:
        assert profile.score([3.0, 10.0]) == pytest.approx(0.0)

    def test_score_is_the_sum_of_absolute_deviations(self, profile: ReferenceProfile) -> None:
        session = [4.5, 9.9997]
        total = sum(abs(value) for value in profile.deviations(session))
        assert profile.score(session) == pytest.approx(total)

    def test_deviations_keep_their_sign(self, profile: ReferenceProfile) -> None:
        below, above = profile.deviations([1.0, 10.0]), profile.deviations([5.0, 10.0])
        assert below[0] < 0 < above[0]

    def test_a_session_of_the_wrong_width_is_rejected(self, profile: ReferenceProfile) -> None:
        with pytest.raises(ValueError, match="expects 2"):
            profile.score([1.0, 2.0, 3.0])


class TestExplanation:
    def test_features_are_ranked_by_absolute_deviation(self, profile: ReferenceProfile) -> None:
        # "flat" deviates by 5.0 and "spread" by 2.25, so the flat feature leads despite the
        # smaller raw difference. That inversion is the whole point of scaling per feature.
        assert profile.top_deviations([6.0, 10.0005]) == [
            ("flat", pytest.approx(5.0)),
            ("spread", pytest.approx(2.25)),
        ]

    def test_the_ranking_is_truncated_to_the_requested_count(
        self, profile: ReferenceProfile
    ) -> None:
        assert len(profile.top_deviations([6.0, 10.0005], count=1)) == 1

    def test_asking_for_more_features_than_exist_returns_all_of_them(
        self, profile: ReferenceProfile
    ) -> None:
        assert len(profile.top_deviations([6.0, 10.0005], count=99)) == 2


class TestDeviceProfiles:
    def test_a_session_is_scored_against_its_own_device_class(
        self, profile: ReferenceProfile
    ) -> None:
        # A mobile profile centred far away proves the desktop score did not come from it.
        mobile = ReferenceProfile.fit([[100.0, 0.0], [102.0, 1.0]], NAMES)
        profiles = DeviceProfiles({"desktop": profile, "mobile": mobile})
        assert profiles.score("desktop", [6.0, 10.0005]) == pytest.approx(7.25)

    def test_a_class_without_a_profile_has_no_score_and_no_fallback(
        self, profile: ReferenceProfile
    ) -> None:
        profiles = DeviceProfiles({"desktop": profile})
        assert profiles.profile_for("tablet") is None
        assert profiles.score("tablet", [6.0, 10.0005]) is None

    def test_an_unknown_device_class_is_rejected(self, profile: ReferenceProfile) -> None:
        with pytest.raises(ValueError, match="unknown device classes"):
            DeviceProfiles({"smartwatch": profile})  # type: ignore[dict-item]
        with pytest.raises(ValueError, match="unknown device class"):
            DeviceProfiles({"desktop": profile}).score("watch", [1.0, 2.0])  # type: ignore[arg-type]

    def test_profiles_with_different_feature_sets_are_rejected(
        self, profile: ReferenceProfile
    ) -> None:
        other = ReferenceProfile.fit(ENROLMENT, ["a", "b"])
        with pytest.raises(ValueError, match="one feature set"):
            DeviceProfiles({"desktop": profile, "mobile": other})

    def test_the_mapping_cannot_be_mutated_after_construction(
        self, profile: ReferenceProfile
    ) -> None:
        source = {"desktop": profile}
        profiles = DeviceProfiles(source)
        source["mobile"] = profile
        assert profiles.profile_for("mobile") is None
        with pytest.raises(TypeError):
            profiles.by_class["mobile"] = profile  # type: ignore[index]


class TestChannelScore:
    def test_confidence_bounds_are_inclusive(self) -> None:
        ChannelScore(1.0, 0.0)
        ChannelScore(1.0, 1.0)
        with pytest.raises(ValueError, match="outside"):
            ChannelScore(1.0, 1.0001)

    def test_a_non_finite_score_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="finite"):
            ChannelScore(float("inf"), 0.5)


class TestBehaviourChannel:
    FEATURES = {"spread": 6.0, "flat": 10.0005, "unused": 99.0}

    def test_no_profile_is_no_evidence_rather_than_an_anomaly(self) -> None:
        assert scoring.behaviour_channel(None, self.FEATURES, 40, 5) == NO_EVIDENCE

    def test_full_evidence_reports_the_profile_score_at_full_confidence(
        self, profile: ReferenceProfile
    ) -> None:
        result = scoring.behaviour_channel(
            profile,
            self.FEATURES,
            scoring.IDENTITY_FULL_KEYSTROKES,
            scoring.IDENTITY_FULL_SESSIONS,
        )
        assert result.score == pytest.approx(7.25)
        assert result.confidence == 1.0

    def test_confidence_multiplies_keystroke_and_session_sufficiency(
        self, profile: ReferenceProfile
    ) -> None:
        # 10 of 40 keystrokes, 4 of 5 sessions: 0.25 * 0.8
        result = scoring.behaviour_channel(profile, self.FEATURES, 10, 4)
        assert result.confidence == pytest.approx(0.2)

    def test_a_profile_one_session_short_of_cold_start_is_not_fully_trusted(
        self, profile: ReferenceProfile
    ) -> None:
        short = scoring.behaviour_channel(
            profile, self.FEATURES, 1000, scoring.IDENTITY_FULL_SESSIONS - 1
        )
        assert short.confidence < 1.0


def _timing(down_down: tuple[float, ...]) -> KeystrokeTiming:
    count = len(down_down) + 1
    return KeystrokeTiming(
        hold=(0.1,) * count,
        down_down=down_down,
        up_down=tuple(value - 0.1 for value in down_down),
    )


class TestAutomationChannel:
    def test_a_fixed_delay_script_scores_maximum_regularity(self) -> None:
        assert scoring.automation_channel(_timing((0.12,) * 15)).score == pytest.approx(1.0)

    def test_regularity_matches_the_hand_computed_value(self) -> None:
        # down-down 0.5, 0.6: CV 0.1285649; regularity (0.2 - CV) / 0.2
        cv = (2 * 0.05**2) ** 0.5 / 0.55
        result = scoring.automation_channel(_timing((0.5, 0.6)))
        assert result.score == pytest.approx((0.2 - cv) / 0.2)

    def test_a_cv_exactly_at_the_human_floor_scores_zero(self) -> None:
        # Two intervals m - d and m + d have sample CV d * sqrt(2) / m, so d = 0.2 * m / sqrt(2).
        spread = scoring.AUTOMATION_HUMAN_CV * 0.5 / 2**0.5
        timing = _timing((0.5 - spread, 0.5 + spread))
        assert scoring.automation_channel(timing).score == pytest.approx(0.0, abs=1e-12)

    def test_irregular_human_timing_scores_zero(self) -> None:
        assert scoring.automation_channel(_timing((0.2, 0.6, 0.3, 0.9))).score == 0.0

    def test_an_exact_replay_scores_full_overlap(self) -> None:
        human = _timing((0.2, 0.6, 0.3, 0.9, 0.25))
        history = timing_shingles(human)
        assert scoring.automation_channel(human, history).score == 1.0

    def test_partial_overlap_is_the_fraction_of_shingles_seen(self) -> None:
        human = _timing((0.2, 0.6, 0.3, 0.9, 0.25))
        shingles = sorted(timing_shingles(human))
        history = frozenset(shingles[:2])
        expected = 2 / len(shingles)
        assert scoring.automation_channel(human, history).score == pytest.approx(expected)

    def test_unreadable_history_scores_regularity_only(self) -> None:
        human = _timing((0.2, 0.6, 0.3, 0.9, 0.25))
        assert scoring.automation_channel(human, None).score == 0.0

    def test_confidence_saturates_at_the_full_interval_count(self) -> None:
        full = scoring.AUTOMATION_FULL_INTERVALS
        assert scoring.automation_channel(_timing((0.12,) * full)).confidence == 1.0
        short = scoring.automation_channel(_timing((0.12,) * 3))
        assert short.confidence == pytest.approx(3 / full)
