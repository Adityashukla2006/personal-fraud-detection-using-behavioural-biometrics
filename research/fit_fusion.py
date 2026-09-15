"""Fits the fusion model (architecture section 8.2) and calibrates the action thresholds.

Tier 2 evidence (section 15.1). The typing is real CMU data, but device, payee and amount context
is synthetic, and the attacker behaviour is the simulator's, written by the same people who wrote
the defence. The fitted weights make the deployed system coherent; the detection rates below are
evidence that the logic behaves as designed, not measured real-world performance.

Sessions
--------
Built offline from the frozen attack classes in ``simulator/attacks.py`` and the same baseline
state the live simulator seeds: a desktop profile from pooled day-1 typing, an enrolled device,
account history, 30-day aggregates, a verified known payee, and a replay history holding the
enrolment typing's timing shingles. Each session is scored by ``fraudcore.session.channel_scores``,
the function the scoring Lambda calls, so the model is fitted on exactly the channel scores the
deployed system computes.

Subjects. The live simulator attacks the first 10 CMU subjects, so they are excluded entirely. Of
the other 41, the first 28 fit the model and the last 13 evaluate it, so no evaluated subject's
typing was seen during fitting.

Model
-----
Standardisation: per channel, the mean and standard deviation of the raw score over genuine
training sessions, the reference against which "unusual" is measured.

Weights: logistic regression on each channel's confidence-shrunk, clipped standardised score --
exactly ``FusionModel.representation`` -- with every weight constrained non-negative, because a
riskier channel score must never lower risk, and light L2 regularisation. Classes are balanced so
the attack mix does not decide the intercept. Fitted by bounded L-BFGS.

Thresholds: from the fitted risk of genuine training sessions. Monitor at the 80th percentile and
step-up at the 95th, so about 5% of genuine sessions meet friction; restrict and block above that.
The policy rules outside the model still apply regardless: behaviour alone never exceeds step-up,
and a block needs corroboration.

Writes fraudcore/models.json unless --dry-run, and tables fusion_fit.csv and fusion_evaluation.csv:
held-out detection per class and genuine friction, the placeholder model against the fitted one.

Usage, from the repository root::

    python -m research.fit_fusion [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy.optimize import minimize

from fraudcore.adaptation import PROFILE_FEATURES, AdaptationPolicy, BufferedSession, rebuild
from fraudcore.features import (
    DeviceRecord,
    KeystrokeTiming,
    PayeeEdge,
    SessionContext,
    Transfer,
    UserAggregates,
    timing_shingles,
    vector,
)
from fraudcore.fusion import MODELS_PATH, FusionModel, fuse, load_models
from fraudcore.policy import ACTIONS, Thresholds, decide
from fraudcore.scoring import CHANNELS, NO_EVIDENCE, ReferenceProfile
from fraudcore.session import SessionEvidence, channel_scores, pooled_extract
from simulator import attacks, cmu

REPO_ROOT = Path(__file__).resolve().parent.parent
TABLES = REPO_ROOT / "research" / "results" / "tables"

SEED = 20260915
LIVE_VICTIMS = 10
TRAINING_SUBJECTS = 28
# Replay copies enrolment typing four fields at a time, and day 1 has 50 repetitions.
SESSIONS_PER_CLASS = 12
L2 = 0.01
SCALE_FLOOR = 1e-3
ALERT_Z = 2.0
TRANSFER_HOUR = 12

THRESHOLD_PERCENTILES = {"monitor": 80.0, "step_up": 95.0, "restrict": 99.0, "block": 99.9}
MIN_THRESHOLD_GAP = 1.0

# The state the live simulator seeds, kept identical so offline and live scores agree.
AGGREGATES = UserAggregates(1500.0, 6000.0, 3.0, (5.0,) * 24, 60)
ENROLLED_DEVICE = DeviceRecord(30)
ACCOUNT_SESSIONS = 40
KNOWN_PAYEE_DAYS = 200.0


@dataclass(frozen=True)
class Baseline:
    profile: ReferenceProfile
    profile_sessions: int
    replay_history: frozenset[int]


def timing(entry: Mapping[str, Any]) -> KeystrokeTiming:
    return KeystrokeTiming(
        hold=tuple(entry["hold"]),
        down_down=tuple(entry["down_down"]),
        up_down=tuple(entry["up_down"]),
        backspaces=int(entry["backspaces"]),
        corrections=int(entry["corrections"]),
        pastes=int(entry["pastes"]),
    )


def baseline(reps: Sequence[cmu.Repetition], policy: AdaptationPolicy) -> Baseline:
    enrolment = [r for r in reps if r.session == attacks.ENROLMENT_SESSION]
    width = len(attacks.CHECKPOINTS)
    groups = [enrolment[i : i + width] for i in range(0, len(enrolment) - width + 1, width)]
    buffer = [
        BufferedSession(
            tuple(vector(pooled_extract([timing(cmu.field(r)) for r in group]), PROFILE_FEATURES)),
            1.0,
        )
        for group in groups
    ]
    profile = rebuild(None, buffer, 1.0, 0.0, policy).profile
    if profile is None:
        raise ValueError("too little enrolment typing to bootstrap a profile")
    history = frozenset().union(*(timing_shingles(timing(cmu.field(r))) for r in enrolment))
    return Baseline(
        ReferenceProfile(PROFILE_FEATURES, profile.centre, profile.scale, "mad", 0),
        len(buffer),
        history,
    )


def evidence(plan: attacks.SessionPlan, state: Baseline) -> SessionEvidence:
    """The confirmation checkpoint's evidence, exactly as the live store would assemble it."""
    transaction = plan.checkpoints[-1].transaction
    if transaction is None:
        raise ValueError("a session plan must end in a transaction")
    known = transaction["payee_id"] == attacks.payee_id(attacks.known_payee(plan.victim))
    home = plan.device_id == f"sim-home-{plan.victim}"
    return SessionEvidence(
        fields=tuple(timing(checkpoint.field) for checkpoint in plan.checkpoints),
        profile=state.profile,
        profile_sessions=state.profile_sessions,
        context=SessionContext(ENROLLED_DEVICE if home else None, None, None, ACCOUNT_SESSIONS),
        transfer=Transfer(float(transaction["amount"]), TRANSFER_HOUR, 1),
        aggregates=AGGREGATES,
        edge=PayeeEdge(KNOWN_PAYEE_DAYS, True) if known else None,
        risk=None,
        replay_history=state.replay_history,
    )


