"""Tests for the twelve keystroke features and device-class derivation.

Values are hand-computed from a three-key synthetic entry so the arithmetic is pinned independently
of the benchmark, which is checked separately for physical plausibility.
"""

from __future__ import annotations

import dataclasses

import pytest

from fraudcore import features
from fraudcore.features import KeystrokeTiming

# Three keystrokes.
#   hold        0.1, 0.2, 0.3   -> mean 0.2, sample std 0.1
#   down-down   0.5, 0.6        -> first press to last press, 1.1
#   flight      0.4, 0.02       -> mean 0.21, sample std sqrt(2 * 0.19^2) = 0.268701
#                                  one burst (0.02 < 0.05), no pause, longest 0.4
#   duration    1.1 + final hold 0.3 = 1.4
#   speed       3 / 1.4
#   edits       1 backspace, 2 corrections, 1 paste over 3 keys
TIMING = KeystrokeTiming(
    hold=(0.1, 0.2, 0.3),
    down_down=(0.5, 0.6),
    up_down=(0.4, 0.02),
    backspaces=1,
    corrections=2,
    pastes=1,
)


@pytest.fixture(scope="module")
def extracted() -> dict[str, float]:
    return features.extract(TIMING)


def test_there_are_twelve_features_nine_of_them_benchmark_computable() -> None:
    assert len(features.FEATURE_NAMES) == 12
    assert len(features.BENCHMARK_FEATURE_NAMES) == 9
    assert features.FEATURE_NAMES[:9] == features.BENCHMARK_FEATURE_NAMES


def test_extract_returns_features_in_declared_order(extracted: dict[str, float]) -> None:
    assert tuple(extracted) == features.FEATURE_NAMES


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("mean_hold", 0.2),
        ("std_hold", 0.1),
        ("mean_flight", 0.21),
        ("std_flight", (2 * 0.19**2) ** 0.5),
        ("burst_count", 1.0),
        ("pause_count", 0.0),
        ("longest_pause", 0.4),
        ("entry_duration", 1.4),
        ("typing_speed", 3 / 1.4),
        ("backspace_rate", 1 / 3),
        ("error_correction_rate", 2 / 3),
        ("paste_count", 1.0),
    ],
)
def test_feature_values_match_hand_calculation(
    extracted: dict[str, float], name: str, expected: float
) -> None:
    assert extracted[name] == pytest.approx(expected)


class TestThresholds:
    def _flight(self, first: float) -> dict[str, float]:
        return features.extract(dataclasses.replace(TIMING, up_down=(first, 0.2)))

    def test_a_flight_exactly_at_the_pause_threshold_is_not_a_pause(self) -> None:
        assert self._flight(features.PAUSE_THRESHOLD_SECONDS)["pause_count"] == 0.0

    def test_a_flight_above_the_pause_threshold_is_a_pause(self) -> None:
        assert self._flight(features.PAUSE_THRESHOLD_SECONDS + 1e-6)["pause_count"] == 1.0

    def test_a_flight_exactly_at_the_burst_threshold_is_not_a_burst(self) -> None:
        assert self._flight(features.BURST_THRESHOLD_SECONDS)["burst_count"] == 0.0

    def test_a_negative_flight_counts_as_a_burst(self) -> None:
        assert self._flight(-0.03)["burst_count"] == 1.0


class TestValidation:
    def test_three_keystrokes_is_the_minimum(self) -> None:
        with pytest.raises(ValueError, match="at least 3"):
            KeystrokeTiming(hold=(0.1, 0.2), down_down=(0.5,), up_down=(0.4,))

    def test_interval_counts_must_match_the_keystroke_count(self) -> None:
        with pytest.raises(ValueError, match="3 holds need 2"):
            KeystrokeTiming(hold=(0.1, 0.2, 0.3), down_down=(0.5,), up_down=(0.4, 0.3))

    def test_a_negative_hold_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="negative duration"):
            dataclasses.replace(TIMING, hold=(0.1, -0.2, 0.3))

    def test_a_negative_down_down_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="negative interval"):
            dataclasses.replace(TIMING, down_down=(0.5, -0.1))

    def test_a_non_finite_value_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-finite"):
            dataclasses.replace(TIMING, up_down=(float("nan"), 0.1))

    def test_negative_edit_counts_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="edit counts"):
            dataclasses.replace(TIMING, pastes=-1)

    def test_a_zero_duration_yields_zero_speed_not_infinity(self) -> None:
        degenerate = KeystrokeTiming(hold=(0.0, 0.0, 0.0), down_down=(0.0, 0.0), up_down=(0.0, 0.0))
        assert features.extract(degenerate)["typing_speed"] == 0.0


