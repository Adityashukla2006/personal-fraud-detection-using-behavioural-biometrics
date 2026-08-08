"""Per-user reference profiles and the scaled Manhattan anomaly score.

PROVISIONAL LOCATION. Per ``docs/work-distribution.md`` the feature and scoring definitions belong in
``backend/shared/`` so that the offline and deployed paths cannot diverge. That module is jointly
owned and its specification is under discussion in issue #1, so this lives here until the spec is
agreed, at which point it moves across unchanged. Nothing in this file depends on the disputed
12-feature question: it operates on whatever column set it is handed.

The detector
------------
A reference profile is a per-feature centre and a per-feature dispersion, estimated from a user's
enrolment repetitions. A new session is scored by its total dispersion-normalised deviation from
that centre:

    score(y) = sum_i  |y_i - centre_i| / dispersion_i

Normalising per feature is what makes the sum meaningful. Hold times and flight times differ in
both scale and natural variability, so an unnormalised Manhattan distance would be dominated by
whichever features happen to be largest, rather than by whichever features are most inconsistent
with this particular user.

Choice of dispersion
--------------------
The published detector this project compares against uses the **mean absolute deviation**, which is
what "scaled" refers to in its name. Standard deviation is offered alongside it only so the two can
be measured against each other rather than argued about -- see ``experiments/baseline_eer.py``,
which reports both. MAD is the default because reproducing the published figure is the point of O3.

MAD is also the more defensible choice on its own terms here: it is far less sensitive to occasional
extreme repetitions, and keystroke timing is full of them, since a user who pauses mid-password
produces one enormous flight time that would inflate a standard deviation and desensitise that
feature for every session afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import numpy as np

Scaling = Literal["mad", "std"]

# Timings in the benchmark are recorded to four decimal places, so 0.0001 s is the measurement
# resolution. A feature whose enrolment dispersion falls below that is constant as far as the
# instrument can tell, and dividing by it would produce an unbounded score from an immeasurably
# small deviation. Flooring at the resolution keeps such features highly sensitive but finite.
DISPERSION_FLOOR = 1e-4


@dataclass(frozen=True)
class ReferenceProfile:
    """A user's enrolled typing behaviour: a centre and a dispersion per feature.

    This is deliberately small. It is a handful of statistics per feature rather than a trained
    model, which is why a profile fits in a single DynamoDB item in the deployed system.
    """

    feature_names: list[str]
    centre: np.ndarray
    dispersion: np.ndarray
    scaling: Scaling
    floored_features: int

    @classmethod
    def fit(
        cls,
        enrolment: np.ndarray,
        feature_names: list[str],
        scaling: Scaling = "mad",
    ) -> "ReferenceProfile":
        """Estimate a reference profile from a user's enrolment repetitions.

        ``enrolment`` is ``(n_repetitions, n_features)`` with columns in ``feature_names`` order.
        """
        samples = np.asarray(enrolment, dtype=float)
        if samples.ndim != 2:
            raise ValueError(f"enrolment must be 2-D, got shape {samples.shape}")
        if samples.shape[1] != len(feature_names):
            raise ValueError(
                f"enrolment has {samples.shape[1]} columns but {len(feature_names)} names"
            )
        if samples.shape[0] < 2:
            raise ValueError("at least two enrolment repetitions are required")

        centre = samples.mean(axis=0)

        if scaling == "mad":
            dispersion = np.abs(samples - centre).mean(axis=0)
        elif scaling == "std":
            dispersion = samples.std(axis=0, ddof=1)
        else:
            raise ValueError(f"unknown scaling {scaling!r}, expected 'mad' or 'std'")

        floored = int((dispersion < DISPERSION_FLOOR).sum())
        dispersion = np.maximum(dispersion, DISPERSION_FLOOR)

        return cls(
            feature_names=list(feature_names),
            centre=centre,
            dispersion=dispersion,
            scaling=scaling,
            floored_features=floored,
        )

    def score(self, sessions: np.ndarray) -> np.ndarray:
        """Return the anomaly score for one or more sessions.

        Accepts a single ``(n_features,)`` vector or an ``(n_sessions, n_features)`` matrix, and
        always returns a 1-D array. Higher means less like the enrolled user, matching the score
        convention assumed throughout ``metrics.py``.
        """
        samples = np.atleast_2d(np.asarray(sessions, dtype=float))
        if samples.shape[1] != len(self.feature_names):
            raise ValueError(
                f"sessions have {samples.shape[1]} features, profile expects {len(self.feature_names)}"
            )

        return (np.abs(samples - self.centre) / self.dispersion).sum(axis=1)

    def deviations(self, session: np.ndarray) -> np.ndarray:
        """Return the signed per-feature contribution of one session, in dispersion units.

        This is the raw material for the feature-level explanation required by O4: the sign gives
        the direction of the deviation and the magnitude gives how far out it sits. Summing the
        absolute values reproduces ``score``.
        """
        vector = np.asarray(session, dtype=float).ravel()
        if vector.size != len(self.feature_names):
            raise ValueError(
                f"session has {vector.size} features, profile expects {len(self.feature_names)}"
            )

        return (vector - self.centre) / self.dispersion

    def top_deviations(self, session: np.ndarray, count: int = 3) -> list[tuple[str, float]]:
        """Return the ``count`` features deviating most, as ``(name, signed_deviation)`` pairs."""
        signed = self.deviations(session)
        order = np.argsort(np.abs(signed))[::-1][:count]
        return [(self.feature_names[i], float(signed[i])) for i in order]
