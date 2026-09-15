"""Profile poisoning on the CMU benchmark: policies P0 to P3, and the budget tradeoff curve.

Tier 1 evidence (architecture section 15.1). Every keystroke vector is real CMU data; only the
schedule of who submits which session, and with what trust, is simulated. That schedule is fixed
here, with a fixed seed, and the P3 policy is the deployed ``fraudcore.adaptation`` code.

Protocol, per victim and attacker
---------------------------------
Each subject's eight sessions were recorded on separate days, 50 repetitions each, here in the nine
deployable aggregate features.

    victim session 1      enrolment: bootstraps the profile and seeds the P3 buffer at full trust
    victim session 2      operating threshold: the score that rejects 5% of these repetitions
    victim sessions 3-6   genuine stream, in recording order, so the drift is real drift
    victim sessions 7-8   held out: false rejection under drift
    attacker sessions 1-4 injection stream
    attacker sessions 5-8 held out: the attacker's natural typing

Each of 20 epochs interleaves 10 genuine and 10 attack sessions. The attacker knows the threshold:
each injected session is pulled toward the current profile until it scores just inside it, which is
the optimal attack on P1 and costs the attacker nothing against the others. Impersonation success is
the fraction of the attacker's held-out natural repetitions the final profile accepts.

Trust. Routine genuine sessions are passkey sign-ins on the enrolled device (tau 0.6), and one in
ten is a passkey step-up (tau 1.0). The attacker is the strong case of section 7.8: a hijacked
session on the victim's enrolled device, also tau 0.6, that can never pass a step-up. An attacker
with only a password on a new device has tau 0 and never enters the buffer; that is reported as
one line, not a figure.

Policies (section 7.9)
    P0  static enrolment profile
    P1  EWMA over sessions the current profile accepts
    P2  EWMA over sessions with tau >= tau_min
    P3  tau-gated buffer, weighted geometric median, anchored displacement budgets

Budgets. Both are calibrated before any policy runs, as the 95th percentile of genuine drift
between a subject's consecutive recording days across all 51 subjects: centre displacement in
scaled units for ``budget``, mean relative scale change for ``scale_budget``. The first run of
this experiment used one budget for both; that run is kept, suffixed ``_shared_budget``, as the
record of the flaw it exposed. The tradeoff curve sweeps both budgets by the same multiplier. CMU
spans eight sessions, so calendar re-anchoring never triggers; only step-ups re-anchor.

Usage, from the repository root::

    python -m research.poisoning
"""

from __future__ import annotations

import argparse
import json
import os
import random
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from fraudcore.adaptation import (  # noqa: E402
    PROFILE_FEATURES,
    AdaptationPolicy,
    BufferedSession,
    Profile,
    TrustEvidence,
    admit,
    bounded,
    displacement,
    rebuild,
    scale_displacement,
    trust,
)
from fraudcore.fusion import MODELS_PATH  # noqa: E402
from fraudcore.scoring import ReferenceProfile  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TABLES = REPO_ROOT / "research" / "results" / "tables"
FIGURES = REPO_ROOT / "research" / "results" / "figures"

SEED = 20260915
FEATURES = list(PROFILE_FEATURES)

ENROLMENT_SESSION = 1
THRESHOLD_SESSION = 2
GENUINE_SESSIONS = (3, 4, 5, 6)
HELD_OUT_SESSIONS = (7, 8)
ATTACK_SESSIONS = (1, 2, 3, 4)
ATTACKER_HELD_OUT_SESSIONS = (5, 6, 7, 8)

EPOCHS = 20
PER_EPOCH = 10
TARGET_FRR = 0.05
ATTACK_MARGIN = 0.95
EWMA_ALPHA = 0.05
STEP_UP_RATE = 0.1
ATTACKERS_PER_VICTIM = 2
DRIFT_PERCENTILE = 95
BUDGET_MULTIPLIERS = (0.25, 0.5, 1.0, 2.0, 4.0, 8.0)
SWEEP_EVERY_NTH_VICTIM = 3
POLICIES = ("P0", "P1", "P2", "P3")

