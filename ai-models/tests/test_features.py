"""Tests for the Set B aggregate feature extraction.

Values are hand-computed from a three-key synthetic session so the arithmetic is pinned
independently of the benchmark, then the real benchmark is used to check the aggregates stay
physically sensible across all 20,400 rows.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

import features
from dataset import DEFAULT_PATH, load_benchmark

# Three keystrokes x, y, z.
#   hold        0.1, 0.2, 0.3   -> mean 0.2, sample std 0.1
#   down-down   0.5, 0.6        -> spans first keypress to last keypress, 1.1
#   flight      0.4, 0.02       -> mean 0.21, sample std 0.2687, one burst, no pause
#   duration    1.1 + final hold 0.3 = 1.4
#   speed       3 keys / 1.4 s  = 2.142857
SESSION = pd.DataFrame(
    [{"H.x": 0.1, "H.y": 0.2, "H.z": 0.3, "DD.x.y": 0.5, "DD.y.z": 0.6, "UD.x.y": 0.4, "UD.y.z": 0.02}]
)


@pytest.fixture(scope="module")
def extracted():
    return features.extract(SESSION).iloc[0]


def test_returns_the_nine_features_in_declared_order():
    result = features.extract(SESSION)
    assert list(result.columns) == features.FEATURE_NAMES
    assert len(features.FEATURE_NAMES) == 9


@pytest.mark.parametrize(
    "name,expected",
    [
        ("mean_hold", 0.2),
        ("std_hold", 0.1),
        ("mean_flight", 0.21),
        ("std_flight", 0.26870057685088806),
        ("burst_count", 1.0),
        ("pause_count", 0.0),
        ("longest_pause", 0.4),
        ("entry_duration", 1.4),
        ("typing_speed", 3.0 / 1.4),
    ],
)
def test_feature_values_match_hand_calculation(extracted, name, expected):
    assert extracted[name] == pytest.approx(expected)


def test_entry_duration_spans_first_press_to_last_release():
    """Summing down-down reaches the last keypress; the final hold extends to its release."""
    assert extracted_duration() == pytest.approx(0.5 + 0.6 + 0.3)


def extracted_duration() -> float:
    return float(features.extract(SESSION).iloc[0]["entry_duration"])


def test_pause_count_responds_to_the_threshold():
    slow = SESSION.copy()
    slow["UD.x.y"] = features.PAUSE_THRESHOLD_SECONDS + 0.1

    assert features.extract(slow).iloc[0]["pause_count"] == 1.0


def test_negative_flight_counts_as_a_burst():
    """Overlapping keystrokes give negative up-down intervals and are the fastest transitions."""
    overlapped = SESSION.copy()
    overlapped["UD.x.y"] = -0.03

    assert features.extract(overlapped).iloc[0]["burst_count"] == 2.0


def test_non_positive_duration_does_not_produce_infinite_speed():
    """Cannot arise in the benchmark, but a live clock anomaly must not emit an infinity."""
    degenerate = SESSION.copy()
    degenerate[["DD.x.y", "DD.y.z", "H.z"]] = 0.0

    assert features.extract(degenerate).iloc[0]["typing_speed"] == 0.0


def test_missing_timing_columns_are_rejected():
    with pytest.raises(ValueError, match="timing columns"):
        features.extract(pd.DataFrame([{"subject": "s001", "rep": 1}]))


def test_with_features_appends_without_dropping_metadata():
    combined = features.with_features(SESSION.assign(subject="s001", sessionIndex=1, rep=1))

    assert {"subject", "sessionIndex", "rep"} <= set(combined.columns)
    assert set(features.FEATURE_NAMES) <= set(combined.columns)


@pytest.mark.skipif(not DEFAULT_PATH.exists(), reason="benchmark not downloaded")
def test_aggregates_are_physically_sensible_on_the_real_benchmark():
    extracted = features.extract(load_benchmark())

    assert len(extracted) == 20_400
    assert np.isfinite(extracted.to_numpy()).all()

    assert (extracted["mean_hold"] > 0).all(), "hold times are durations and cannot be negative"
    assert (extracted["entry_duration"] > 0).all()
    assert (extracted["typing_speed"] > 0).all()
    assert (extracted["burst_count"] <= 10).all(), "only 10 transitions exist in the password"
    assert (extracted["pause_count"] <= 10).all()
