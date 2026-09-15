"""Poisoning-resistant profile adaptation (architecture section 7).

Naive adaptation fails twice over (7.1). Its acceptance test reads the quantity being updated, so
the system's own judgement is the only guard on that judgement. And its estimator, a running mean,
has a breakdown point of zero: a stream of accepted outliers moves it without bound. Each half of
this module answers one of those failures.

Trust (7.3) authorises an update from evidence outside the behavioural channel: how the session was
verified, how established the device is, whether credentials just changed, and whether the
non-behavioural channels raised the transaction. It is a product because these are gates, not votes;
any single zero vetoes.

Robust recomputation (7.5, 7.6) rebuilds the profile as the trust-weighted geometric median of the
admitted buffer, whose breakdown point is one half, then bounds how far the result may move from the
last verified anchor. Movement faster than genuine drift is not drift, however confident each update
looked.

The scale is bounded too, because inflating it is an evasion a centre-only budget would miss. It
has its own budget: centre displacement is a distance in scaled units, scale displacement is a
relative change, and one number cannot calibrate both. A scale budget of zero freezes the scale at
the anchor's, which the poisoning experiment found to resist impersonation better than adapting it.

Where the anchor comes from. Section 7.8's cost argument says an attacker needs one passkey step-up
of their own per budget's worth of progress. That holds only if the anchor is independent of the
attacker. Re-anchoring to the freshly projected profile (``anchor="projected"``) breaks it: every
step-up the genuine user passes resets the budget from a position the attacker has already pulled,
so the attacker gains a new budget each time the victim verifies. ``anchor="verified"`` re-anchors
instead to the robust centre of fully trusted sessions only, which no attacker without the
authenticator can contribute to.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fraudcore.features import (
    BENCHMARK_FEATURE_NAMES,
    ENROLLED_DEVICE_SESSIONS,
    STABILITY_WINDOW_DAYS,
)
from fraudcore.fusion import MODELS_PATH, FusionModel, load_models
from fraudcore.policy import BEHAVIOURAL_CHANNELS, Action
from fraudcore.scoring import CHANNELS, DISPERSION_FLOOR, ChannelScore

# Profiles are learned over the features the benchmark can measure, so the budgets calibrated from
# CMU drift are a statement about the profiles the live system actually adapts.
PROFILE_FEATURES: tuple[str, ...] = BENCHMARK_FEATURE_NAMES

SECONDS_PER_DAY = 86_400
FULL_TRUST = 1.0

Verification = Literal["stepup", "passkey_signin", "password"]
C_VERIFY: Mapping[str, float] = {"stepup": 1.0, "passkey_signin": 0.6, "password": 0.3}

AnchorSource = Literal["projected", "verified"]
ANCHOR_SOURCES: tuple[str, ...] = ("projected", "verified")

# The detector's dispersion is a mean absolute deviation, but a robust rebuild needs the median
# absolute deviation. Under normality they differ by a constant factor; applying it keeps an adapted
# profile's scores on the same scale as an enrolled one, so thresholds mean the same thing.
MEDIAN_TO_MEAN_ABSOLUTE_DEVIATION = math.sqrt(2 / math.pi) / 0.6744897501960817

# A point this close to the current estimate is treated as sitting on it.
_COINCIDENT = 1e-12


# ---------------------------------------------------------------------------------------------
# Trust
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TrustEvidence:
    """Everything the trust score may use. Deliberately nothing from the behavioural channel.

    ``device_sessions`` is ``None`` for a device never seen before. ``action`` is the decision's
    action; restriction and blocking can only come from non-behavioural channels (the friction
    ceiling), so reading it keeps the independence principle intact.
    """

    verification: Verification
    device_sessions: int | None
    days_since_credential_change: float | None
    days_since_contact_change: float | None
    non_behavioural_alert: bool
    action: Action


def c_verify(verification: str) -> float:
    if verification not in C_VERIFY:
        raise ValueError(f"unknown verification {verification!r}")
    return C_VERIFY[verification]


def c_device(device_sessions: int | None) -> float:
    if device_sessions is None:
        return 0.0
    return 1.0 if device_sessions >= ENROLLED_DEVICE_SESSIONS else 0.5


def c_stability(
    days_since_credential_change: float | None, days_since_contact_change: float | None
) -> float:
    """0.0 if a credential or contact detail changed within the stability window, else 1.0."""
    recent = [
        days
        for days in (days_since_credential_change, days_since_contact_change)
        if days is not None and days < STABILITY_WINDOW_DAYS
    ]
    return 0.0 if recent else 1.0


def c_txn(non_behavioural_alert: bool, action: Action) -> float:
    if action in ("restrict", "block"):
        return 0.0
    return 0.5 if non_behavioural_alert else 1.0


def trust(evidence: TrustEvidence) -> float:
    return (
        c_verify(evidence.verification)
        * c_device(evidence.device_sessions)
        * c_stability(evidence.days_since_credential_change, evidence.days_since_contact_change)
        * c_txn(evidence.non_behavioural_alert, evidence.action)
    )


def non_behavioural_alert(scores: Mapping[str, ChannelScore], model: FusionModel) -> bool:
    """Whether any non-behavioural channel crossed its alert threshold in this session.

    Only transaction, context and payee count. Using the fused risk instead would let the
    behavioural channel vouch for its own update (section 7.3).
    """
    return any(
        model.representation(name, scores[name]) >= model.channels[name].alert_z
        for name in CHANNELS
        if name not in BEHAVIOURAL_CHANNELS and name in scores
    )


# ---------------------------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AdaptationPolicy:
    """Adaptation parameters.

    ``budget`` bounds centre displacement, in scaled units, and must be positive. ``scale_budget``
    bounds the mean relative change of the scale; zero freezes the scale. Both are required, with no
    default, because each is calibrated or chosen from measured drift and a guessed value would
    silently decide the security property. ``anchor`` chooses where a re-anchor moves the anchor to;
    see the module docstring.
    """

    budget: float
    scale_budget: float
    tau_min: float = 0.5
    buffer_capacity: int = 200
    rebuild_every: int = 20
    cold_start_sessions: int = 5
    reanchor_days: float = 30.0
    anchor: AnchorSource = "projected"

    def __post_init__(self) -> None:
        if not (math.isfinite(self.budget) and self.budget > 0):
            raise ValueError("budget must be positive and finite")
        if not (math.isfinite(self.scale_budget) and self.scale_budget >= 0):
            raise ValueError("scale_budget must be finite and non-negative (0 freezes the scale)")
        if not 0 < self.tau_min <= FULL_TRUST:
            raise ValueError("tau_min must be in (0, 1]")
        if not 1 <= self.cold_start_sessions <= self.buffer_capacity:
            raise ValueError("need 1 <= cold_start_sessions <= buffer_capacity")
        if self.rebuild_every < 1 or self.reanchor_days <= 0:
            raise ValueError("rebuild_every and reanchor_days must be positive")
        if self.anchor not in ANCHOR_SOURCES:
            raise ValueError(f"anchor must be one of {ANCHOR_SOURCES}")

    @property
    def frozen_scale(self) -> bool:
        return self.scale_budget == 0

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> AdaptationPolicy:
        return cls(
            budget=float(data["budget"]),
            scale_budget=float(data["scale_budget"]),
            tau_min=float(data["tau_min"]),
            buffer_capacity=int(data["buffer_capacity"]),
            rebuild_every=int(data["rebuild_every"]),
            cold_start_sessions=int(data["cold_start_sessions"]),
            reanchor_days=float(data["reanchor_days"]),
            anchor=str(data["anchor"]),  # type: ignore[arg-type]
        )

    @classmethod
    def load(cls, path: Path = MODELS_PATH) -> AdaptationPolicy:
        return cls.from_dict(load_models(path)["adaptation"])


def admit(tau: float, policy: AdaptationPolicy) -> bool:
    """Admission is a threshold, not a weight: below tau_min a session has no influence at all.

    A small weight times unlimited sessions would still be unlimited influence (section 7.4).
    """
    return tau >= policy.tau_min


@dataclass(frozen=True)
class BufferedSession:
    features: tuple[float, ...]
    weight: float


def bounded(buffer: Sequence[BufferedSession], capacity: int) -> tuple[BufferedSession, ...]:
    """The newest ``capacity`` sessions of a buffer ordered oldest first."""
    if capacity < 1:
        raise ValueError("capacity must be at least 1")
    return tuple(buffer[-capacity:])


# ---------------------------------------------------------------------------------------------
# Robust estimation
# ---------------------------------------------------------------------------------------------


def _check_weighted(points: Sequence[Sequence[float]], weights: Sequence[float]) -> int:
    if not points or len(points) != len(weights):
        raise ValueError("need one weight per point, and at least one point")
    if any(not (math.isfinite(weight) and weight > 0) for weight in weights):
        raise ValueError("weights must be positive and finite")
    width = len(points[0])
    if width == 0 or any(len(point) != width for point in points):
        raise ValueError("every point must have the same non-zero width")
    return width


def weighted_median(values: Sequence[float], weights: Sequence[float]) -> float:
    """The smallest value at which cumulative weight reaches half the total."""
    _check_weighted([[value] for value in values], weights)
    half = sum(weights) / 2
    cumulative = 0.0
    ordered = sorted(zip(values, weights, strict=True))
    for value, weight in ordered:
        cumulative += weight
        if cumulative >= half:
            return value
    return ordered[-1][0]


def _distance(a: Sequence[float], b: Sequence[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b, strict=True)))


def weighted_geometric_median(
    points: Sequence[Sequence[float]],
    weights: Sequence[float],
    tolerance: float = 1e-6,
    max_iterations: int = 100,
) -> tuple[float, ...]:
    """Weighted geometric median by Weiszfeld iteration, with the Vardi-Zhang guard.

    Plain Weiszfeld divides by each point's distance from the estimate, which is undefined when the
    estimate lands on a data point. The guard treats such a point as pulling with its full weight:
    if that weight outweighs the pull of all the others, the point is the median and iteration
    stops; otherwise the step is shortened in proportion. This matters here, because a buffer
    dominated by near-identical sessions is exactly where the estimate lands on a point.
    """
    width = _check_weighted(points, weights)
    total = sum(weights)
    current = [
        sum(weight * point[f] for point, weight in zip(points, weights, strict=True)) / total
        for f in range(width)
    ]

    for _ in range(max_iterations):
        numerator = [0.0] * width
        denominator = 0.0
        coincident = 0.0
        for point, weight in zip(points, weights, strict=True):
            distance = _distance(point, current)
            if distance <= _COINCIDENT:
                coincident += weight
                continue
            pull = weight / distance
            denominator += pull
            for f in range(width):
                numerator[f] += pull * point[f]

        if denominator == 0.0:
            break
        target = [value / denominator for value in numerator]

        if coincident > 0:
            # The others' net pull is denominator * (target - current); compare it to the weight
            # sitting on the estimate.
            resultant = denominator * _distance(target, current)
            if resultant <= coincident:
                break
            step = 1 - coincident / resultant
            target = [c + step * (t - c) for t, c in zip(target, current, strict=True)]

        moved = _distance(target, current)
        current = target
        if moved < tolerance:
            break

    return tuple(current)


def weighted_scale(
    points: Sequence[Sequence[float]], weights: Sequence[float], centre: Sequence[float]
) -> tuple[float, ...]:
    """Per-feature weighted median absolute deviation, on the detector's dispersion scale."""
    width = _check_weighted(points, weights)
    if len(centre) != width:
        raise ValueError("centre width does not match the points")
    return tuple(
        max(
            DISPERSION_FLOOR,
            MEDIAN_TO_MEAN_ABSOLUTE_DEVIATION
            * weighted_median([abs(point[f] - centre[f]) for point in points], weights),
        )
        for f in range(width)
    )


