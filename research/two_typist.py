"""Two typists on one account, on the CMU benchmark: does ``fraudcore.batch.two_typist`` see them?

Tier 1 evidence: every session is real typing, pooled four repetitions to a session exactly as the
live scorer pools a transfer form (``research.poisoning.pooled_sessions``). Only which sessions are
put on one account is simulated.

Protocol
--------
Each subject's day-1 typing enrols a profile, whose scale is the metric, as live. An account is
``ACCOUNT_SESSIONS`` pooled sessions drawn from days 3 to 8. A single-typist account draws them all
from the subject; a shared account replaces a share of them with a second subject's sessions from
the same days. The second subject is the next one in order, so every subject is both.

The flag threshold is calibrated on the first half of the subjects, at the separation that falsely
flags ``FALSE_FLAG_RATE`` of their single-typist accounts, and then evaluated on the second half,
so the reported rates are not measured on the accounts that set the threshold. The share rule
(``MIN_TYPIST_SHARE``) applies on top, so a 10% second typist is below what the rule can flag.

Outputs::

    research/results/tables/two_typist.csv      detection rate per share, held-out subjects
    research/results/tables/two_typist_runs.csv every account's separation and share
    research/results/figures/two_typist.png

Usage, from the repository root::

    python -m research.two_typist [--seeds 5]
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from fraudcore.adaptation import AdaptationPolicy  # noqa: E402
from fraudcore.batch import MIN_TYPIST_SHARE, two_typist  # noqa: E402
from research.poisoning import (  # noqa: E402
    FIGURES,
    INK,
    INK_MUTED,
    INK_SECONDARY,
    SURFACE,
    TABLES,
    _style,
    enrol,
    pooled_sessions,
)

SEED = 20260924
ACCOUNT_SESSIONS = 30
SHARES = (0.0, 0.1, 0.2, 0.3, 0.5)
ACCOUNT_DAYS = (3, 4, 5, 6, 7, 8)
FALSE_FLAG_RATE = 0.05
SEEDS = 5


def account(own: np.ndarray, other: np.ndarray, share: float, rng: random.Random) -> np.ndarray:
    borrowed = round(ACCOUNT_SESSIONS * share)
    mine = rng.sample(range(len(own)), ACCOUNT_SESSIONS - borrowed)
    theirs = rng.sample(range(len(other)), borrowed)
    return np.vstack([own[mine], other[theirs]]) if borrowed else own[mine]


def run(seeds: int) -> pd.DataFrame:
    pooled = pooled_sessions()
    subjects = sorted(pooled)
    policy = AdaptationPolicy.load()
    rows = []
    for index, subject in enumerate(subjects):
        partner = subjects[(index + 1) % len(subjects)]
        scale = enrol(pooled[subject][1], policy).scale
        own = np.vstack([pooled[subject][day] for day in ACCOUNT_DAYS])
        other = np.vstack([pooled[partner][day] for day in ACCOUNT_DAYS])
        for seed in range(seeds):
            rng = random.Random(SEED + index * 100 + seed)
            for share in SHARES:
                split = two_typist(account(own, other, share, rng).tolist(), scale)
                assert split is not None
                rows.append(
                    {
                        "subject": subject,
                        "half": "calibration" if index < len(subjects) // 2 else "evaluation",
                        "seed": seed,
                        "share": share,
                        "separation": split.separation,
                        "minority": split.share,
                    }
                )
    return pd.DataFrame(rows)


def summarise(runs: pd.DataFrame) -> tuple[float, pd.DataFrame]:
    calibration = runs[(runs["half"] == "calibration") & (runs["share"] == 0.0)]
    threshold = float(np.quantile(calibration["separation"], 1 - FALSE_FLAG_RATE))
    held_out = runs[runs["half"] == "evaluation"].assign(
        flagged=lambda frame: (
            (frame["separation"] >= threshold) & (frame["minority"] >= MIN_TYPIST_SHARE)
        )
    )
    table = (
        held_out.groupby("share")
        .agg(
            accounts=("subject", "size"),
            flag_rate=("flagged", "mean"),
            separation_median=("separation", "median"),
        )
        .reset_index()
    )
    return threshold, table


def plot(runs: pd.DataFrame, threshold: float, destination: Path) -> None:
    fig, ax = plt.subplots(figsize=(7, 4.4), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    held_out = runs[runs["half"] == "evaluation"]
    data = [held_out[held_out["share"] == share]["separation"] for share in SHARES]
    ax.boxplot(data, tick_labels=[f"{share:.0%}" for share in SHARES], showfliers=False)
    ax.axhline(threshold, color=INK_MUTED, linestyle="--", linewidth=1)
    ax.annotate(
        f"flag threshold {threshold:.2f}",
        xy=(0.55, threshold),
        xytext=(0, 4),
        textcoords="offset points",
        fontsize=8,
        color=INK_SECONDARY,
    )
    ax.set_xlabel("Share of the account's sessions typed by a second person", color=INK_SECONDARY)
    ax.set_ylabel("Separation of the best two-group split", color=INK_SECONDARY)
    ax.set_title(
        "Two typists on one account, held-out CMU subjects", loc="left", color=INK, fontsize=10
    )
    _style(ax)
    fig.tight_layout()
    fig.savefig(destination, facecolor=SURFACE)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description="Two-typist detection on CMU.")
    parser.add_argument("--seeds", type=int, default=SEEDS)
    args = parser.parse_args()
    runs = run(args.seeds)
    threshold, table = summarise(runs)
    TABLES.mkdir(parents=True, exist_ok=True)
    runs.to_csv(TABLES / "two_typist_runs.csv", index=False)
    table.to_csv(TABLES / "two_typist.csv", index=False)
    plot(runs, threshold, FIGURES / "two_typist.png")
    print(f"calibrated separation threshold at {FALSE_FLAG_RATE:.0%} false flags: {threshold:.4f}")
    print(table.round(4).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
