"""Tests for reference profiles and the scaled Manhattan score.

Cases are hand-computable so they pin the arithmetic itself, independently of the benchmark. The
strongest evidence that the detector as a whole is correct is that it reproduces the published EER
exactly; these cover the pieces that an EER alone would not localise.
"""

from __future__ import annotations

import numpy as np
import pytest

from scoring import DISPERSION_FLOOR, ReferenceProfile

NAMES = ["a", "b"]

# Column a: values 1, 3, 5 -> mean 3, MAD 4/3, sample std 2.
# Column b: constant 10    -> mean 10, dispersion 0, so floored.
ENROLMENT = np.array([[1.0, 10.0], [3.0, 10.0], [5.0, 10.0]])


def test_centre_is_the_feature_mean():
    profile = ReferenceProfile.fit(ENROLMENT, NAMES)
    assert profile.centre == pytest.approx([3.0, 10.0])


def test_mad_dispersion_matches_hand_calculation():
    profile = ReferenceProfile.fit(ENROLMENT, NAMES, scaling="mad")
    assert profile.dispersion[0] == pytest.approx(4.0 / 3.0)


def test_std_dispersion_matches_hand_calculation():
    profile = ReferenceProfile.fit(ENROLMENT, NAMES, scaling="std")
    assert profile.dispersion[0] == pytest.approx(2.0)


def test_constant_feature_is_floored_and_counted():
    """A feature with no enrolment variation must not produce an unbounded score."""
    profile = ReferenceProfile.fit(ENROLMENT, NAMES)

    assert profile.floored_features == 1
    assert profile.dispersion[1] == DISPERSION_FLOOR
    assert np.isfinite(profile.score(np.array([3.0, 11.0]))).all()


def test_the_centre_scores_zero():
    profile = ReferenceProfile.fit(ENROLMENT, NAMES)
    assert profile.score(profile.centre)[0] == pytest.approx(0.0)


def test_score_matches_hand_calculation():
    """|5-3| / (4/3) = 1.5, and the constant feature contributes nothing at its centre."""
    profile = ReferenceProfile.fit(ENROLMENT, NAMES)
    assert profile.score(np.array([5.0, 10.0]))[0] == pytest.approx(1.5)


def test_score_is_symmetric_about_the_centre():
    profile = ReferenceProfile.fit(ENROLMENT, NAMES)
    below = profile.score(np.array([1.0, 10.0]))[0]
    above = profile.score(np.array([5.0, 10.0]))[0]
    assert below == pytest.approx(above)


def test_score_accepts_a_matrix_and_returns_one_value_per_row():
    profile = ReferenceProfile.fit(ENROLMENT, NAMES)
    scores = profile.score(np.array([[3.0, 10.0], [5.0, 10.0], [1.0, 10.0]]))

    assert scores.shape == (3,)
    assert scores[0] == pytest.approx(0.0)
    assert scores[1] == pytest.approx(1.5)


def test_deviations_are_signed_and_sum_to_the_score():
    """The explanation required by O4 must be consistent with the score it explains."""
    profile = ReferenceProfile.fit(ENROLMENT, NAMES)
    session = np.array([5.0, 10.0])

    signed = profile.deviations(session)
    assert signed[0] > 0, "typing above the enrolled centre should deviate positively"
    assert np.abs(signed).sum() == pytest.approx(profile.score(session)[0])

    slower = profile.deviations(np.array([1.0, 10.0]))
    assert slower[0] < 0


def test_top_deviations_are_ordered_by_magnitude():
    profile = ReferenceProfile.fit(ENROLMENT, NAMES)
    top = profile.top_deviations(np.array([5.0, 10.0005]), count=2)

    assert [name for name, _ in top] == ["b", "a"], "the floored feature dominates once it moves"
    assert abs(top[0][1]) >= abs(top[1][1])


def test_more_dispersed_features_are_less_sensitive():
    """Scaling must make a fixed deviation matter less where the user is naturally variable."""
    tight = ReferenceProfile.fit(np.array([[0.0], [0.1], [-0.1]]), ["x"])
    loose = ReferenceProfile.fit(np.array([[0.0], [5.0], [-5.0]]), ["x"])

    assert tight.score(np.array([1.0]))[0] > loose.score(np.array([1.0]))[0]


@pytest.mark.parametrize(
    "enrolment,names",
    [
        (np.array([[1.0, 2.0]]), NAMES),               # only one repetition
        (np.array([[1.0], [2.0]]), NAMES),             # column count disagrees with names
        (np.array([1.0, 2.0, 3.0]), ["a"]),            # not 2-D
    ],
)
def test_invalid_enrolment_is_rejected(enrolment, names):
    with pytest.raises(ValueError):
        ReferenceProfile.fit(enrolment, names)


def test_unknown_scaling_is_rejected():
    with pytest.raises(ValueError, match="unknown scaling"):
        ReferenceProfile.fit(ENROLMENT, NAMES, scaling="median")  # type: ignore[arg-type]


def test_wrong_width_session_is_rejected():
    profile = ReferenceProfile.fit(ENROLMENT, NAMES)
    with pytest.raises(ValueError):
        profile.score(np.array([[1.0, 2.0, 3.0]]))