def build_sessions(
    subjects: Sequence[str], data: Mapping[str, list[cmu.Repetition]], per_class: int
) -> list[dict[str, Any]]:
    everyone = list(data)
    policy = AdaptationPolicy.load()
    rng = random.Random(SEED)
    rows = []
    for subject in subjects:
        attacker = everyone[(everyone.index(subject) + 1) % len(everyone)]
        state = baseline(data[subject], policy)
        for attack in attacks.ATTACK_CLASSES:
            for index in range(per_class):
                plan = attacks.plan_session(
                    attack, subject, data[subject], data[attacker], index,
                    f"sim-home-{subject}", rng,
                )
                rows.append(
                    {
                        "subject": subject,
                        "attack": attack,
                        "label": int(attack != "genuine"),
                        "scores": channel_scores(evidence(plan, state)),
                    }
                )
    return rows


def standardisation(rows: Sequence[Mapping[str, Any]]) -> dict[str, tuple[float, float]]:
    """Mean and floored standard deviation of each channel's raw score over genuine sessions."""
    genuine = [row["scores"] for row in rows if row["label"] == 0]
    if not genuine:
        raise ValueError("standardisation needs genuine sessions")
    result = {}
    for name in CHANNELS:
        values = np.array([scores.get(name, NO_EVIDENCE).score for scores in genuine], dtype=float)
        result[name] = (float(values.mean()), max(float(values.std()), SCALE_FLOOR))
    return result


def template(
    standard: Mapping[str, tuple[float, float]], z_limit: float, intercept: float = 0.0,
    weights: Mapping[str, float] | None = None,
) -> FusionModel:
    return FusionModel.from_dict(
        {
            "intercept": intercept,
            "z_limit": z_limit,
            "channels": {
                name: {
                    "weight": 1.0 if weights is None else weights[name],
                    "mean": standard[name][0],
                    "scale": standard[name][1],
                    "alert_z": ALERT_Z,
                }
                for name in CHANNELS
            },
        }
    )


def design(rows: Sequence[Mapping[str, Any]], model: FusionModel) -> np.ndarray:
    return np.array(
        [
            [model.representation(name, row["scores"].get(name, NO_EVIDENCE)) for name in CHANNELS]
            for row in rows
        ],
        dtype=float,
    )


def fit(x: np.ndarray, y: np.ndarray, l2: float = L2) -> tuple[float, np.ndarray]:
    """Balanced, L2-regularised logistic regression with non-negative weights."""
    positives, negatives = float(y.sum()), float(len(y) - y.sum())
    if positives == 0 or negatives == 0:
        raise ValueError("fitting needs both classes")
    weight = np.where(y == 1, 0.5 / positives, 0.5 / negatives)

    def loss(theta: np.ndarray) -> tuple[float, np.ndarray]:
        logits = theta[0] + x @ theta[1:]
        losses = np.logaddexp(0.0, logits) - y * logits
        probabilities = 1.0 / (1.0 + np.exp(-logits))
        residual = weight * (probabilities - y)
        gradient = np.concatenate([[residual.sum()], x.T @ residual + 2 * l2 * theta[1:]])
        return float((weight * losses).sum() + l2 * np.sum(theta[1:] ** 2)), gradient

    width = x.shape[1]
    result = minimize(
        loss,
        np.zeros(width + 1),
        jac=True,
        method="L-BFGS-B",
        bounds=[(None, None)] + [(0.0, None)] * width,
    )
    if not result.success:
        raise RuntimeError(f"fusion fit did not converge: {result.message}")
    return float(result.x[0]), result.x[1:]