# ---------------------------------------------------------------------------------------------
# Displacement budget and anchoring
# ---------------------------------------------------------------------------------------------


def displacement(
    candidate: Sequence[float], anchor: Sequence[float], anchor_scale: Sequence[float]
) -> float:
    """Mean scaled Manhattan distance: the same metric as scoring, so a unit of budget is a unit
    of anomaly (section 7.6)."""
    return sum(
        abs(c - a) / s for c, a, s in zip(candidate, anchor, anchor_scale, strict=True)
    ) / len(anchor)


def scale_displacement(candidate_scale: Sequence[float], anchor_scale: Sequence[float]) -> float:
    """Mean relative change of the per-feature scale."""
    return sum(
        abs(c - a) / a for c, a in zip(candidate_scale, anchor_scale, strict=True)
    ) / len(anchor_scale)


def project(
    candidate: Sequence[float], anchor: Sequence[float], distance: float, budget: float
) -> tuple[tuple[float, ...], bool]:
    """Move no further than ``budget`` from the anchor along the line to the candidate.

    Returns the accepted point and whether the budget bound it. A candidate exactly at the budget is
    accepted unchanged and does not count as saturation. A budget of zero returns the anchor.
    """
    if distance <= budget:
        return tuple(candidate), False
    ratio = budget / distance
    return tuple(a + ratio * (c - a) for c, a in zip(candidate, anchor, strict=True)), True


