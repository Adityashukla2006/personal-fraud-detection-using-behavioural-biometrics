"""From risk to action, under constraints the model is deliberately not trusted to learn.

The fusion model is fitted on synthetic labels, so it is given no unbounded authority. Its risk
proposes an action through fixed thresholds, and then three rules can only lower that action,
never raise it (architecture sections 5.2 and 8.3):

1. Friction ceiling. The behavioural channels alone can never escalate past step-up. Restriction
   and blocking need at least one non-behavioural channel above threshold. This caps the
   worst-case cost of behavioural drift at one re-authentication.
2. Corroboration for block. Blocking needs two channels above threshold, or one plus a
   batch-derived flag. Otherwise the most a decision reaches is restriction.
3. Checkpoint. Only the confirmation checkpoint can restrict or block. Earlier checkpoints can at
   most raise a step-up.

Which channels are behavioural
------------------------------
Both ``behaviour`` and ``automation``. Automation is computed from the same keystroke timing, so a
capture glitch or an unusual but genuine typist moves both at once; counting automation as the
non-behavioural corroboration would let the behavioural payload corroborate itself.

A dependency failure never reaches this logic. ``fail_open`` is the decision the scoring path
returns instead, and it is ``monitor``, never ``block`` (CLAUDE.md section 2).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from fraudcore.fusion import MODELS_PATH, Contribution, Fusion, FusionModel, load_models
from fraudcore.scoring import CHANNELS

Action = Literal["allow", "monitor", "step_up", "restrict", "block"]
ACTIONS: tuple[Action, ...] = ("allow", "monitor", "step_up", "restrict", "block")

Checkpoint = Literal["login", "payee", "amount", "confirmation"]
CHECKPOINTS: tuple[Checkpoint, ...] = ("login", "payee", "amount", "confirmation")

BEHAVIOURAL_CHANNELS: frozenset[str] = frozenset({"behaviour", "automation"})

TOP_CONTRIBUTIONS = 3


def _rank(action: Action) -> int:
    return ACTIONS.index(action)


@dataclass(frozen=True)
class Thresholds:
    """Minimum risk, inclusive, at which each action applies. Below ``monitor`` is ``allow``."""

    monitor: float
    step_up: float
    restrict: float
    block: float

    def __post_init__(self) -> None:
        if not 0 < self.monitor < self.step_up < self.restrict < self.block <= 100:
            raise ValueError(
                "thresholds must satisfy 0 < monitor < step_up < restrict < block <= 100"
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Thresholds:
        return cls(**{action: float(data[action]) for action in ACTIONS[1:]})

    @classmethod
    def load(cls, path: Path = MODELS_PATH) -> Thresholds:
        return cls.from_dict(load_models(path)["thresholds"])

    def action_for(self, risk: float) -> Action:
        for action in reversed(ACTIONS[1:]):
            if risk >= getattr(self, action):
                return action
        return "allow"


@dataclass(frozen=True)
class Decision:
    action: Action
    risk: float | None
    confidence: float
    contributions: tuple[Contribution, ...]
    constraints: tuple[str, ...] = ()
    """The policy rules that lowered the model's proposed action, in the order they applied."""


def fail_open() -> Decision:
    """The decision returned when a dependency fails: watch, but never add friction."""
    return Decision(
        action="monitor", risk=None, confidence=0.0, contributions=(), constraints=("fail_open",)
    )


def alerting_channels(fusion: Fusion, model: FusionModel) -> frozenset[str]:
    """Channels whose confidence-shrunk standardised score is at or above their alert threshold."""
    return frozenset(name for name in CHANNELS if fusion.z[name] >= model.channels[name].alert_z)


def decide(
    fusion: Fusion,
    model: FusionModel,
    thresholds: Thresholds,
    checkpoint: Checkpoint,
    batch_flag: bool = False,
) -> Decision:
    """Map a fused risk to an action, then apply the friction ceiling, corroboration and checkpoint.

    ``batch_flag`` is a slow-plane flag on this session's destination or account, such as a payee
    the aggregator has marked.
    """
    if checkpoint not in CHECKPOINTS:
        raise ValueError(f"unknown checkpoint {checkpoint!r}, expected {CHECKPOINTS}")

    action = thresholds.action_for(fusion.risk)
    alerting = alerting_channels(fusion, model)
    applied: list[str] = []

    if _rank(action) > _rank("step_up") and not alerting - BEHAVIOURAL_CHANNELS:
        action = "step_up"
        applied.append("friction_ceiling")

    if action == "block" and not (len(alerting) >= 2 or (alerting and batch_flag)):
        action = "restrict"
        applied.append("corroboration")

    if checkpoint != "confirmation" and _rank(action) > _rank("step_up"):
        action = "step_up"
        applied.append("checkpoint")

    return Decision(
        action=action,
        risk=fusion.risk,
        confidence=fusion.confidence,
        contributions=fusion.top(TOP_CONTRIBUTIONS),
        constraints=tuple(applied),
    )
