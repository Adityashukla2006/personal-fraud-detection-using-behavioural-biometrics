"""Feature set B over the benchmark, through the deployed ``fraudcore`` feature code."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fraudcore.features import BENCHMARK_FEATURE_NAMES
from research.dataset import DEFAULT_PATH, load_benchmark, with_aggregate_features

SESSION = pd.DataFrame(
    [
        {
            "subject": "s001",
            "H.x": 0.1,
            "DD.x.y": 0.5,
            "UD.x.y": 0.4,
            "H.y": 0.2,
            "DD.y.z": 0.6,
            "UD.y.z": 0.02,
            "H.z": 0.3,
        }
    ]
)


def test_aggregates_are_appended_without_dropping_metadata() -> None:
    combined = with_aggregate_features(SESSION)
    assert "subject" in combined.columns
    assert set(BENCHMARK_FEATURE_NAMES) <= set(combined.columns)
    assert combined.loc[0, "entry_duration"] == pytest.approx(1.4)


def test_missing_timing_columns_are_rejected() -> None:
    with pytest.raises(ValueError, match="timing columns"):
        with_aggregate_features(pd.DataFrame([{"subject": "s001"}]))


@pytest.mark.skipif(not DEFAULT_PATH.exists(), reason="benchmark not downloaded")
def test_aggregates_are_physically_sensible_on_the_real_benchmark() -> None:
    extracted = with_aggregate_features(load_benchmark())[list(BENCHMARK_FEATURE_NAMES)]

    assert len(extracted) == 20_400
    assert np.isfinite(extracted.to_numpy()).all()
    assert (extracted["mean_hold"] > 0).all()
    assert (extracted["entry_duration"] > 0).all()
    assert (extracted["burst_count"] <= 10).all(), "the password has only 10 transitions"
    assert (extracted["pause_count"] <= 10).all()
