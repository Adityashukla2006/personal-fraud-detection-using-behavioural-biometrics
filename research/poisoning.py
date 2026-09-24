"""Profile poisoning on the CMU benchmark: policies P0 to P3, across a fixed set of variants.

Tier 1 evidence (architecture section 15.1). Every keystroke vector is real CMU data; only the
schedule of who submits which session, and with what trust, is simulated. That schedule is fixed
here, with declared seeds, and the P3 policy is the deployed ``fraudcore.adaptation`` code.

Protocol, per victim and attacker
---------------------------------
Each subject's eight sessions were recorded on separate days, 50 repetitions each.

    victim session 1      enrolment: bootstraps the profile and seeds the P3 buffer at full trust
    victim session 2      calibration: the score that rejects 5% of these repetitions
    victim sessions 3-6   genuine stream, days in recording order, so the drift is real drift
    victim sessions 7-8   held out: false rejection under drift
    attacker sessions 1-4 injection stream
    attacker sessions 5-8 held out: the attacker's natural typing

Each of 20 epochs interleaves 10 genuine and 10 attack sessions. The attacker knows the threshold:
each injected session is pulled toward the current profile until it scores just inside it, which is
the optimal attack on P1 and costs the attacker nothing against the others. Impersonation success is
the fraction of the attacker's held-out natural repetitions the final profile accepts.

Sampling and the calendar
-------------------------
A recording day supplies the typing for a block of five consecutive epochs, and the 50 repetitions
of that day are consumed in a seeded random order rather than in file order. Recording days stay in
recording order, so drift between blocks is measured drift; the shuffle only decides which of that
day's repetitions lands on which login, which is the part the dataset does not determine.

Simulated logins carry a clock. An epoch stands for ``EPOCH_DAYS`` of live usage, so the 200 logins
span about 140 days. CMU cannot supply that much typing and the eight recordings span days, not
months, so the calendar is stretched deliberately: without it ``reanchor_days`` never elapses and
the quiet-period re-anchor, which is live policy, is never exercised at all.

Trust, and two regimes
----------------------
Routine genuine sessions are passkey sign-ins on the enrolled device (tau 0.6), and one in ten is a
passkey step-up (tau 1.0). That is the ``signin`` regime, and it describes a system that adapts on
ordinary sign-ins. The deployed system does not: the adaptation Lambda is triggered only by
``stepup.verified``, so every session that can reach a profile has ``c_verify = 1.0``. The
``stepup_only`` regime models that path, with every adapting session at tau 1.0 on both sides.

The attacker is the strong case of section 7.8: a hijacked session on the victim's enrolled device
at tau 0.6, which ``tau_min = 0.5`` admits. Under the deployed trigger that attacker never reaches
adaptation at all, so the deployed threat is the attacker who also holds the authenticator, at tau
1.0, and that attacker re-anchors as well as poisons. Both are run. An attacker with only a password
on a new device has tau 0 and never enters the buffer; that is reported as one line per variant.

Arms
----
Each arm is a protocol, not a policy, and every policy is run under each:

    legacy         frozen threshold, signin regime, attacker tau 0.6 (the first reported protocol)
    recalculated   threshold recalibrated against the current profile, otherwise legacy
    production     recalculated threshold, stepup_only regime, attacker tau 1.0

``legacy`` holds the operating threshold at the one computed from the enrolment profile, so every
later acceptance test judges a profile that has moved by a threshold that has not. A live system
recalibrates, and so does ``recalculated``; the difference between the two is the size of that
confound. ``production`` is the deployed path and is the arm the figures and any adoption come from.

Seeds and intervals
-------------------
Each victim-attacker pair is run under ``--seeds`` seeds, which vary both the step-up schedule and
the within-day sampling. Reported intervals are 95% bootstrap intervals over victim-attacker pairs,
resampling pairs rather than runs because pairs, not schedules, are the independent unit.

Policies (section 7.9)
    P0  static enrolment profile
    P1  EWMA over sessions the current profile accepts
    P2  EWMA over sessions with tau >= tau_min
    P3  tau-gated buffer, weighted geometric median, anchored displacement budgets

Variants
--------
The first run, now ``baseline``, showed P3 accepting the attacker far more often than a static
profile. The variants below were fixed, all of them, before any was run; every one is reported,
whatever it shows. Each changes one thing from the baseline, and the last two combine them.

    baseline         the 9 deployable aggregates; budgets at the 95th percentile of genuine
                     day-to-day drift; scale adapted; re-anchor to the projected profile
    raw_features     the 31 raw per-key timings of the published benchmark representation
    median_budget    budgets at the 50th percentile of drift, so they bind on ordinary movement
    frozen_scale     the scale is never adapted; only the centre learns
    verified_anchor  re-anchor to fully trusted sessions only, so a genuine step-up cannot refresh
                     the attacker's budget
    combined         median budget, frozen scale and verified anchor together
    combined_raw     the combined variant over the 31 raw timings

The live system can only use the 9 aggregates (raw per-key timings mean nothing once the field is
not a fixed password), so the raw variants are evidence about representation, and only a
9-feature variant can be adopted into fraudcore/models.json.

Budgets are calibrated per variant before any policy runs, from genuine drift between a subject's
consecutive recording days, in each budget's own units. The tradeoff curve sweeps both budgets by
one multiplier.

Usage, from the repository root::

    python -m research.poisoning [--configs baseline combined] [--seeds 5] [--adopt combined]
"""

