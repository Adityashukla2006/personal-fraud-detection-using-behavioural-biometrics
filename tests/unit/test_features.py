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


def _histogram(**bins: float) -> tuple[float, ...]:
    counts = [0.0] * features.HOURS_PER_DAY
    for hour, count in bins.items():
        counts[int(hour.removeprefix("h"))] = count
    return tuple(counts)


# p50 400, p95 1400, busiest hour 14 with 9 transfers, hour 2 used once.
AGGREGATES = features.UserAggregates(
    amount_p50=400.0,
    amount_p95=1400.0,
    daily_count_p95=4.0,
    hour_histogram=_histogram(h14=9, h2=1),
    history_count=50,
)


class TestTransactionFeatures:
    def test_values_match_hand_calculation(self) -> None:
        # amount_z    (900 - 400) / 1000     = 0.5
        # hour_rarity 1 - (1 + 1) / (9 + 1)  = 0.8
        # velocity    6 / 4                  = 1.5
        transfer = features.Transfer(amount=900.0, hour=2, transfers_24h=6)
        assert features.transaction_features(transfer, AGGREGATES) == pytest.approx(
            {"amount_z": 0.5, "hour_rarity": 0.8, "velocity": 1.5}
        )

    def test_the_busiest_hour_has_zero_rarity(self) -> None:
        transfer = features.Transfer(amount=400.0, hour=14, transfers_24h=1)
        assert features.transaction_features(transfer, AGGREGATES)["hour_rarity"] == 0.0

    def test_an_empty_histogram_has_zero_rarity_everywhere(self) -> None:
        empty = dataclasses.replace(AGGREGATES, hour_histogram=(0.0,) * 24)
        transfer = features.Transfer(amount=400.0, hour=3, transfers_24h=1)
        assert features.transaction_features(transfer, empty)["hour_rarity"] == 0.0

    def test_the_spread_is_floored_when_the_quantiles_coincide(self) -> None:
        # p50 == p95 == 400: the spread floors to a tenth of the median, 40.
        flat = dataclasses.replace(AGGREGATES, amount_p95=400.0)
        transfer = features.Transfer(amount=440.0, hour=14, transfers_24h=1)
        assert features.transaction_features(transfer, flat)["amount_z"] == pytest.approx(1.0)

    def test_invalid_inputs_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="24 bins"):
            dataclasses.replace(AGGREGATES, hour_histogram=(1.0,) * 23)
        with pytest.raises(ValueError, match="p50 <= p95"):
            dataclasses.replace(AGGREGATES, amount_p95=100.0)
        with pytest.raises(ValueError, match="outside 0 to 23"):
            features.Transfer(amount=1.0, hour=24, transfers_24h=1)
        with pytest.raises(ValueError, match="positive"):
            features.Transfer(amount=0.0, hour=1, transfers_24h=1)
        with pytest.raises(ValueError, match="at least 1"):
            features.Transfer(amount=1.0, hour=1, transfers_24h=0)


class TestContextFeatures:
    def _context(self, **overrides: object) -> features.SessionContext:
        base = features.SessionContext(
            device=features.DeviceRecord(session_count=30),
            days_since_credential_change=None,
            days_since_contact_change=None,
            account_sessions=40,
        )
        return dataclasses.replace(base, **overrides)

    def test_values_match_hand_calculation(self) -> None:
        # new device 1.0; credential changed 1.75 days ago: 1 - 1.75 / 7 = 0.75; contact never.
        context = self._context(device=None, days_since_credential_change=1.75)
        assert features.context_features(context) == pytest.approx(
            {"device_novelty": 1.0, "credential_recency": 0.75, "contact_recency": 0.0}
        )

    def test_a_device_at_the_enrolment_count_is_familiar(self) -> None:
        enrolled = features.DeviceRecord(session_count=features.ENROLLED_DEVICE_SESSIONS)
        younger = features.DeviceRecord(session_count=features.ENROLLED_DEVICE_SESSIONS - 1)
        assert features.context_features(self._context(device=enrolled))["device_novelty"] == 0.0
        assert features.context_features(self._context(device=younger))["device_novelty"] == 0.5

    def test_a_change_exactly_at_the_stability_window_no_longer_counts(self) -> None:
        context = self._context(days_since_contact_change=features.STABILITY_WINDOW_DAYS)
        assert features.context_features(context)["contact_recency"] == 0.0

    def test_negative_values_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="days since"):
            self._context(days_since_credential_change=-1.0)
        with pytest.raises(ValueError, match="session_count"):
            features.DeviceRecord(session_count=-1)


class TestPayeeFeatures:
    def test_values_match_hand_calculation(self) -> None:
        # first used 6 days ago: 1 - 6 / 30 = 0.8; unverified; global risk 0.4
        edge = features.PayeeEdge(days_since_first_seen=6.0, verified=False)
        risk = features.PayeeRisk(risk_score=0.4, hours_since_computed=42.0)
        assert features.payee_features(edge, risk) == pytest.approx(
            {"payee_novelty": 0.8, "payee_unverified": 1.0, "global_risk": 0.4}
        )

    def test_a_never_used_payee_is_new_and_unverified(self) -> None:
        assert features.payee_features(None, None) == {
            "payee_novelty": 1.0,
            "payee_unverified": 1.0,
            "global_risk": 0.0,
        }

    def test_a_verified_payee_at_the_new_payee_window_is_established(self) -> None:
        edge = features.PayeeEdge(features.NEW_PAYEE_WINDOW_DAYS, verified=True)
        assert features.payee_features(edge, None)["payee_novelty"] == 0.0
        assert features.payee_features(edge, None)["payee_unverified"] == 0.0

    def test_invalid_inputs_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="outside"):
            features.PayeeRisk(risk_score=1.01, hours_since_computed=0.0)
        with pytest.raises(ValueError, match="hours_since_computed"):
            features.PayeeRisk(risk_score=0.5, hours_since_computed=-1.0)
        with pytest.raises(ValueError, match="days_since_first_seen"):
            features.PayeeEdge(days_since_first_seen=-1.0, verified=True)


class TestDeviceClass:
    def test_no_touch_and_fine_pointer_is_desktop(self) -> None:
        assert features.device_class(0, False, 1080) == "desktop"

    def test_touch_at_the_tablet_breakpoint_is_tablet(self) -> None:
        assert features.device_class(5, True, features.TABLET_MIN_SHORT_SIDE) == "tablet"

    def test_touch_just_below_the_breakpoint_is_mobile(self) -> None:
        assert features.device_class(5, True, features.TABLET_MIN_SHORT_SIDE - 1) == "mobile"

    def test_a_coarse_pointer_without_touch_points_is_not_desktop(self) -> None:
        assert features.device_class(0, True, 400) == "mobile"