@dataclass(frozen=True)
class Profile:
    centre: tuple[float, ...]
    scale: tuple[float, ...]
    anchor_centre: tuple[float, ...]
    anchor_scale: tuple[float, ...]
    anchored_at: float
    last_saturated_at: float | None
    saturations: int
    version: int

    def __post_init__(self) -> None:
        widths = {
            len(self.centre),
            len(self.scale),
            len(self.anchor_centre),
            len(self.anchor_scale),
        }
        if len(widths) != 1 or 0 in widths:
            raise ValueError("profile vectors must share one non-zero width")


@dataclass(frozen=True)
class Rebuild:
    outcome: Literal["cold_start", "bootstrapped", "updated"]
    profile: Profile | None
    candidate_displacement: float
    saturated: bool
    reanchored: bool


def _verified_anchor(
    profile: Profile, sessions: Sequence[BufferedSession], policy: AdaptationPolicy
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    """The anchor a re-anchor moves to under ``anchor="verified"``.

    The robust centre of fully trusted sessions only, itself bounded by the budgets from the old
    anchor, so one verification cannot jump the anchor either. The scale is only re-estimated once
    there are as many verified sessions as a cold start needs; the dispersion of one or two points
    would collapse it to the floor.
    """
    verified = [session for session in sessions if session.weight >= FULL_TRUST]
    if not verified:
        return profile.anchor_centre, profile.anchor_scale

    points = [session.features for session in verified]
    weights = [session.weight for session in verified]
    candidate = weighted_geometric_median(points, weights)
    distance = displacement(candidate, profile.anchor_centre, profile.anchor_scale)
    centre, _ = project(candidate, profile.anchor_centre, distance, policy.budget)

    scale = profile.anchor_scale
    if len(verified) >= policy.cold_start_sessions and not policy.frozen_scale:
        candidate_scale = weighted_scale(points, weights, candidate)
        spread = scale_displacement(candidate_scale, profile.anchor_scale)
        projected, _ = project(candidate_scale, profile.anchor_scale, spread, policy.scale_budget)
        scale = tuple(max(DISPERSION_FLOOR, value) for value in projected)
    return centre, scale


def rebuild(
    profile: Profile | None,
    buffer: Sequence[BufferedSession],
    tau: float,
    now: float,
    policy: AdaptationPolicy,
) -> Rebuild:
    """Recompute a profile from its buffer under the centre and scale budgets.

    ``tau`` is the trust of the session that triggered the rebuild. A fully trusted session, which
    only a passed step-up can produce, re-anchors: the budgets are measured from the new anchor
    from then on. So does a quiet period of ``reanchor_days`` without saturation. Nothing else does.
    """
    sessions = bounded(buffer, policy.buffer_capacity)

    if profile is None and len(sessions) < policy.cold_start_sessions:
        return Rebuild("cold_start", None, 0.0, False, False)
    if not sessions:
        return Rebuild("updated", profile, 0.0, False, False)

    points = [session.features for session in sessions]
    weights = [session.weight for session in sessions]
    centre = weighted_geometric_median(points, weights)
    scale = weighted_scale(points, weights, centre)

    if profile is None:
        bootstrapped = Profile(centre, scale, centre, scale, now, None, 0, 1)
        return Rebuild("bootstrapped", bootstrapped, 0.0, False, True)

    if len(centre) != len(profile.centre):
        raise ValueError("buffer width does not match the profile")

    distance = displacement(centre, profile.anchor_centre, profile.anchor_scale)
    new_centre, centre_bound = project(centre, profile.anchor_centre, distance, policy.budget)
    if policy.frozen_scale:
        # A frozen scale is a design choice, not a bound hit, so it never counts as saturation.
        new_scale, scale_bound = profile.anchor_scale, False
    else:
        spread = scale_displacement(scale, profile.anchor_scale)
        new_scale, scale_bound = project(scale, profile.anchor_scale, spread, policy.scale_budget)
        new_scale = tuple(max(DISPERSION_FLOOR, value) for value in new_scale)
    saturated = centre_bound or scale_bound

    quiet_since = (
        profile.anchored_at
        if profile.last_saturated_at is None
        else max(profile.anchored_at, profile.last_saturated_at)
    )
    reanchor = tau >= FULL_TRUST or (
        not saturated and now - quiet_since >= policy.reanchor_days * SECONDS_PER_DAY
    )

    if not reanchor:
        anchor_centre, anchor_scale = profile.anchor_centre, profile.anchor_scale
    elif policy.anchor == "verified":
        anchor_centre, anchor_scale = _verified_anchor(profile, sessions, policy)
    else:
        anchor_centre, anchor_scale = new_centre, new_scale

    updated = Profile(
        centre=new_centre,
        scale=new_scale,
        anchor_centre=anchor_centre,
        anchor_scale=anchor_scale,
        anchored_at=now if reanchor else profile.anchored_at,
        last_saturated_at=now if saturated else profile.last_saturated_at,
        saturations=profile.saturations + int(saturated),
        version=profile.version + 1,
    )
    return Rebuild("updated", updated, distance, saturated, reanchor)