ROUTINE = trust(TrustEvidence("passkey_signin", 20, None, None, False, "allow"))
STEP_UP = trust(TrustEvidence("stepup", 20, None, None, False, "allow"))
HIJACKED = trust(TrustEvidence("passkey_signin", 20, None, None, False, "allow"))
PASSWORD_ONLY = trust(TrustEvidence("password", None, None, None, False, "allow"))

POLICY_COLOURS = {"P0": "#898781", "P1": "#eb6834", "P2": "#d4a21a", "P3": "#2a78d6"}
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
GRIDLINE = "#e1e0d9"

Sessions = dict[int, np.ndarray]


def reference(centre: Sequence[float], scale: Sequence[float]) -> ReferenceProfile:
    return ReferenceProfile(
        tuple(FEATURES),
        tuple(float(value) for value in centre),
        tuple(float(value) for value in scale),
        "mad",
        0,
    )


def enrol(rows: np.ndarray, policy: AdaptationPolicy) -> Profile:
    """Bootstrap a profile exactly as the live system does, at full trust."""
    buffer = [BufferedSession(tuple(float(v) for v in row), STEP_UP) for row in rows]
    result = rebuild(None, buffer, tau=STEP_UP, now=0.0, policy=policy)
    if result.profile is None:
        raise ValueError("enrolment has fewer sessions than cold start requires")
    return result.profile


def operating_threshold(profile: ReferenceProfile, rows: np.ndarray) -> float:
    scores = [profile.score(row.tolist()) for row in rows]
    return float(np.quantile(scores, 1 - TARGET_FRR))


def engineer(
    sample: np.ndarray, profile: ReferenceProfile, threshold: float, margin: float = ATTACK_MARGIN
) -> np.ndarray:
    """Pull a session toward the profile until it scores ``margin * threshold``.

    The scaled Manhattan score is homogeneous in the deviation from the centre, so shrinking that
    deviation by a factor shrinks the score by exactly the same factor.
    """
    score = profile.score(sample.tolist())
    limit = margin * threshold
    if score <= limit:
        return sample
    centre = np.asarray(profile.centre)
    return centre + (limit / score) * (sample - centre)


def acceptance(profile: ReferenceProfile, rows: np.ndarray, threshold: float) -> float:
    return float(np.mean([profile.score(row.tolist()) <= threshold for row in rows]))


class Static:
    """P0."""

    saturations = 0

    def __init__(self, profile: Profile) -> None:
        self._reference = reference(profile.centre, profile.scale)

    def observe(self, sample: np.ndarray, tau: float) -> None:
        return None

    def reference(self) -> ReferenceProfile:
        return self._reference


class Ewma:
    """P1 and P2: a running mean of accepted sessions, scale held fixed."""

    saturations = 0

    def __init__(self, profile: Profile, threshold: float | None, tau_min: float | None) -> None:
        self.centre = np.asarray(profile.centre, dtype=float)
        self.scale = profile.scale
        self.threshold = threshold
        self.tau_min = tau_min

    def reference(self) -> ReferenceProfile:
        return reference(self.centre, self.scale)

    def observe(self, sample: np.ndarray, tau: float) -> None:
        if self.threshold is not None:
            accepted = self.reference().score(sample.tolist()) <= self.threshold
        else:
            accepted = self.tau_min is not None and tau >= self.tau_min
        if accepted:
            self.centre = (1 - EWMA_ALPHA) * self.centre + EWMA_ALPHA * sample