from __future__ import annotations

import argparse
import csv
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
    SECONDS_PER_DAY,
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
# One epoch of ten logins stands for this much calendar time; see the module docstring.
EPOCH_DAYS = 7.0
SEEDS = 5
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_ALPHA = 0.05
# Varying the schedule and the sampling costs a full run each, so only these carry every seed.
MULTI_SEED_CONFIGS = ("baseline", "combined")
# A scale budget of zero freezes the scale in fraudcore, without counting as saturation.
FROZEN_SCALE_BUDGET = 0.0

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


@dataclass(frozen=True)
class Config:
    name: str
    representation: str
    percentile: int
    scale: str
    anchor: str


@dataclass(frozen=True)
class Arm:
    """A measurement protocol. Orthogonal to both the policy and the configuration."""

    name: str
    threshold: str
    regime: str
    attacker_tau: float


SIGNIN = "signin"
STEPUP_ONLY = "stepup_only"
FROZEN_THRESHOLD = "frozen"
RECALCULATED_THRESHOLD = "recalculated"

ARMS: tuple[Arm, ...] = (
    Arm("legacy", FROZEN_THRESHOLD, SIGNIN, HIJACKED),
    Arm("recalculated", RECALCULATED_THRESHOLD, SIGNIN, HIJACKED),
    Arm("production", RECALCULATED_THRESHOLD, STEPUP_ONLY, STEP_UP),
)
PRIMARY_ARM = "production"
# The gated line: an attacker with only a password never reaches tau_min, whatever the arm.
GATED_ARM = Arm("password_only", RECALCULATED_THRESHOLD, SIGNIN, PASSWORD_ONLY)


CONFIGS: tuple[Config, ...] = (
    Config("baseline", "set_b", 95, "adapted", "projected"),
    Config("raw_features", "set_a", 95, "adapted", "projected"),
    Config("median_budget", "set_b", 50, "adapted", "projected"),
    Config("frozen_scale", "set_b", 95, "frozen", "projected"),
    Config("verified_anchor", "set_b", 95, "adapted", "verified"),
    Config("combined", "set_b", 50, "frozen", "verified"),
    Config("combined_raw", "set_a", 50, "frozen", "verified"),
)