def thresholds_from(risks: np.ndarray) -> Thresholds:
    """Action thresholds at percentiles of genuine risk, kept strictly increasing and below 100."""
    values: dict[str, float] = {}
    floor = 0.01
    for name, percentile in THRESHOLD_PERCENTILES.items():
        value = max(float(np.percentile(risks, percentile)), floor)
        values[name] = value
        floor = value + MIN_THRESHOLD_GAP
    ceiling = 99.9
    for name in reversed(list(THRESHOLD_PERCENTILES)):
        values[name] = min(values[name], ceiling)
        ceiling = values[name] - MIN_THRESHOLD_GAP / 10
    return Thresholds(**{name: round(value, 4) for name, value in values.items()})


def evaluate(
    rows: Sequence[Mapping[str, Any]], model: FusionModel, thresholds: Thresholds, label: str
) -> pd.DataFrame:
    records = []
    for row in rows:
        fused = fuse(model, row["scores"])
        decision = decide(fused, model, thresholds, "confirmation")
        records.append({"attack": row["attack"], "action": decision.action, "risk": fused.risk})
    frame = pd.DataFrame(records)
    summary = []
    for attack in attacks.ATTACK_CLASSES:
        group = frame[frame["attack"] == attack]
        entry: dict[str, Any] = {
            "model": label,
            "attack": attack,
            "sessions": len(group),
            "detection_rate": float(group["action"].isin(attacks.DETECTED_ACTIONS).mean()),
            "mean_risk": float(group["risk"].mean()),
        }
        for action in ACTIONS:
            entry[f"{action}_rate"] = float((group["action"] == action).mean())
        summary.append(entry)
    return pd.DataFrame(summary)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fit the fusion model on synthetic sessions.")
    parser.add_argument("--dry-run", action="store_true", help="do not write models.json")
    args = parser.parse_args(argv)

    data = cmu.load()
    subjects = list(data)[LIVE_VICTIMS:]
    training, evaluation = subjects[:TRAINING_SUBJECTS], subjects[TRAINING_SUBJECTS:]
    print(f"fitting on {len(training)} subjects, evaluating on {len(evaluation)}")

    train_rows = build_sessions(training, data, SESSIONS_PER_CLASS)
    eval_rows = build_sessions(evaluation, data, SESSIONS_PER_CLASS)

    current = load_models()
    placeholder = FusionModel.from_dict(current)
    placeholder_thresholds = Thresholds.from_dict(current["thresholds"])

    standard = standardisation(train_rows)
    x = design(train_rows, template(standard, placeholder.z_limit))
    y = np.array([row["label"] for row in train_rows], dtype=float)
    intercept, fitted = fit(x, y)
    weights = {name: float(value) for name, value in zip(CHANNELS, fitted, strict=True)}
    model = template(standard, placeholder.z_limit, intercept, weights)

    genuine_x = x[y == 0]
    genuine_risk = 100.0 / (1.0 + np.exp(-(intercept + genuine_x @ fitted)))
    thresholds = thresholds_from(genuine_risk)

    fit_table = pd.DataFrame(
        [
            {"channel": name, "weight": weights[name], "mean": standard[name][0],
             "scale": standard[name][1]}
            for name in CHANNELS
        ]
        + [{"channel": "intercept", "weight": intercept, "mean": "", "scale": ""}]
    )
    evaluation_table = pd.concat(
        [
            evaluate(eval_rows, placeholder, placeholder_thresholds, "placeholder"),
            evaluate(eval_rows, model, thresholds, "fitted"),
        ],
        ignore_index=True,
    )

    TABLES.mkdir(parents=True, exist_ok=True)
    fit_table.to_csv(TABLES / "fusion_fit.csv", index=False)
    evaluation_table.to_csv(TABLES / "fusion_evaluation.csv", index=False)
    print(fit_table.to_string(index=False))
    print(f"\nthresholds: {thresholds}")
    print(
        evaluation_table[["model", "attack", "detection_rate", "step_up_rate", "restrict_rate",
                          "block_rate", "mean_risk"]].round(4).to_string(index=False)
    )

    if not args.dry_run:
        current["status"] = (
            "fitted by research/fit_fusion.py on synthetic sessions (Tier 2): "
            f"{len(train_rows)} sessions from {len(training)} CMU subjects; thresholds at genuine "
            "risk percentiles 80/95/99/99.9"
        )
        current["intercept"] = round(intercept, 6)
        for name in CHANNELS:
            current["channels"][name] = {
                "weight": round(weights[name], 6),
                "mean": round(standard[name][0], 6),
                "scale": round(standard[name][1], 6),
                "alert_z": ALERT_Z,
            }
        current["thresholds"] = {
            name: getattr(thresholds, name) for name in THRESHOLD_PERCENTILES
        }
        MODELS_PATH.write_text(json.dumps(current, indent=2) + "\n", encoding="utf-8")
        print(f"\nfitted model written to {MODELS_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
