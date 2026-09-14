"""Detection baseline: EER, FAR and FRR over the 51 CMU subjects (objective O3).

Replicates the published evaluation so the resulting figure is comparable rather than free
floating. For each subject in turn: build a reference profile from their first 200 repetitions,
score their remaining 200 as genuine and 250 impostor vectors drawn from the other 50 subjects,
take the EER of those two distributions, then average across all subjects.

Two representations go through that identical protocol:

    Set A  the 31 raw per-key timing columns, the published comparison
    Set B  the 9 benchmark-computable aggregates from ``fraudcore.features``, what is deployed

The gap between them is the accuracy cost of a representation that works on any input field.

Per-device-class profiles cannot be evaluated here: every CMU repetition was typed on one keyboard,
so the benchmark has exactly one device class. That mechanism is covered by unit tests, not by a
benchmark number.

Usage, from the repository root::

    make baseline
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from fraudcore.features import BENCHMARK_FEATURE_NAMES  # noqa: E402
from fraudcore.scoring import ReferenceProfile, Scaling  # noqa: E402
from research.dataset import (  # noqa: E402
    load_benchmark,
    make_split,
    subjects,
    timing_columns,
    with_aggregate_features,
)
from research.metrics import BenchmarkResult, equal_error_rate, error_curve  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TABLES = REPO_ROOT / "research" / "results" / "tables"
FIGURES = REPO_ROOT / "research" / "results" / "figures"

# Killourhy and Maxion (2009) report the scaled Manhattan detector as the best performer on this
# benchmark. VERIFY against the source PDF before this figure goes anywhere near the report -- it is
# quoted here from secondary knowledge and the project doc requires it be taken from the paper.
PUBLISHED_BASELINE_EER = 0.0962
PUBLISHED_BASELINE_LABEL = "Killourhy & Maxion (2009), Manhattan (scaled)"

# Reference palette, light surface: categorical slots 1 and 2, ink and chrome.
SET_A_COLOUR = "#2a78d6"
SET_B_COLOUR = "#eb6834"
SURFACE = "#fcfcfb"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRIDLINE = "#e1e0d9"


def score_all(profile: ReferenceProfile, sessions: np.ndarray) -> np.ndarray:
    """Score every row of a session matrix with the pure-Python detector.

    ``fraudcore`` scores one session at a time, because that is all the Lambda ever needs and
    adding a batch path would be a second code path to keep correct. The experiment loops here
    instead, which keeps the deployed and measured detector byte-for-byte the same function.
    """
    return np.array([profile.score(row.tolist()) for row in sessions], dtype=float)


def evaluate(frame: pd.DataFrame, columns: list[str], scaling: Scaling) -> BenchmarkResult:
    """Run the full 51-subject protocol and return the per-subject EERs."""
    per_subject: dict[str, float] = {}

    for subject in subjects(frame):
        split = make_split(frame, subject, columns=columns)
        profile = ReferenceProfile.fit(split.enrolment, split.feature_names, scaling=scaling)

        genuine = score_all(profile, split.genuine_test)
        impostor = score_all(profile, split.impostor_test)

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
            score_all(profile, split.genuine_test), score_all(profile, split.impostor_test)
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


def plot_per_subject(set_a: BenchmarkResult, set_b: BenchmarkResult, destination: Path) -> None:
    """Per-subject EER for both representations, subjects ordered by their Set A error."""
    order = sorted(set_a.per_subject, key=set_a.per_subject.__getitem__)
    rank = np.arange(1, len(order) + 1)
    a = np.array([set_a.per_subject[s] for s in order])
    b = np.array([set_b.per_subject[s] for s in order])

    fig, ax = plt.subplots(figsize=(9, 4.8), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)

    ax.vlines(rank, a, b, color=GRIDLINE, linewidth=1, zorder=1)
    for values, colour, label in (
        (a, SET_A_COLOUR, f"Set A, 31 raw per-key features (mean {set_a.mean_eer:.4f})"),
        (b, SET_B_COLOUR, f"Set B, 9 deployable aggregates (mean {set_b.mean_eer:.4f})"),
    ):
        ax.scatter(
            rank, values, s=28, color=colour, edgecolors=SURFACE, linewidths=1.5,
            label=label, zorder=3,
        )

    ax.axhline(PUBLISHED_BASELINE_EER, color=INK_MUTED, linewidth=1, linestyle="--", zorder=2)
    ax.annotate(
        f"published baseline {PUBLISHED_BASELINE_EER:.4f}",
        xy=(1, PUBLISHED_BASELINE_EER), xytext=(0, 4), textcoords="offset points",
        color=INK_SECONDARY, fontsize=8,
    )

    ax.set_xlabel("Subject, ordered by Set A EER", color=INK_SECONDARY)
    ax.set_ylabel("Equal Error Rate", color=INK_SECONDARY)
    ax.set_title(
        "Per-subject EER on the CMU benchmark, scaled Manhattan detector",
        loc="left", color="#0b0b0b", fontsize=11,
    )
    ax.set_xlim(0, len(order) + 1)
    ax.set_ylim(bottom=0)
    ax.grid(axis="y", color=GRIDLINE, linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color("#c3c2b7")
    ax.tick_params(colors=INK_MUTED, labelsize=8, length=0)
    ax.legend(loc="upper left", frameon=False, fontsize=8, labelcolor=INK_SECONDARY)

    fig.tight_layout()
    fig.savefig(destination, facecolor=SURFACE)
    plt.close(fig)


def main() -> int:
    frame = with_aggregate_features(load_benchmark())
    set_a_columns = timing_columns(frame)
    set_b_columns = list(BENCHMARK_FEATURE_NAMES)

    print(f"Benchmark: {len(frame):,} rows, {len(subjects(frame))} subjects")
    print(f"Set A: {len(set_a_columns)} raw timing features (published-baseline comparison)")
    print(f"Set B: {len(set_b_columns)} aggregate features (deployable representation)\n")

    results: dict[Scaling, BenchmarkResult] = {}
    for scaling in ("mad", "std"):
        results[scaling] = evaluate(frame, set_a_columns, scaling)
        print(f"--- Set A, dispersion = {scaling} ---")
        print(results[scaling].summary())
        print()

    mad_eer = results["mad"].mean_eer
    std_eer = results["std"].mean_eer

    print(f"Published baseline: {PUBLISHED_BASELINE_EER:.4f}  ({PUBLISHED_BASELINE_LABEL})")
    mad_delta = mad_eer - PUBLISHED_BASELINE_EER
    std_delta = std_eer - PUBLISHED_BASELINE_EER
    print(f"This implementation: {mad_eer:.4f}  (MAD)   delta {mad_delta:+.4f}")
    print(f"                     {std_eer:.4f}  (std)   delta {std_delta:+.4f}")
    better = "MAD" if mad_eer < std_eer else "standard deviation"
    print(f"Lower error with {better}, by {abs(mad_eer - std_eer):.4f} EER.\n")

    set_b = evaluate(frame, set_b_columns, "mad")
    print("--- Set B, dispersion = mad ---")
    print(set_b.summary())
    print(
        f"\nCost of the deployable representation: {set_b.mean_eer - mad_eer:+.4f} EER "
        f"({mad_eer:.4f} -> {set_b.mean_eer:.4f})"
    )

    print("\nMean FAR at fixed FRR targets (Set A, MAD):")
    print(operating_points(frame, set_a_columns, "mad").to_string(index=False))
    print("\nMean FAR at fixed FRR targets (Set B, MAD):")
    print(operating_points(frame, set_b_columns, "mad").to_string(index=False))

    TABLES.mkdir(parents=True, exist_ok=True)
    table = pd.DataFrame(
        {
            "subject": list(results["mad"].per_subject),
            "eer_set_a_mad": list(results["mad"].per_subject.values()),
            "eer_set_a_std": list(results["std"].per_subject.values()),
            "eer_set_b_mad": [set_b.per_subject[s] for s in results["mad"].per_subject],
        }
    )
    table_path = TABLES / "baseline_eer.csv"
    table.to_csv(table_path, index=False)
    print(f"\nPer-subject results written to {table_path.relative_to(REPO_ROOT)}")

    FIGURES.mkdir(parents=True, exist_ok=True)
    figure_path = FIGURES / "baseline_eer.png"
    plot_per_subject(results["mad"], set_b, figure_path)
    print(f"Figure written to {figure_path.relative_to(REPO_ROOT)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
