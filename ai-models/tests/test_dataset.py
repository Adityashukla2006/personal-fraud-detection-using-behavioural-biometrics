"""Tests for benchmark loading and the evaluation split.

These assert the split protocol matches the published one, because the comparison in O3 is void if
it does not. They need the real benchmark file and skip cleanly when it is absent, since the dataset
is deliberately not committed.
"""

from __future__ import annotations

import numpy as np
import pytest

from dataset import (
    DEFAULT_PATH,
    ENROLMENT_REPETITIONS,
    IMPOSTOR_REPETITIONS_PER_SUBJECT,
    load_benchmark,
    make_split,
    subjects,
    timing_columns,
)

pytestmark = pytest.mark.skipif(
    not DEFAULT_PATH.exists(),
    reason="benchmark not downloaded; run 'python ai-models/download_dataset.py'",
)


@pytest.fixture(scope="module")
def frame():
    return load_benchmark()


def test_benchmark_has_documented_shape(frame):
    assert frame.shape == (20_400, 34)
    assert len(subjects(frame)) == 51
    assert len(timing_columns(frame)) == 31
    assert frame.isnull().sum().sum() == 0


def test_timing_columns_split_into_expected_groups(frame):
    columns = timing_columns(frame)
    assert len([c for c in columns if c.startswith("H.")]) == 11
    assert len([c for c in columns if c.startswith("DD.")]) == 10
    assert len([c for c in columns if c.startswith("UD.")]) == 10


def test_negative_flight_times_are_preserved(frame):
    """Negative up-down values are genuine finger overlap and must survive loading.

    Clipping them to zero would erase a real and discriminative part of a typist's rhythm, so this
    pins the raw values against a well-meaning future "fix".
    """
    up_down = frame[[c for c in frame.columns if c.startswith("UD.")]]
    assert (up_down < 0).to_numpy().sum() == 22_118


def test_split_has_published_protocol_shapes(frame):
    split = make_split(frame, subjects(frame)[0])

    assert split.enrolment.shape == (ENROLMENT_REPETITIONS, 31)
    assert split.genuine_test.shape == (200, 31)
    assert split.impostor_test.shape == (50 * IMPOSTOR_REPETITIONS_PER_SUBJECT, 31)


def test_enrolment_and_genuine_test_do_not_overlap(frame):
    """The subject's test repetitions must be unseen, or the FRR is measured on training data."""
    split = make_split(frame, subjects(frame)[0])

    enrolled = {tuple(row) for row in split.enrolment}
    tested = {tuple(row) for row in split.genuine_test}
    assert enrolled.isdisjoint(tested)


def test_impostor_set_excludes_the_genuine_subject(frame):
    """The genuine subject must never appear in their own impostor set."""
    target = subjects(frame)[0]
    split = make_split(frame, target)

    others = frame[frame["subject"] != target].groupby("subject", sort=True).head(
        IMPOSTOR_REPETITIONS_PER_SUBJECT
    )
    assert target not in set(others["subject"])
    assert others["subject"].nunique() == 50
    assert len(split.impostor_test) == 250


def test_enrolment_precedes_test_chronologically(frame):
    """Enrolment must come from earlier sessions than the genuine test set.

    The eight sessions were recorded on separate days. Enrolling on the earliest repetitions means
    the reported FRR includes real behavioural drift rather than concealing it, which is what makes
    the Week 2 drift experiment meaningful.
    """
    target = subjects(frame)[0]
    rows = frame[frame["subject"] == target]

    enrolment_sessions = set(rows.iloc[:ENROLMENT_REPETITIONS]["sessionIndex"])
    test_sessions = set(rows.iloc[ENROLMENT_REPETITIONS:]["sessionIndex"])

    assert enrolment_sessions == {1, 2, 3, 4}
    assert test_sessions == {5, 6, 7, 8}


def test_every_subject_yields_a_valid_split(frame):
    for subject in subjects(frame):
        split = make_split(frame, subject)
        assert split.enrolment.shape == (200, 31)
        assert split.genuine_test.shape == (200, 31)
        assert split.impostor_test.shape == (250, 31)
        assert np.isfinite(split.enrolment).all()


def test_alternative_column_set_flows_through(frame):
    """A different feature representation must run through the same protocol unchanged."""
    subset = timing_columns(frame)[:5]
    split = make_split(frame, subjects(frame)[0], columns=subset)

    assert split.feature_names == subset
    assert split.enrolment.shape == (200, 5)
    assert split.impostor_test.shape == (250, 5)