class Proposed:
    """P3: the deployed policy, driven through fraudcore."""

    def __init__(self, profile: Profile, enrolment: np.ndarray, policy: AdaptationPolicy) -> None:
        self.profile = profile
        self.policy = policy
        # The enrolment sessions bootstrapped the profile, so they are in the buffer at full trust.
        self.buffer = [BufferedSession(tuple(float(v) for v in row), STEP_UP) for row in enrolment]
        self.pending = 0
        self.batch_tau = 0.0

    @property
    def saturations(self) -> int:
        return self.profile.saturations

    def reference(self) -> ReferenceProfile:
        return reference(self.profile.centre, self.profile.scale)

    def observe(self, sample: np.ndarray, tau: float) -> None:
        if not admit(tau, self.policy):
            return
        session = BufferedSession(tuple(float(v) for v in sample), tau)
        self.buffer = list(bounded([*self.buffer, session], self.policy.buffer_capacity))
        self.pending += 1
        self.batch_tau = max(self.batch_tau, tau)
        if self.pending >= self.policy.rebuild_every:
            result = rebuild(self.profile, self.buffer, self.batch_tau, 0.0, self.policy)
            if result.profile is not None:
                self.profile = result.profile
            self.pending, self.batch_tau = 0, 0.0


def make_learner(
    name: str, profile: Profile, enrolment: np.ndarray, threshold: float, policy: AdaptationPolicy
) -> Static | Ewma | Proposed:
    if name == "P0":
        return Static(profile)
    if name == "P1":
        return Ewma(profile, threshold=threshold, tau_min=None)
    if name == "P2":
        return Ewma(profile, threshold=None, tau_min=policy.tau_min)
    if name == "P3":
        return Proposed(profile, enrolment, policy)
    raise ValueError(f"unknown policy {name!r}")


@dataclass(frozen=True)
class Job:
    victim: str
    attacker: str
    policy: str
    budget: float
    scale_budget: float
    multiplier: float
    attacker_tau: float
    victim_sessions: Sessions
    attacker_sessions: Sessions
    seed: int


def run(job: Job) -> dict[str, Any]:
    policy = replace(AdaptationPolicy.load(), budget=job.budget, scale_budget=job.scale_budget)
    victim, attacker = job.victim_sessions, job.attacker_sessions

    initial = enrol(victim[ENROLMENT_SESSION], policy)
    initial_reference = reference(initial.centre, initial.scale)
    threshold = operating_threshold(initial_reference, victim[THRESHOLD_SESSION])
    learner = make_learner(job.policy, initial, victim[ENROLMENT_SESSION], threshold, policy)

    genuine = np.vstack([victim[s] for s in GENUINE_SESSIONS])
    attacks = np.vstack([attacker[s] for s in ATTACK_SESSIONS])
    held_out = np.vstack([victim[s] for s in HELD_OUT_SESSIONS])
    natural = np.vstack([attacker[s] for s in ATTACKER_HELD_OUT_SESSIONS])

    # The same seed for every policy on a pair, so every policy faces the same step-up schedule.
    rng = random.Random(job.seed)
    trajectory = []
    for epoch in range(EPOCHS):
        for offset in range(PER_EPOCH):
            index = epoch * PER_EPOCH + offset
            learner.observe(genuine[index], STEP_UP if rng.random() < STEP_UP_RATE else ROUTINE)
            crafted = engineer(attacks[index], learner.reference(), threshold)
            learner.observe(crafted, job.attacker_tau)
        trajectory.append(acceptance(learner.reference(), natural, threshold))

    final = learner.reference()
    return {
        "victim": job.victim,
        "attacker": job.attacker,
        "policy": job.policy,
        "budget": job.budget,
        "scale_budget": job.scale_budget,
        "multiplier": job.multiplier,
        "attacker_tau": job.attacker_tau,
        "impersonation": trajectory[-1],
        "baseline_impersonation": acceptance(initial_reference, natural, threshold),
        "false_rejection": 1.0 - acceptance(final, held_out, threshold),
        "displacement": displacement(final.centre, initial.centre, initial.scale),
        "scale_change": scale_displacement(final.dispersion, initial.scale),
        "saturations": learner.saturations,
        "trajectory": trajectory,
    }


