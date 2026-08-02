"""Detection baseline: EER, FAR and FRR over the 51 CMU subjects (objective O3).

Replicates the published evaluation so the resulting figure is comparable rather than free
floating. For each subject in turn: build a reference profile from their first 200 repetitions,
score their remaining 200 as genuine and 250 impostor vectors drawn from the other 50 subjects,
take the EER of those two distributions, then average across all subjects.

Both dispersion measures are evaluated. Mean absolute deviation is what the published detector uses
and is the project's default; standard deviation is included so the choice rests on a measured
difference rather than on assertion, which is the open question in issue #1.

Usage, from the repository root::

    python ai-models/experiments/baseline_eer.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dataset import load_benchmark, make_split, subjects, timing_columns  # noqa: E402
from metrics import BenchmarkResult, equal_error_rate, error_curve  # noqa: E402
from scoring import ReferenceProfile, Scaling  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
TABLES = REPO_ROOT / "results" / "tables"

# Killourhy and Maxion (2009) report the scaled Manhattan detector as the best performer on this
# benchmark. VERIFY against the source PDF before this figure goes anywhere near the report -- it is
# quoted here from secondary knowledge and the project doc requires it be taken from the paper.
PUBLISHED_BASELINE_EER = 0.0962
PUBLISHED_BASELINE_LABEL = "Killourhy & Maxion (2009), Manhattan (scaled)"


def evaluate(frame: pd.DataFrame, columns: list[str], scaling: Scaling) -> BenchmarkResult:
    """Run the full 51-subject protocol and return the per-subject EERs."""
    per_subject: dict[str, float] = {}

    for subject in subjects(frame):
        split = make_split(frame, subject, columns=columns)
        profile = ReferenceProfile.fit(split.enrolment, split.feature_names, scaling=scaling)

        genuine = profile.score(split.genuine_test)
        impostor = profile.score(split.impostor_test)

        per_subject[subject] = equal_error_rate(genuine, impostor)

    return BenchmarkResult(per_subject=per_subject)


def operating_points(frame: pd.DataFrame, columns: list[str], scaling: Scaling) -> pd.DataFrame:
    """Report FAR at several fixed FRR targets, averaged across subjects.

    EER is a single summary point. A deployment has to pick an operating point, and the useful
    question there is what fraction of impostors get through when genuine users are inconvenienced
    at a chosen rate, so those are tabulated directly.
    """
    targets = [0.01, 0.05, 0.10, 0.20]
    rows: list[dict[str, float]] = []

    for subject in subjects(frame):
        split = make_split(frame, subject, columns=columns)
        profile = ReferenceProfile.fit(split.enrolment, split.feature_names, scaling=scaling)
        curve = error_curve(
            profile.score(split.genuine_test), profile.score(split.impostor_test)
        )

        row: dict[str, float] = {}
        for target in targets:
            # Strictest threshold whose false rejection rate still meets the target.
            allowed = np.flatnonzero(curve.frr <= target)
            row[f"far_at_frr_{target:.2f}"] = (
                float(curve.far[allowed[0]]) if allowed.size else float("nan")
            )
        rows.append(row)

    return pd.DataFrame(rows).mean().to_frame(name="mean").T


def main() -> int:
    frame = load_benchmark()
    columns = timing_columns(frame)

    print(f"Benchmark: {len(frame):,} rows, {len(subjects(frame))} subjects")
    print(f"Feature set A: {len(columns)} raw timing features\n")

    results: dict[Scaling, BenchmarkResult] = {}
    for scaling in ("mad", "std"):
        results[scaling] = evaluate(frame, columns, scaling)
        print(f"--- dispersion = {scaling} ---")
        print(results[scaling].summary())
        print()

    mad_eer = results["mad"].mean_eer
    std_eer = results["std"].mean_eer

    print(f"Published baseline: {PUBLISHED_BASELINE_EER:.4f}  ({PUBLISHED_BASELINE_LABEL})")
    print(f"This implementation: {mad_eer:.4f}  (MAD)   delta {mad_eer - PUBLISHED_BASELINE_EER:+.4f}")
    print(f"                     {std_eer:.4f}  (std)   delta {std_eer - PUBLISHED_BASELINE_EER:+.4f}")
    print()
    better = "MAD" if mad_eer < std_eer else "standard deviation"
    print(f"Lower error with {better}, by {abs(mad_eer - std_eer):.4f} EER.")

    print("\nMean FAR at fixed FRR targets (MAD):")
    print(operating_points(frame, columns, "mad").to_string(index=False))

    TABLES.mkdir(parents=True, exist_ok=True)
    table = pd.DataFrame(
        {
            "subject": list(results["mad"].per_subject),
            "eer_mad": list(results["mad"].per_subject.values()),
            "eer_std": list(results["std"].per_subject.values()),
        }
    )
    destination = TABLES / "baseline_eer.csv"
    table.to_csv(destination, index=False)
    print(f"\nPer-subject results written to {destination.relative_to(REPO_ROOT)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
