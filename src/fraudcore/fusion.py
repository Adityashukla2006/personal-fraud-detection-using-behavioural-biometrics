"""Logistic fusion of the channel scores, with exact attribution and a separate confidence.

Architecture section 8.1, implemented as written:

    z_i  = c_i * standardise(s_i)      # a channel with no evidence has c_i = 0, so z_i = 0
    L    = w_0 + sum_i w_i * z_i
    risk = 100 * sigmoid(L)

Why a linear model
------------------
Everything beyond the identity channel is trained on synthetic labels, and a model with the
capacity to fit nonlinear structure would learn the simulator rather than fraud (section 8.2). The
linear form buys two things besides. Attribution is exact: each ``w_i * z_i`` is that channel's
share of the logit, and they sum with the intercept to the logit itself, so the top-three
explanation needs no approximation and no library. And the deployed model is five coefficients and
an intercept in ``models.json``, which the Lambda reads as data.

What this module does not do
----------------------------
It does not choose an action. The mapping from risk to action, and the constraints the model is
deliberately not trusted to learn, live in ``policy.py``.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from fraudcore.scoring import CHANNELS, NO_EVIDENCE, ChannelScore

MODELS_PATH = Path(__file__).with_name("models.json")


def load_models(path: Path = MODELS_PATH) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


@dataclass(frozen=True)
class ChannelModel:
    """Fitted parameters for one channel.

    ``mean`` and ``scale`` standardise the raw score so the weights are comparable across channels.
    ``alert_z`` is the confidence-shrunk standardised score at which the channel counts as above
    threshold for the policy rules.
    """

    weight: float
    mean: float
    scale: float
    alert_z: float

    def __post_init__(self) -> None:
        if not all(math.isfinite(v) for v in (self.weight, self.mean, self.scale, self.alert_z)):
            raise ValueError("channel model parameters must be finite")
        # A negative weight would make a riskier score lower the risk, and would break the weighted
        # confidence summary, which is only meaningful for non-negative weights.
        if self.weight < 0:
            raise ValueError("channel weight cannot be negative")
        if self.scale <= 0:
            raise ValueError("channel scale must be positive")

    def standardise(self, score: float) -> float:
        return (score - self.mean) / self.scale


@dataclass(frozen=True)
class FusionModel:
    intercept: float
    channels: Mapping[str, ChannelModel]

    def __post_init__(self) -> None:
        if set(self.channels) != set(CHANNELS):
            raise ValueError(f"fusion model must define exactly the channels {CHANNELS}")
        object.__setattr__(self, "channels", MappingProxyType(dict(self.channels)))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FusionModel:
        return cls(
            intercept=float(data["intercept"]),
            channels={
                name: ChannelModel(
                    weight=float(spec["weight"]),
                    mean=float(spec["mean"]),
                    scale=float(spec["scale"]),
                    alert_z=float(spec["alert_z"]),
                )
                for name, spec in data["channels"].items()
            },
        )

    @classmethod
    def load(cls, path: Path = MODELS_PATH) -> FusionModel:
        return cls.from_dict(load_models(path))


@dataclass(frozen=True)
class Contribution:
    """One channel's exact share of the logit, ``w_i * z_i``. Positive raises risk."""

    channel: str
    value: float


@dataclass(frozen=True)
class Fusion:
    risk: float
    logit: float
    confidence: float
    z: Mapping[str, float]
    contributions: tuple[Contribution, ...]
    """Every channel, largest absolute contribution first."""

    def top(self, count: int = 3) -> tuple[Contribution, ...]:
        return self.contributions[:count]


def _sigmoid(value: float) -> float:
    # Split by sign so neither branch can overflow math.exp for an extreme logit.
    if value >= 0:
        return 1.0 / (1.0 + math.exp(-value))
    exponential = math.exp(value)
    return exponential / (1.0 + exponential)


def fuse(model: FusionModel, scores: Mapping[str, ChannelScore]) -> Fusion:
    """Fuse channel scores into a risk in [0, 100], with attribution and confidence.

    A channel absent from ``scores`` is treated as having no evidence. A channel the model does not
    know is an error, because silently ignoring it would hide a wiring mistake.
    """
    unknown = sorted(set(scores) - set(CHANNELS))
    if unknown:
        raise ValueError(f"unknown channels {unknown}, expected {CHANNELS}")

    z: dict[str, float] = {}
    contributions: list[Contribution] = []
    for name in CHANNELS:
        channel = model.channels[name]
        score = scores.get(name, NO_EVIDENCE)
        z[name] = score.confidence * channel.standardise(score.score)
        contributions.append(Contribution(name, channel.weight * z[name]))

    logit = model.intercept + sum(contribution.value for contribution in contributions)

    total_weight = sum(model.channels[name].weight for name in CHANNELS)
    weighted_confidence = sum(
        model.channels[name].weight * scores.get(name, NO_EVIDENCE).confidence for name in CHANNELS
    )

    return Fusion(
        risk=100.0 * _sigmoid(logit),
        logit=logit,
        confidence=weighted_confidence / total_weight if total_weight > 0 else 0.0,
        z=MappingProxyType(z),
        # sorted is stable, so ties keep the declared channel order and the explanation is
        # deterministic.
        contributions=tuple(sorted(contributions, key=lambda c: abs(c.value), reverse=True)),
    )