def calibrate_budgets(
    sessions: dict[str, Sessions], policy: AdaptationPolicy
) -> tuple[float, float, list[float], list[float]]:
    """95th-percentile genuine drift between consecutive recording days, in each budget's units."""
    centre_drift: list[float] = []
    scale_drift: list[float] = []
    for subject in sorted(sessions):
        profiles = [enrol(sessions[subject][k], policy) for k in sorted(sessions[subject])]
        for earlier, later in zip(profiles, profiles[1:], strict=False):
            centre_drift.append(displacement(later.centre, earlier.centre, earlier.scale))
            scale_drift.append(scale_displacement(later.scale, earlier.scale))
    return (
        float(np.percentile(centre_drift, DRIFT_PERCENTILE)),
        float(np.percentile(scale_drift, DRIFT_PERCENTILE)),
        centre_drift,
        scale_drift,
    )


def _job(
    victim: str,
    attacker: str,
    sessions: dict[str, Sessions],
    seed: int,
    policy: str,
    tau: float,
    budgets: tuple[float, float],
    multiplier: float = 1.0,
) -> Job:
    return Job(
        victim=victim,
        attacker=attacker,
        policy=policy,
        budget=budgets[0] * multiplier,
        scale_budget=budgets[1] * multiplier,
        multiplier=multiplier,
        attacker_tau=tau,
        victim_sessions=sessions[victim],
        attacker_sessions=sessions[attacker],
        seed=seed,
    )


def build_jobs(
    sessions: dict[str, Sessions], victims: list[str], budgets: tuple[float, float]
) -> list[Job]:
    everyone = sorted(sessions)
    jobs: list[Job] = []
    for victim_index, victim in enumerate(victims):
        position = everyone.index(victim)
        for attacker_index in range(1, ATTACKERS_PER_VICTIM + 1):
            attacker = everyone[(position + attacker_index) % len(everyone)]
            seed = SEED + position * 100 + attacker_index
            pair = (victim, attacker, sessions, seed)
            jobs += [_job(*pair, policy, HIJACKED, budgets) for policy in POLICIES]
            jobs.append(_job(*pair, "P3", PASSWORD_ONLY, budgets))
            if victim_index % SWEEP_EVERY_NTH_VICTIM == 0:
                jobs += [
                    _job(*pair, "P3", HIJACKED, budgets, multiplier)
                    for multiplier in BUDGET_MULTIPLIERS
                    if multiplier != 1.0
                ]
    return jobs