class TestFromEvents:
    def test_intervals_are_derived_from_press_and_release_times(self) -> None:
        timing = KeystrokeTiming.from_events([0.0, 0.5, 1.1], [0.1, 0.7, 1.4])
        assert timing.hold == pytest.approx((0.1, 0.2, 0.3))
        assert timing.down_down == pytest.approx((0.5, 0.6))
        assert timing.up_down == pytest.approx((0.4, 0.4))

    def test_mismatched_event_counts_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="key-up"):
            KeystrokeTiming.from_events([0.0, 0.5, 1.1], [0.1, 0.7])


def test_the_timing_payload_carries_no_content_fields() -> None:
    """CLAUDE.md section 2: no key identity or typed content leaves the browser.

    The field set is pinned exactly, so adding any field -- a key code, a character, an amount --
    fails here and has to be argued for rather than slipped in.
    """
    fields = {field.name for field in dataclasses.fields(KeystrokeTiming)}
    assert fields == {"hold", "down_down", "up_down", "backspaces", "corrections", "pastes"}


class TestIntervalCv:
    def test_cv_matches_the_hand_computed_value(self) -> None:
        # down-down 0.5, 0.6: mean 0.55, sample std sqrt(2 * 0.05^2) = 0.0707107
        assert features.interval_cv(TIMING) == pytest.approx((2 * 0.05**2) ** 0.5 / 0.55)

    def test_perfectly_regular_intervals_have_zero_cv(self) -> None:
        regular = dataclasses.replace(TIMING, down_down=(0.1, 0.1))
        assert features.interval_cv(regular) == 0.0

    def test_all_zero_intervals_report_zero_not_a_division_error(self) -> None:
        assert features.interval_cv(dataclasses.replace(TIMING, down_down=(0.0, 0.0))) == 0.0


class TestTimingShingles:
    # Five symbols: holds and down-downs interleaved, 0.1 0.5 0.2 0.6 0.3, i.e. 100 500 200 600 300
    # at 1 ms resolution.
    def test_a_single_window_hash_matches_the_hand_computed_polynomial(self) -> None:
        base = features._HASH_BASE
        expected = (((100 * base + 500) * base + 200) * base + 600) * base + 300
        assert features.timing_shingles(TIMING, window=5) == {expected % features._HASH_MODULUS}

    def test_rolling_matches_hashing_each_window_from_scratch(self) -> None:
        rolled = features.timing_shingles(TIMING, window=2)
        base, modulus = features._HASH_BASE, features._HASH_MODULUS
        pairs = [(100, 500), (500, 200), (200, 600), (600, 300)]
        assert rolled == {(a * base + b) % modulus for a, b in pairs}

    def test_an_entry_exactly_one_window_long_yields_one_shingle(self) -> None:
        assert len(features.timing_shingles(TIMING, window=5)) == 1

    def test_an_entry_shorter_than_the_window_yields_none(self) -> None:
        assert features.timing_shingles(TIMING, window=6) == frozenset()

    def test_sub_resolution_differences_hash_identically(self) -> None:
        jittered = dataclasses.replace(TIMING, hold=(0.1002, 0.2, 0.3))
        assert features.timing_shingles(jittered, window=5) == features.timing_shingles(
            TIMING, window=5
        )

    def test_invalid_parameters_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="window"):
            features.timing_shingles(TIMING, window=0)
        with pytest.raises(ValueError, match="resolution"):
            features.timing_shingles(TIMING, resolution=0.0)


def test_vector_orders_by_the_requested_names(extracted: dict[str, float]) -> None:
    assert features.vector(extracted, ["paste_count", "mean_hold"]) == pytest.approx([1.0, 0.2])


class TestDeviceClass:
    def test_no_touch_and_fine_pointer_is_desktop(self) -> None:
        assert features.device_class(0, False, 1080) == "desktop"

    def test_touch_at_the_tablet_breakpoint_is_tablet(self) -> None:
        assert features.device_class(5, True, features.TABLET_MIN_SHORT_SIDE) == "tablet"

    def test_touch_just_below_the_breakpoint_is_mobile(self) -> None:
        assert features.device_class(5, True, features.TABLET_MIN_SHORT_SIDE - 1) == "mobile"

    def test_a_coarse_pointer_without_touch_points_is_not_desktop(self) -> None:
        assert features.device_class(0, True, 400) == "mobile"