def reference(
    centre: Sequence[float], scale: Sequence[float], features: Sequence[str] = FEATURES
) -> ReferenceProfile:
    return ReferenceProfile(
        tuple(features),
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


class Threshold:
    """The operating threshold in force, either held at enrolment or recalibrated.

    Recalibration is what a live system does: the threshold is a false rejection rate against
    genuine typing, so it is recomputed from the calibration day whenever the profile moves. Holding
    it fixed instead judges a profile that has moved by a threshold that has not, which flatters an
    attacker who inflates the scale and penalises one who shifts the centre. Recomputation is keyed
    on the profile itself, so an unchanged profile costs nothing.
    """

    def __init__(self, mode: str, calibration: np.ndarray, initial: float) -> None:
        if mode not in (FROZEN_THRESHOLD, RECALCULATED_THRESHOLD):
            raise ValueError(f"unknown threshold mode {mode!r}")
        self.mode = mode
        self.calibration = calibration
        self.initial = initial
        self._key: tuple[tuple[float, ...], tuple[float, ...]] | None = None
        self._value = initial

    def of(self, profile: ReferenceProfile) -> float:
        if self.mode == FROZEN_THRESHOLD:
            return self.initial
        key = (profile.centre, profile.dispersion)
        if key != self._key:
            self._key = key
            self._value = operating_threshold(profile, self.calibration)
        return self._value


def stream(sessions: Sessions, days: Sequence[int], rng: random.Random) -> np.ndarray:
    """The repetitions a simulated login stream consumes, in order.

    Recording days keep their order, so the drift the stream carries is the drift CMU measured.
    Within a day the 50 repetitions are permuted, because which repetition of a sitting lands on
    which login is not something the dataset decides and should not be fixed across seeds.
    """
    blocks = []
    for day in days:
        rows = sessions[day]
        order = list(range(len(rows)))
        rng.shuffle(order)
        blocks.append(rows[order])
    return np.vstack(blocks)


def timestamp(epoch: int, offset: int) -> float:
    """The clock a simulated login carries, in seconds. See the module docstring."""
    return (epoch + offset / PER_EPOCH) * EPOCH_DAYS * SECONDS_PER_DAY


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

    def __init__(self, profile: Profile, features: Sequence[str] = FEATURES) -> None:
        self._reference = reference(profile.centre, profile.scale, features)

    def observe(self, sample: np.ndarray, tau: float, now: float = 0.0) -> None:
        return None

    def reference(self) -> ReferenceProfile:
        return self._reference


class Ewma:
    """P1 and P2: a running mean of accepted sessions, scale held fixed."""

    saturations = 0

    def __init__(
        self,
        profile: Profile,
        threshold: Threshold | None,
        tau_min: float | None,
        features: Sequence[str] = FEATURES,
    ) -> None:
        self.centre = np.asarray(profile.centre, dtype=float)
        self.scale = profile.scale
        self.threshold = threshold
        self.tau_min = tau_min
        self.features = features

    def reference(self) -> ReferenceProfile:
        return reference(self.centre, self.scale, self.features)

    def observe(self, sample: np.ndarray, tau: float, now: float = 0.0) -> None:
        if self.threshold is not None:
            # P1's admission is the system's own live judgement, threshold included, which is the
            # circularity section 7.1 objects to. It reads the threshold in force, not a stale one.
            current = self.reference()
            accepted = current.score(sample.tolist()) <= self.threshold.of(current)
        else:
            accepted = self.tau_min is not None and tau >= self.tau_min
        if accepted:
            self.centre = (1 - EWMA_ALPHA) * self.centre + EWMA_ALPHA * sample


class Proposed:
    """P3: the deployed policy, driven through fraudcore."""

    def __init__(
        self,
        profile: Profile,
        enrolment: np.ndarray,
        policy: AdaptationPolicy,
        features: Sequence[str] = FEATURES,
    ) -> None:
        self.profile = profile
        self.policy = policy
        self.features = features
        # The enrolment sessions bootstrapped the profile, so they are in the buffer at full trust.
        self.buffer = [BufferedSession(tuple(float(v) for v in row), STEP_UP) for row in enrolment]
        self.pending = 0
        self.batch_tau = 0.0

    @property
    def saturations(self) -> int:
        return self.profile.saturations

    def reference(self) -> ReferenceProfile:
        return reference(self.profile.centre, self.profile.scale, self.features)

    def observe(self, sample: np.ndarray, tau: float, now: float = 0.0) -> None:
        if not admit(tau, self.policy):
            return
        session = BufferedSession(tuple(float(v) for v in sample), tau)
        self.buffer = list(bounded([*self.buffer, session], self.policy.buffer_capacity))
        self.pending += 1
        self.batch_tau = max(self.batch_tau, tau)
        if self.pending >= self.policy.rebuild_every:
            result = rebuild(self.profile, self.buffer, self.batch_tau, now, self.policy)
            if result.profile is not None:
                self.profile = result.profile
            self.pending, self.batch_tau = 0, 0.0


def make_learner(
    name: str,
    profile: Profile,
    enrolment: np.ndarray,
    threshold: Threshold,
    policy: AdaptationPolicy,
    features: Sequence[str] = FEATURES,
) -> Static | Ewma | Proposed:
    if name == "P0":
        return Static(profile, features)
    if name == "P1":
        return Ewma(profile, threshold=threshold, tau_min=None, features=features)
    if name == "P2":
        return Ewma(profile, threshold=None, tau_min=policy.tau_min, features=features)
    if name == "P3":
        return Proposed(profile, enrolment, policy, features)
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
    features: tuple[str, ...] = tuple(FEATURES)
    anchor: str = "projected"
    config: str = "baseline"
    arm: str = "legacy"
    threshold_mode: str = FROZEN_THRESHOLD
    regime: str = SIGNIN


def run(job: Job) -> dict[str, Any]:
    policy = replace(
        AdaptationPolicy.load(),
        budget=job.budget,
        scale_budget=job.scale_budget,
        anchor=job.anchor,  # type: ignore[arg-type]
    )
    victim, attacker = job.victim_sessions, job.attacker_sessions
    features = job.features

    initial = enrol(victim[ENROLMENT_SESSION], policy)
    initial_reference = reference(initial.centre, initial.scale, features)
    calibration = victim[THRESHOLD_SESSION]
    initial_threshold = operating_threshold(initial_reference, calibration)
    threshold = Threshold(job.threshold_mode, calibration, initial_threshold)
    learner = make_learner(
        job.policy, initial, victim[ENROLMENT_SESSION], threshold, policy, features
    )

    # The same seed for every policy on a pair, so every policy faces the same step-up schedule
    # and the same typing samples in the same order.
    rng = random.Random(job.seed)
    genuine = stream(victim, GENUINE_SESSIONS, rng)
    attacks = stream(attacker, ATTACK_SESSIONS, rng)
    held_out = np.vstack([victim[s] for s in HELD_OUT_SESSIONS])
    natural = np.vstack([attacker[s] for s in ATTACKER_HELD_OUT_SESSIONS])

    trajectory = []
    for epoch in range(EPOCHS):
        for offset in range(PER_EPOCH):
            index = epoch * PER_EPOCH + offset
            now = timestamp(epoch, offset)
            if job.regime == STEPUP_ONLY:
                genuine_tau = STEP_UP
            else:
                genuine_tau = STEP_UP if rng.random() < STEP_UP_RATE else ROUTINE
            learner.observe(genuine[index], genuine_tau, now)
            current = learner.reference()
            crafted = engineer(attacks[index], current, threshold.of(current))
            learner.observe(crafted, job.attacker_tau, now)
        current = learner.reference()
        trajectory.append(acceptance(current, natural, threshold.of(current)))

    final = learner.reference()
    final_threshold = threshold.of(final)
    return {
        "config": job.config,
        "arm": job.arm,
        "threshold_mode": job.threshold_mode,
        "regime": job.regime,
        "victim": job.victim,
        "attacker": job.attacker,
        "policy": job.policy,
        "budget": job.budget,
        "scale_budget": job.scale_budget,
        "multiplier": job.multiplier,
        "attacker_tau": job.attacker_tau,
        "seed": job.seed,
        "impersonation": trajectory[-1],
        "baseline_impersonation": acceptance(initial_reference, natural, initial_threshold),
        "false_rejection": 1.0 - acceptance(final, held_out, final_threshold),
        "threshold_ratio": final_threshold / initial_threshold,
        "displacement": displacement(final.centre, initial.centre, initial.scale),
        "scale_change": scale_displacement(final.dispersion, initial.scale),
        "saturations": learner.saturations,
        "trajectory": trajectory,
    }


def calibrate_budgets(
    sessions: dict[str, Sessions], policy: AdaptationPolicy, percentile: int = DRIFT_PERCENTILE
) -> tuple[float, float, list[float], list[float]]:
    """Genuine drift between consecutive recording days, at ``percentile``, per budget's units."""
    centre_drift: list[float] = []
    scale_drift: list[float] = []
    for subject in sorted(sessions):
        profiles = [enrol(sessions[subject][k], policy) for k in sorted(sessions[subject])]
        for earlier, later in zip(profiles, profiles[1:], strict=False):
            centre_drift.append(displacement(later.centre, earlier.centre, earlier.scale))
            scale_drift.append(scale_displacement(later.scale, earlier.scale))
    return (
        float(np.percentile(centre_drift, percentile)),
        float(np.percentile(scale_drift, percentile)),
        centre_drift,
        scale_drift,
    )


def build_jobs(
    sessions: dict[str, Sessions],
    victims: list[str],
    budgets: tuple[float, float],
    config: Config = CONFIGS[0],
    features: Sequence[str] = FEATURES,
    arms: Sequence[Arm] = ARMS,
    seeds: int = 1,
) -> list[Job]:
    everyone = sorted(sessions)
    jobs: list[Job] = []

    def job(
        victim: str,
        attacker: str,
        seed: int,
        policy: str,
        arm: Arm,
        multiplier: float = 1.0,
    ) -> Job:
        return Job(
            victim=victim,
            attacker=attacker,
            policy=policy,
            budget=budgets[0] * multiplier,
            scale_budget=budgets[1] * multiplier,
            multiplier=multiplier,
            attacker_tau=arm.attacker_tau,
            victim_sessions=sessions[victim],
            attacker_sessions=sessions[attacker],
            seed=seed,
            features=tuple(features),
            anchor=config.anchor,
            config=config.name,
            arm=arm.name,
            threshold_mode=arm.threshold,
            regime=arm.regime,
        )

    primary = next((arm for arm in arms if arm.name == PRIMARY_ARM), arms[0])
    for victim_index, victim in enumerate(victims):
        position = everyone.index(victim)
        for attacker_index in range(1, ATTACKERS_PER_VICTIM + 1):
            attacker = everyone[(position + attacker_index) % len(everyone)]
            for replicate in range(seeds):
                seed = SEED + position * 100 + attacker_index + replicate * 1_000_000
                for arm in arms:
                    jobs += [job(victim, attacker, seed, policy, arm) for policy in POLICIES]
                jobs.append(job(victim, attacker, seed, "P3", GATED_ARM))
                # The curve is a property of the budgets, so it is swept on one arm only.
                if victim_index % SWEEP_EVERY_NTH_VICTIM == 0:
                    jobs += [
                        job(victim, attacker, seed, "P3", primary, multiplier)
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


def plot_policies(
    trajectory: pd.DataFrame, summary: pd.DataFrame, destination: Path, title: str = ""
) -> None:
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
    left.set_title(
        f"Impersonation success under poisoning{title}", loc="left", color=INK, fontsize=10
    )
    left.set_ylim(0, 1)
    left.legend(frameon=False, fontsize=8, labelcolor=INK_SECONDARY)
    _style(left)

    positions = np.arange(len(POLICIES))
    width = 0.38
    values = summary.set_index("policy").loc[list(POLICIES)]
    colours = [POLICY_COLOURS[p] for p in POLICIES]

    def errors(metric: str) -> np.ndarray:
        mean = values[f"{metric}_mean"].to_numpy(dtype=float)
        low = mean - values[f"{metric}_lo"].to_numpy(dtype=float)
        high = values[f"{metric}_hi"].to_numpy(dtype=float) - mean
        return np.vstack([np.clip(low, 0, None), np.clip(high, 0, None)])

    bars = {"error_kw": {"ecolor": INK_SECONDARY, "elinewidth": 1, "capsize": 3}}
    right.bar(
        positions - width / 2,
        values["impersonation_mean"],
        width,
        color=colours,
        yerr=errors("impersonation"),
        **bars,
    )
    right.bar(
        positions + width / 2,
        values["false_rejection_mean"],
        width,
        color=colours,
        alpha=0.35,
        yerr=errors("false_rejection"),
        **bars,
    )
    right.set_xticks(positions, list(POLICIES))
    right.set_ylim(0, 1)
    right.set_title(
        "Final impersonation (solid) and false rejection under drift (faded), 95% CI",
        loc="left",
        color=INK,
        fontsize=10,
    )
    _style(right)

    fig.tight_layout()
    fig.savefig(destination, facecolor=SURFACE)
    plt.close(fig)


def plot_tradeoff(
    curve: pd.DataFrame, references: pd.DataFrame, destination: Path, title: str = ""
) -> None:
    fig, ax = plt.subplots(figsize=(7, 4.8), dpi=150)
    fig.patch.set_facecolor(SURFACE)

    mean = curve["impersonation_mean"].to_numpy(dtype=float)
    ax.errorbar(
        curve["false_rejection_mean"],
        mean,
        yerr=np.vstack(
            [
                np.clip(mean - curve["impersonation_lo"].to_numpy(dtype=float), 0, None),
                np.clip(curve["impersonation_hi"].to_numpy(dtype=float) - mean, 0, None),
            ]
        ),
        color=POLICY_COLOURS["P3"],
        ecolor=INK_MUTED,
        elinewidth=1,
        capsize=3,
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
        f"Displacement budgets: poisoning resistance against drift tolerance{title}",
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


def plot_variants(variants: pd.DataFrame, destination: Path) -> None:
    """P3 against P0 for every variant: impersonation, and false rejection under drift."""
    fig, (left, right) = plt.subplots(1, 2, figsize=(11, 4.8), dpi=150, sharey=True)
    fig.patch.set_facecolor(SURFACE)
    positions = np.arange(len(variants))
    height = 0.38

    for ax, metric, heading in (
        (left, "impersonation", "Attacker's natural typing accepted after poisoning"),
        (right, "false_rejection", "Genuine false rejection under drift"),
    ):
        ax.barh(
            positions - height / 2,
            variants[f"P0_{metric}"],
            height,
            color=POLICY_COLOURS["P0"],
            label="P0 static",
        )
        ax.barh(
            positions + height / 2,
            variants[f"P3_{metric}"],
            height,
            color=POLICY_COLOURS["P3"],
            label="P3 proposed",
        )
        ax.set_xlim(0, 1)
        ax.set_title(heading, loc="left", color=INK, fontsize=10)
        ax.set_facecolor(SURFACE)
        ax.grid(axis="x", color=GRIDLINE, linewidth=0.6)
        ax.set_axisbelow(True)
        for side in ("top", "right", "left"):
            ax.spines[side].set_visible(False)
        ax.tick_params(colors=INK_MUTED, labelsize=8, length=0)

    left.set_yticks(positions, list(variants["config"]))
    left.invert_yaxis()
    left.legend(frameon=False, fontsize=8, labelcolor=INK_SECONDARY, loc="lower right")
    fig.tight_layout()
    fig.savefig(destination, facecolor=SURFACE)
    plt.close(fig)


def write_budgets(
    budget: float,
    scale_budget: float,
    samples: int,
    config: Config,
    path: Path = MODELS_PATH,
) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    data["adaptation"]["budget"] = round(budget, 4)
    data["adaptation"]["scale_budget"] = round(scale_budget, 4)
    data["adaptation"]["anchor"] = config.anchor
    data["adaptation"]["budget_status"] = (
        f"adopted from research/poisoning.py variant '{config.name}': "
        f"{config.percentile}th percentile of {samples} consecutive-session drifts "
        f"across the 51 CMU subjects; scale {config.scale}; anchor {config.anchor}; "
        f"evaluated on the '{PRIMARY_ARM}' arm"
    )
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def load_sessions(columns: Sequence[str] | None = None) -> dict[str, Sessions]:
    from research.dataset import load_benchmark, with_aggregate_features

    frame = with_aggregate_features(load_benchmark())
    chosen = list(columns) if columns is not None else FEATURES
    return {
        subject: {
            int(index): group[chosen].to_numpy(dtype=float)
            for index, group in rows.groupby("sessionIndex", sort=True)
        }
        for subject, rows in frame.groupby("subject", sort=True)
    }


def bootstrap_ci(
    runs: pd.DataFrame, column: str, resamples: int = BOOTSTRAP_RESAMPLES, seed: int = SEED
) -> tuple[float, float]:
    """A 95% interval for the mean of ``column``, resampling victim-attacker pairs.

    The pair is the independent unit: several seeds of one pair share a victim, an attacker and
    their drift, so resampling runs would treat those repeats as new evidence and report an interval
    narrower than the experiment earns. Seeds are averaged within a pair first, then pairs are
    resampled with replacement.
    """
    if runs.empty:
        return (float("nan"), float("nan"))
    per_pair = runs.groupby(["victim", "attacker"])[column].mean().to_numpy(dtype=float)
    if len(per_pair) < 2:
        return (float(per_pair[0]), float(per_pair[0]))
    rng = np.random.default_rng(seed)
    draws = rng.integers(0, len(per_pair), size=(resamples, len(per_pair)))
    means = per_pair[draws].mean(axis=1)
    half = BOOTSTRAP_ALPHA / 2
    return float(np.quantile(means, half)), float(np.quantile(means, 1 - half))


def with_intervals(
    runs: pd.DataFrame, keys: Sequence[str], columns: Sequence[str], frame: pd.DataFrame
) -> pd.DataFrame:
    """Attach ``_lo`` and ``_hi`` columns to an already aggregated ``frame``."""
    bounds = []
    for _, row in frame.iterrows():
        mask = np.ones(len(runs), dtype=bool)
        for key in keys:
            mask &= (runs[key] == row[key]).to_numpy()
        group = runs[mask]
        entry: dict[str, float] = {}
        for column in columns:
            entry[f"{column}_lo"], entry[f"{column}_hi"] = bootstrap_ci(group, column)
        bounds.append(entry)
    return pd.concat([frame.reset_index(drop=True), pd.DataFrame(bounds)], axis=1)


def summarise(results: pd.DataFrame, victims: list[str]) -> dict[str, pd.DataFrame]:
    main_runs = results[(results["arm"] != GATED_ARM.name) & (results["multiplier"] == 1.0)]
    summary = (
        main_runs.groupby(["arm", "policy"])
        .agg(
            runs=("victim", "size"),
            pairs=("victim", "nunique"),
            impersonation_mean=("impersonation", "mean"),
            baseline_impersonation_mean=("baseline_impersonation", "mean"),
            false_rejection_mean=("false_rejection", "mean"),
            threshold_ratio_median=("threshold_ratio", "median"),
            displacement_median=("displacement", "median"),
            scale_change_median=("scale_change", "median"),
            saturations_mean=("saturations", "mean"),
        )
        .reset_index()
    )
    summary = with_intervals(
        main_runs, ["arm", "policy"], ["impersonation", "false_rejection"], summary
    )

    exploded = main_runs.explode("trajectory")
    exploded = exploded.assign(
        epoch=exploded.groupby(level=0).cumcount(),
        impersonation=exploded["trajectory"].astype(float),
    )
    trajectory = exploded.groupby(["arm", "policy", "epoch"])["impersonation"].mean().reset_index()

    sweep_victims = set(victims[::SWEEP_EVERY_NTH_VICTIM])
    sweep = results[(results["policy"] == "P3") & (results["arm"] == PRIMARY_ARM)]
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
    curve = with_intervals(sweep, ["multiplier"], ["impersonation", "false_rejection"], curve)
    reference_runs = main_runs[
        (main_runs["arm"] == PRIMARY_ARM)
        & main_runs["victim"].isin(sweep_victims)
        & main_runs["policy"].isin(["P0", "P2"])
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
        "gated": results[results["arm"] == GATED_ARM.name],
    }


def run_config(
    config: Config,
    sessions: dict[str, Sessions],
    features: Sequence[str],
    victims: list[str],
    workers: int,
    arms: Sequence[Arm] = ARMS,
    seeds: int = 1,
) -> tuple[list[dict[str, Any]], float, float, int]:
    base = replace(AdaptationPolicy.load(), anchor=config.anchor)  # type: ignore[arg-type]
    budget, scale_budget, centre_drift, _ = calibrate_budgets(sessions, base, config.percentile)
    if config.scale == "frozen":
        scale_budget = FROZEN_SCALE_BUDGET
    print(
        f"\n=== {config.name}: {config.representation}, {len(features)} features, "
        f"p{config.percentile} budgets, scale {config.scale}, anchor {config.anchor}, "
        f"{seeds} seed(s)"
    )
    print(f"centre budget {budget:.4f}, scale budget {scale_budget:.4g}")

    jobs = build_jobs(sessions, victims, (budget, scale_budget), config, features, arms, seeds)
    with ProcessPoolExecutor(max_workers=workers) as pool:
        results = pd.DataFrame(pool.map(run, jobs, chunksize=2))
    tables = summarise(results, victims)

    directory = TABLES / "poisoning" / config.name
    directory.mkdir(parents=True, exist_ok=True)
    (FIGURES / "poisoning").mkdir(parents=True, exist_ok=True)
    tables["summary"].to_csv(directory / "policies.csv", index=False)
    tables["trajectory"].to_csv(directory / "trajectory.csv", index=False)
    tables["curve"].to_csv(directory / "tradeoff.csv", index=False)

    figure_arm = PRIMARY_ARM if PRIMARY_ARM in set(tables["summary"]["arm"]) else arms[0].name
    suffix = f" ({config.name}, {figure_arm})"
    plot_policies(
        tables["trajectory"][tables["trajectory"]["arm"] == figure_arm],
        tables["summary"][tables["summary"]["arm"] == figure_arm],
        FIGURES / "poisoning" / f"{config.name}_policies.png",
        suffix,
    )
    plot_tradeoff(
        tables["curve"],
        tables["references"],
        FIGURES / "poisoning" / f"{config.name}_tradeoff.png",
        suffix,
    )

    gated = tables["gated"]
    printed = tables["summary"].set_index(["arm", "policy"])
    print(
        printed[
            ["impersonation_mean", "impersonation_lo", "impersonation_hi", "false_rejection_mean"]
        ].round(4)
    )

    rows: list[dict[str, Any]] = []
    for arm in arms:
        if arm.name not in set(tables["summary"]["arm"]):
            continue
        summary = tables["summary"][tables["summary"]["arm"] == arm.name].set_index("policy")
        row: dict[str, Any] = {
            "config": config.name,
            "arm": arm.name,
            "threshold": arm.threshold,
            "regime": arm.regime,
            "attacker_tau": arm.attacker_tau,
            "seeds": seeds,
            "representation": config.representation,
            "features": len(features),
            "percentile": config.percentile,
            "scale": config.scale,
            "anchor": config.anchor,
            "budget": budget,
            "scale_budget": scale_budget,
            "unpoisoned_impersonation": float(summary.loc["P0", "baseline_impersonation_mean"]),
            "password_only_P3_impersonation": float(gated["impersonation"].mean()),
            "P3_saturations_mean": float(summary.loc["P3", "saturations_mean"]),
        }
        for policy in POLICIES:
            for metric in ("impersonation", "false_rejection"):
                row[f"{policy}_{metric}"] = float(summary.loc[policy, f"{metric}_mean"])
                row[f"{policy}_{metric}_lo"] = float(summary.loc[policy, f"{metric}_lo"])
                row[f"{policy}_{metric}_hi"] = float(summary.loc[policy, f"{metric}_hi"])
        rows.append(row)
    return rows, budget, scale_budget, len(centre_drift)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Poisoning experiment, policies P0 to P3.")
    parser.add_argument("--configs", nargs="*", default=[c.name for c in CONFIGS])
    parser.add_argument("--arms", nargs="*", default=[a.name for a in ARMS])
    parser.add_argument(
        "--seeds",
        type=int,
        default=SEEDS,
        help=f"seeds per pair for {', '.join(MULTI_SEED_CONFIGS)}",
    )
    parser.add_argument("--victims", type=int, default=None, help="limit victims, for a smoke run")
    parser.add_argument("--workers", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--adopt", default=None, help="write this 9-feature variant's budgets")
    args = parser.parse_args(argv)

    arms = [arm for arm in ARMS if arm.name in args.arms]
    if not arms:
        parser.error(f"--arms must name at least one of {[a.name for a in ARMS]}")
    if args.seeds < 1:
        parser.error("--seeds must be at least 1")

    from research.dataset import load_benchmark, timing_columns

    chosen = [config for config in CONFIGS if config.name in args.configs]
    raw_columns = timing_columns(load_benchmark())
    representations = {"set_b": FEATURES, "set_a": raw_columns}
    loaded = {
        name: load_sessions(columns)
        for name, columns in representations.items()
        if any(config.representation == name for config in chosen)
    }

    rows = []
    adopted: tuple[Config, float, float, int] | None = None
    for config in chosen:
        sessions = loaded[config.representation]
        victims = sorted(sessions)[: args.victims] if args.victims else sorted(sessions)
        seeds = args.seeds if config.name in MULTI_SEED_CONFIGS else 1
        config_rows, budget, scale_budget, samples = run_config(
            config,
            sessions,
            representations[config.representation],
            victims,
            args.workers,
            arms,
            seeds,
        )
        rows += config_rows
        if config.name == args.adopt:
            adopted = (config, budget, scale_budget, samples)

    variants = pd.DataFrame(rows)
    TABLES.mkdir(parents=True, exist_ok=True)
    variants.to_csv(TABLES / "poisoning_variants.csv", index=False, quoting=csv.QUOTE_MINIMAL)
    figure_arm = PRIMARY_ARM if PRIMARY_ARM in set(variants["arm"]) else arms[0].name
    plot_variants(
        variants[variants["arm"] == figure_arm].reset_index(drop=True),
        FIGURES / "poisoning_variants.png",
    )
    columns = [
        "config",
        "arm",
        "P0_impersonation",
        "P3_impersonation",
        "P0_false_rejection",
        "P3_false_rejection",
        "password_only_P3_impersonation",
        "P3_saturations_mean",
    ]
    print("\n" + variants[columns].round(4).to_string(index=False))

    if args.adopt:
        if adopted is None:
            parser.error(f"--adopt {args.adopt} was not among the variants run")
        config, budget, scale_budget, samples = adopted
        if config.representation != "set_b":
            parser.error("only a 9-feature variant can be adopted by the live system")
        if args.victims:
            parser.error("refusing to adopt budgets from a smoke run")
        if PRIMARY_ARM not in {arm.name for arm in arms}:
            parser.error(f"refusing to adopt without the '{PRIMARY_ARM}' arm")
        write_budgets(budget, scale_budget, samples, config)
        print(f"\nAdopted '{config.name}' into {MODELS_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
