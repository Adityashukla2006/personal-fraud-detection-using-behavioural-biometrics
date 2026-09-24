"""Tests for the two-typist evaluation's protocol, with no dataset."""

from __future__ import annotations

import random

import numpy as np
import pandas as pd
import pytest

from research import two_typist


def test_an_account_borrows_exactly_the_share_it_is_asked_for() -> None:
    own, other = np.zeros((72, 2)), np.ones((72, 2))
    account = two_typist.account(own, other, 0.3, random.Random(1))
    assert account.shape == (two_typist.ACCOUNT_SESSIONS, 2)
    assert account[:, 0].sum() == round(two_typist.ACCOUNT_SESSIONS * 0.3)


def test_a_single_typist_account_borrows_nothing() -> None:
    own, other = np.zeros((72, 2)), np.ones((72, 2))
    assert two_typist.account(own, other, 0.0, random.Random(1)).sum() == 0


def _runs() -> pd.DataFrame:
    rows = []
    for half in ("calibration", "evaluation"):
        for i in range(100):
            rows.append(
                {
                    "subject": f"s{i}",
                    "half": half,
                    "share": 0.0,
                    "separation": i / 10,
                    "minority": 0.5,
                }
            )
            rows.append(
                {
                    "subject": f"s{i}",
                    "half": half,
                    "share": 0.5,
                    "separation": 20.0,
                    "minority": 0.5,
                }
            )
    return pd.DataFrame(rows)


def test_the_threshold_comes_from_calibration_single_typists_only() -> None:
    threshold, _ = two_typist.summarise(_runs())
    # 95th percentile of 0.0 .. 9.9, ignoring the shared accounts at 20.
    assert threshold == pytest.approx(np.quantile([i / 10 for i in range(100)], 0.95))


def test_held_out_rates_use_the_calibrated_threshold() -> None:
    _, table = two_typist.summarise(_runs())
    rates = dict(zip(table["share"], table["flag_rate"], strict=True))
    assert rates[0.5] == 1.0
    assert rates[0.0] == pytest.approx(0.05, abs=0.011)
