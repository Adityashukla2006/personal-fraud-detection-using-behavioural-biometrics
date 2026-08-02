"""Detection error metrics: FAR, FRR, ROC and Equal Error Rate.

Score convention, applied consistently everywhere in this project: a score is an **anomaly score**,
so larger means less like the enrolled user. A genuine session should score low and an impostor
session should score high. Every function here assumes that direction, and silently getting it
backwards would invert the ROC curve and produce an EER above 0.5, which is the signature to look
for if a number ever looks wrong.

The two error rates are defined against a decision threshold ``t``, where a session is accepted when
its score is at or below ``t``:

    FAR   false acceptance rate   impostor sessions accepted        (a security failure)
    FRR   false rejection rate    genuine sessions rejected         (a usability failure)

Raising ``t`` accepts more of everything, so FAR rises and FRR falls. The Equal Error Rate is the
value both take at the threshold where they cross. It is reported because it summarises a detector
in one number without first committing to an operating point, which is what makes it comparable
across papers -- and comparability against the published baseline is the whole point of O3.

EER is a summary, not an operating point. The deployed system does not run at the EER threshold; it
maps scores onto four graded response tiers, and those thresholds are chosen separately.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sklearn.metrics import roc_curve

# Impostor is the positive class: the detector's job is to flag impostors, and anomaly scores are
# already oriented so that higher means more impostor-like.
GENUINE_LABEL = 0
IMPOSTOR_LABEL = 1


@dataclass(frozen=True)
class ErrorCurve:
    """FAR and FRR sampled across all achievable thresholds, ordered by increasing threshold."""

    thresholds: np.ndarray
    far: np.ndarray
    frr: np.ndarray

    def at_threshold(self, threshold: float) -> tuple[float, float]:
        """Return ``(far, frr)`` at the sampled threshold nearest the one requested."""
        index = int(np.argmin(np.abs(self.thresholds - threshold)))
        return float(self.far[index]), float(self.frr[index])


def error_curve(genuine_scores: np.ndarray, impostor_scores: np.ndarray) -> ErrorCurve:
    """Compute FAR and FRR across thresholds from the two score distributions."""
    genuine = np.asarray(genuine_scores, dtype=float).ravel()
    impostor = np.asarray(impostor_scores, dtype=float).ravel()

    if genuine.size == 0 or impostor.size == 0:
        raise ValueError("both genuine and impostor scores are required")

    labels = np.concatenate(
        [np.full(genuine.size, GENUINE_LABEL), np.full(impostor.size, IMPOSTOR_LABEL)]
    )
    scores = np.concatenate([genuine, impostor])

    # With impostor as the positive class:
    #   false positive rate = genuine sessions flagged as impostor = FRR
    #   true positive rate  = impostors caught, so 1 - tpr = impostors accepted = FAR
    false_positive_rate, true_positive_rate, thresholds = roc_curve(labels, scores)

    # roc_curve returns thresholds in decreasing order; flip so thresholds increase, which matches
    # the reading that raising the threshold accepts more.
    return ErrorCurve(
        thresholds=thresholds[::-1],
        far=(1.0 - true_positive_rate)[::-1],
        frr=false_positive_rate[::-1],
    )


def equal_error_rate(genuine_scores: np.ndarray, impostor_scores: np.ndarray) -> float:
    """Return the Equal Error Rate, linearly interpolated at the FAR/FRR crossing.

    Interpolation matters at this sample size. With 200 genuine and 250 impostor scores the curve
    is a step function, so simply taking the sampled point where FAR and FRR are closest quantises
    the result to a visibly coarse grid -- enough to shift the third decimal place, which is exactly
    the precision at which the published baseline is quoted.
    """
    curve = error_curve(genuine_scores, impostor_scores)
    far, frr = curve.far, curve.frr

    difference = far - frr

    # Arrays are ordered by increasing threshold, so FAR rises from 0 and FRR falls from 1. The
    # crossing is therefore the first index at which FAR has risen to meet FRR, and `difference`
    # runs negative to positive exactly once.
    crossing = np.flatnonzero(difference >= 0)
    if crossing.size == 0:
        return float(np.clip(far[-1], 0.0, 1.0))

    index = int(crossing[0])
    if index == 0:
        return float(np.clip(far[0], 0.0, 1.0))

    # Linearly interpolate between the bracketing samples, which straddle the crossing with
    # difference[index - 1] < 0 <= difference[index].
    before, after = difference[index - 1], difference[index]
    span = after - before
    weight = 0.0 if span == 0 else -before / span

    eer = far[index - 1] + weight * (far[index] - far[index - 1])
    return float(np.clip(eer, 0.0, 1.0))


@dataclass(frozen=True)
class BenchmarkResult:
    """Per-subject EERs and their mean, which is the figure compared against the published baseline."""

    per_subject: dict[str, float]

    @property
    def mean_eer(self) -> float:
        return float(np.mean(list(self.per_subject.values())))

    @property
    def std_eer(self) -> float:
        return float(np.std(list(self.per_subject.values())))

    def summary(self) -> str:
        values = np.array(list(self.per_subject.values()))
        return (
            f"subjects: {len(values)}\n"
            f"mean EER: {self.mean_eer:.4f}\n"
            f"std  EER: {self.std_eer:.4f}\n"
            f"min/max : {values.min():.4f} / {values.max():.4f}"
        )
