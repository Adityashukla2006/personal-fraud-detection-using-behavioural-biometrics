"""Tests for the detection error metrics.

The Equal Error Rate is the number this project is judged on, so it is tested against cases whose
answers are known independently of the implementation rather than against previously recorded
output. Three of these are exact by construction and one is exact in the statistical limit.
"""

from __future__ import annotations

import numpy as np
import pytest

from metrics import equal_error_rate, error_curve


def test_perfect_separation_gives_zero_eer():
    """Every genuine score below every impostor score: a threshold exists that makes no errors."""
    genuine = np.array([1.0, 2.0, 3.0, 4.0])
    impostor = np.array([10.0, 11.0, 12.0, 13.0])

    assert equal_error_rate(genuine, impostor) == pytest.approx(0.0, abs=1e-9)


def test_identical_distributions_give_half():
    """Indistinguishable populations: the detector cannot beat a coin toss."""
    values = np.linspace(0.0, 1.0, 500)

    assert equal_error_rate(values, values.copy()) == pytest.approx(0.5, abs=0.01)


def test_inverted_scores_give_eer_near_one():
    """Guards the score convention.

    If genuine sessions score *higher* than impostors, the anomaly-score direction has been
    inverted somewhere upstream. That mistake is silent -- the pipeline still runs and still emits
    a number -- so it is pinned here. An EER above 0.5 in any experiment means this happened.
    """
    genuine = np.array([10.0, 11.0, 12.0, 13.0])
    impostor = np.array([1.0, 2.0, 3.0, 4.0])

    assert equal_error_rate(genuine, impostor) == pytest.approx(1.0, abs=1e-9)


def test_matches_analytic_eer_for_two_gaussians():
    """For two unit-variance normals separated by d, the EER is exactly Phi(-d/2).

    This is the only test that exercises the interpolation on a realistically overlapping pair of
    distributions, which is the regime the real experiment runs in.
    """
    from scipy.stats import norm

    separation = 2.0
    rng = np.random.default_rng(20250801)
    genuine = rng.normal(0.0, 1.0, 40_000)
    impostor = rng.normal(separation, 1.0, 40_000)

    expected = float(norm.cdf(-separation / 2.0))  # 0.1587 for d = 2
    assert equal_error_rate(genuine, impostor) == pytest.approx(expected, abs=0.005)


def test_eer_is_bounded():
    rng = np.random.default_rng(7)
    for _ in range(20):
        genuine = rng.normal(0.0, 1.0, 200)
        impostor = rng.normal(rng.uniform(-3.0, 3.0), 1.0, 250)
        assert 0.0 <= equal_error_rate(genuine, impostor) <= 1.0


def test_error_curve_is_monotone_in_threshold():
    """Raising the threshold accepts more sessions, so FAR must rise and FRR must fall."""
    rng = np.random.default_rng(11)
    curve = error_curve(rng.normal(0.0, 1.0, 300), rng.normal(2.0, 1.0, 300))

    assert np.all(np.diff(curve.thresholds) >= 0)
    assert np.all(np.diff(curve.far) >= -1e-12), "FAR must be non-decreasing in threshold"
    assert np.all(np.diff(curve.frr) <= 1e-12), "FRR must be non-increasing in threshold"


def test_far_and_frr_cross_at_the_eer():
    """At the EER threshold the two error rates should meet."""
    rng = np.random.default_rng(3)
    genuine = rng.normal(0.0, 1.0, 4_000)
    impostor = rng.normal(1.5, 1.0, 4_000)

    curve = error_curve(genuine, impostor)
    eer = equal_error_rate(genuine, impostor)

    crossing = int(np.argmin(np.abs(curve.far - curve.frr)))
    assert curve.far[crossing] == pytest.approx(eer, abs=0.01)
    assert curve.frr[crossing] == pytest.approx(eer, abs=0.01)


@pytest.mark.parametrize("genuine,impostor", [([], [1.0]), ([1.0], []), ([], [])])
def test_empty_input_is_rejected(genuine, impostor):
    with pytest.raises(ValueError):
        equal_error_rate(np.array(genuine), np.array(impostor))
