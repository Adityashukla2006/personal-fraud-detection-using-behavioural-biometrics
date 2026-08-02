"""Loading the CMU benchmark and building the evaluation splits.

This module owns two things and deliberately nothing else: reading the verified benchmark file, and
constructing the train/test split used to measure detection error.

The split is not an arbitrary choice. It reproduces the protocol from the source paper, because the
comparison in objective O3 is only meaningful if our error rates are computed the same way the
published ones were. Any deviation here silently invalidates that comparison, so the protocol is
implemented once, in one place, and every experiment draws its splits from here.

Protocol, per subject, treating that subject as genuine and all others as impostors:

    enrolment      the subject's first 200 repetitions, used to build their reference profile
    genuine test   the subject's remaining 200 repetitions
    impostor test  the first 5 repetitions of each of the other 50 subjects, so 250 vectors

Equal Error Rate is computed from those two test score distributions per subject, then averaged
across all 51 subjects.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = REPO_ROOT / "data" / "raw" / "DSL-StrongPasswordData.csv"

# The 31 raw timing columns, in file order. Order is fixed and relied upon downstream: a profile is
# a vector of per-column statistics, so a reordering would silently misalign profiles against
# sessions. Built by prefix rather than hardcoded so it cannot drift from the file.
TIMING_PREFIXES = ("H.", "DD.", "UD.")

ENROLMENT_REPETITIONS = 200
IMPOSTOR_REPETITIONS_PER_SUBJECT = 5


def timing_columns(frame: pd.DataFrame) -> list[str]:
    """Return the 31 timing column names, in the order they appear in the file."""
    return [c for c in frame.columns if c.startswith(TIMING_PREFIXES)]


def load_benchmark(path: Path | None = None) -> pd.DataFrame:
    """Load the benchmark, sorted into the canonical repetition order.

    Rows are sorted by ``(subject, sessionIndex, rep)`` so that "the first 200 repetitions" means
    the same thing regardless of how the file happens to be ordered on disk. The eight sessions were
    recorded on separate days, so this ordering is chronological, which matters: enrolling on the
    earliest repetitions and testing on later ones is what exposes the detector to genuine
    behavioural drift rather than hiding it by shuffling.
    """
    source = path or DEFAULT_PATH
    if not source.exists():
        raise FileNotFoundError(
            f"benchmark not found at {source}. "
            "Run 'python ai-models/download_dataset.py' from the repository root first."
        )

    frame = pd.read_csv(source)
    frame = frame.sort_values(["subject", "sessionIndex", "rep"], kind="stable")
    return frame.reset_index(drop=True)


def subjects(frame: pd.DataFrame) -> list[str]:
    """Return the sorted list of subject identifiers."""
    return sorted(frame["subject"].unique())


@dataclass(frozen=True)
class EvaluationSplit:
    """One subject's enrolment set and the two test sets scored against it.

    Arrays are ``(n_repetitions, n_features)`` with columns in ``feature_names`` order.
    """

    subject: str
    feature_names: list[str]
    enrolment: np.ndarray
    genuine_test: np.ndarray
    impostor_test: np.ndarray

    def __post_init__(self) -> None:
        width = len(self.feature_names)
        for name in ("enrolment", "genuine_test", "impostor_test"):
            array = getattr(self, name)
            if array.ndim != 2 or array.shape[1] != width:
                raise ValueError(
                    f"{name} has shape {array.shape}, expected (n, {width})"
                )


def make_split(
    frame: pd.DataFrame,
    subject: str,
    columns: list[str] | None = None,
    enrolment_repetitions: int = ENROLMENT_REPETITIONS,
    impostor_repetitions: int = IMPOSTOR_REPETITIONS_PER_SUBJECT,
) -> EvaluationSplit:
    """Build the evaluation split for one subject.

    ``columns`` selects which feature representation to use. It defaults to the 31 raw timing
    columns, which is the representation the published baseline is computed over. Passing a
    different column set is how the deployable aggregate representation is evaluated through the
    same protocol.
    """
    features = columns or timing_columns(frame)

    genuine_rows = frame[frame["subject"] == subject]
    if len(genuine_rows) <= enrolment_repetitions:
        raise ValueError(
            f"subject {subject} has {len(genuine_rows)} repetitions, "
            f"need more than {enrolment_repetitions} to leave a genuine test set"
        )

    enrolment = genuine_rows.iloc[:enrolment_repetitions][features].to_numpy(dtype=float)
    genuine_test = genuine_rows.iloc[enrolment_repetitions:][features].to_numpy(dtype=float)

    others = frame[frame["subject"] != subject]
    impostor_rows = others.groupby("subject", sort=True).head(impostor_repetitions)
    impostor_test = impostor_rows[features].to_numpy(dtype=float)

    return EvaluationSplit(
        subject=subject,
        feature_names=list(features),
        enrolment=enrolment,
        genuine_test=genuine_test,
        impostor_test=impostor_test,
    )