def _style(ax: Any) -> None:
    ax.set_facecolor(SURFACE)
    ax.grid(axis="y", color=GRIDLINE, linewidth=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color("#c3c2b7")
    ax.tick_params(colors=INK_MUTED, labelsize=8, length=0)


def plot_policies(trajectory: pd.DataFrame, summary: pd.DataFrame, destination: Path) -> None:
    fig, (left, right) = plt.subplots(1, 2, figsize=(10, 4.4), dpi=150)
    fig.patch.set_facecolor(SURFACE)

    for policy in POLICIES:
        rows = trajectory[trajectory["policy"] == policy]
        left.plot(
            rows["epoch"] + 1,
            rows["impersonation"],
            color=POLICY_COLOURS[policy],
            linewidth=2,
            label=policy,
            linestyle="--" if policy == "P0" else "-",
        )
    left.set_xlabel("Epoch (10 genuine and 10 attack sessions each)", color=INK_SECONDARY)
    left.set_ylabel("Attacker's natural typing accepted", color=INK_SECONDARY)
    left.set_title("Impersonation success under poisoning", loc="left", color=INK, fontsize=10)
    left.set_ylim(0, 1)
    left.legend(frameon=False, fontsize=8, labelcolor=INK_SECONDARY)
    _style(left)

    positions = np.arange(len(POLICIES))
    width = 0.38
    values = summary.set_index("policy").loc[list(POLICIES)]
    colours = [POLICY_COLOURS[p] for p in POLICIES]
    right.bar(positions - width / 2, values["impersonation_mean"], width, color=colours)
    right.bar(
        positions + width / 2, values["false_rejection_mean"], width, color=colours, alpha=0.35
    )
    right.set_xticks(positions, list(POLICIES))
    right.set_ylim(0, 1)
    right.set_title(
        "Final impersonation (solid) and false rejection under drift (faded)",
        loc="left",
        color=INK,
        fontsize=10,
    )
    _style(right)

    fig.tight_layout()
    fig.savefig(destination, facecolor=SURFACE)
    plt.close(fig)


def plot_tradeoff(curve: pd.DataFrame, references: pd.DataFrame, destination: Path) -> None:
    fig, ax = plt.subplots(figsize=(7, 4.8), dpi=150)
    fig.patch.set_facecolor(SURFACE)

    ax.plot(
        curve["false_rejection_mean"],
        curve["impersonation_mean"],
        color=POLICY_COLOURS["P3"],
        linewidth=2,
        marker="o",
        markersize=5,
        label="P3, both budgets swept",
    )
    for _, row in curve.iterrows():
        ax.annotate(
            f"{row['multiplier']:g}x",
            (row["false_rejection_mean"], row["impersonation_mean"]),
            xytext=(6, -3),
            textcoords="offset points",
            fontsize=8,
            color=INK_SECONDARY,
        )
    for _, row in references.iterrows():
        ax.scatter(
            row["false_rejection_mean"],
            row["impersonation_mean"],
            s=60,
            color=POLICY_COLOURS[row["policy"]],
            zorder=3,
            label=f"{row['policy']} (reference)",
        )

    ax.set_xlabel("Genuine false rejection under drift (sessions 7-8)", color=INK_SECONDARY)
    ax.set_ylabel("Impersonation success after poisoning", color=INK_SECONDARY)
    ax.set_title(
        "Displacement budgets: poisoning resistance against drift tolerance",
        loc="left",
        color=INK,
        fontsize=10,
    )
    ax.set_xlim(left=0)
    ax.set_ylim(bottom=0)
    ax.legend(frameon=False, fontsize=8, labelcolor=INK_SECONDARY)
    _style(ax)
    fig.tight_layout()
    fig.savefig(destination, facecolor=SURFACE)
    plt.close(fig)


def write_budgets(
    budget: float, scale_budget: float, samples: int, path: Path = MODELS_PATH
) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    data["adaptation"]["budget"] = round(budget, 4)
    data["adaptation"]["scale_budget"] = round(scale_budget, 4)
    data["adaptation"]["budget_status"] = (
        f"calibrated by research/poisoning.py: {DRIFT_PERCENTILE}th percentile of {samples} "
        "consecutive-session drifts across the 51 CMU subjects, centre and scale separately"
    )
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def load_sessions() -> dict[str, Sessions]:
    from research.dataset import load_benchmark, with_aggregate_features

    frame = with_aggregate_features(load_benchmark())
    return {
        subject: {
            int(index): group[FEATURES].to_numpy(dtype=float)
            for index, group in rows.groupby("sessionIndex", sort=True)
        }
        for subject, rows in frame.groupby("subject", sort=True)
    }


def summarise(results: pd.DataFrame, victims: list[str]) -> dict[str, pd.DataFrame]:
    main_runs = results[(results["attacker_tau"] == HIJACKED) & (results["multiplier"] == 1.0)]
    summary = (
        main_runs.groupby("policy")
        .agg(
            pairs=("victim", "size"),
            impersonation_mean=("impersonation", "mean"),
            baseline_impersonation_mean=("baseline_impersonation", "mean"),
            false_rejection_mean=("false_rejection", "mean"),
            displacement_median=("displacement", "median"),
            scale_change_median=("scale_change", "median"),
            saturations_mean=("saturations", "mean"),
        )
        .reset_index()
    )

    exploded = main_runs.explode("trajectory")
    exploded = exploded.assign(
        epoch=exploded.groupby(level=0).cumcount(),
        impersonation=exploded["trajectory"].astype(float),
    )
    trajectory = exploded.groupby(["policy", "epoch"])["impersonation"].mean().reset_index()

    sweep_victims = set(victims[::SWEEP_EVERY_NTH_VICTIM])
    sweep = results[(results["policy"] == "P3") & (results["attacker_tau"] == HIJACKED)]
    sweep = sweep[sweep["victim"].isin(sweep_victims)]
    curve = (
        sweep.groupby("multiplier")
        .agg(
            budget=("budget", "first"),
            scale_budget=("scale_budget", "first"),
            impersonation_mean=("impersonation", "mean"),
            false_rejection_mean=("false_rejection", "mean"),
            saturations_mean=("saturations", "mean"),
        )
        .reset_index()
        .sort_values("multiplier")
    )
    reference_runs = main_runs[
        main_runs["victim"].isin(sweep_victims) & main_runs["policy"].isin(["P0", "P2"])
    ]
    references = (
        reference_runs.groupby("policy")
        .agg(
            impersonation_mean=("impersonation", "mean"),
            false_rejection_mean=("false_rejection", "mean"),
        )
        .reset_index()
    )
    return {
        "summary": summary,
        "trajectory": trajectory,
        "curve": curve,
        "references": references,
        "gated": results[results["attacker_tau"] == PASSWORD_ONLY],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Poisoning experiment, policies P0 to P3.")
    parser.add_argument("--victims", type=int, default=None, help="limit victims, for a smoke run")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--no-write-model", action="store_true")
    args = parser.parse_args(argv)

    sessions = load_sessions()
    budget, scale_budget, centre_drift, scale_drift = calibrate_budgets(
        sessions, AdaptationPolicy.load()
    )
    print(
        f"Centre budget {budget:.4f} (median drift {np.median(centre_drift):.4f}); "
        f"scale budget {scale_budget:.4f} (median drift {np.median(scale_drift):.4f}); "
        f"{DRIFT_PERCENTILE}th percentile of {len(centre_drift)} consecutive-day drifts"
    )

    victims = sorted(sessions)[: args.victims] if args.victims else sorted(sessions)
    jobs = build_jobs(sessions, victims, (budget, scale_budget))
    print(f"{len(victims)} victims, {len(jobs)} runs on {args.workers} workers")
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        results = pd.DataFrame(pool.map(run, jobs, chunksize=2))

    tables = summarise(results, victims)
    gated = tables["gated"]
    print("\nPolicies, hijacked enrolled-device attacker (tau 0.6):")
    print(tables["summary"].to_string(index=False, float_format=lambda v: f"{v:.4f}"))
    print(
        f"\nPassword-only attacker on a new device (tau {PASSWORD_ONLY:.1f}), P3: impersonation "
        f"{gated['impersonation'].mean():.4f} against {gated['baseline_impersonation'].mean():.4f} "
        "before any poisoning"
    )
    print("\nBudget sweep, P3:")
    print(tables["curve"].to_string(index=False, float_format=lambda v: f"{v:.4f}"))

    TABLES.mkdir(parents=True, exist_ok=True)
    FIGURES.mkdir(parents=True, exist_ok=True)
    tables["summary"].to_csv(TABLES / "poisoning_policies.csv", index=False)
    tables["trajectory"].to_csv(TABLES / "poisoning_trajectory.csv", index=False)
    tables["curve"].to_csv(TABLES / "budget_tradeoff.csv", index=False)
    pd.DataFrame(
        [
            {
                "budget": budget,
                "scale_budget": scale_budget,
                "percentile": DRIFT_PERCENTILE,
                "samples": len(centre_drift),
                "centre_drift_median": float(np.median(centre_drift)),
                "scale_drift_median": float(np.median(scale_drift)),
                "password_only_impersonation": float(gated["impersonation"].mean()),
                "unpoisoned_impersonation": float(gated["baseline_impersonation"].mean()),
            }
        ]
    ).to_csv(TABLES / "adaptation_budget.csv", index=False)
    plot_policies(tables["trajectory"], tables["summary"], FIGURES / "poisoning_policies.png")
    plot_tradeoff(tables["curve"], tables["references"], FIGURES / "budget_tradeoff.png")
    print(f"\nTables and figures written under {TABLES.parent.relative_to(REPO_ROOT)}")

    if not args.no_write_model and not args.victims:
        write_budgets(budget, scale_budget, len(centre_drift))
        print(f"Calibrated budgets written to {MODELS_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
