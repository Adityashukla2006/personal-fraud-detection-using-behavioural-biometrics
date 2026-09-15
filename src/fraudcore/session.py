"""One checkpoint's evidence, assembled into channel scores, a fused risk and a decision.

Which evidence feeds which channel is itself decision logic, so it is decided once, here, and both
the scoring Lambda and the research track call it.

Evidence accumulates through a session (architecture section 5.2). Keystroke fields from every
checkpoint so far are pooled, so identity confidence rises as the user types more instead of resting
on one short field. Transaction and payee evidence exist only once there is a transfer to judge;
before that, those channels report no evidence rather than scoring an absent payee as a new one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from fraudcore.features import (
    FEATURE_NAMES,
    KeystrokeTiming,
    PayeeEdge,
    PayeeRisk,
    SessionContext,
    Transfer,
    UserAggregates,
    extract,
)
from fraudcore.fusion import Fusion, FusionModel, fuse
from fraudcore.policy import Checkpoint, Decision, Thresholds, decide
from fraudcore.scoring import (
    NO_EVIDENCE,
    ChannelScore,
    ReferenceProfile,
    behaviour_channel,
    context_channel,
    payee_channel,
    session_automation_channel,
    transaction_channel,
)


def pooled_extract(fields: Sequence[KeystrokeTiming]) -> dict[str, float]:
    """The twelve features over several field entries, each weighted by its keystroke count.

    Weighting by keystrokes means a long field says more about a user's rhythm than a three-key
    one. Profiles are built from sessions pooled the same way, so the comparison stays like for
    like.
    """
    if not fields:
        raise ValueError("at least one field is required")
    counts = [len(field.hold) for field in fields]
    extracted = [extract(field) for field in fields]
    total = sum(counts)
    return {
        name: sum(count * values[name] for count, values in zip(counts, extracted, strict=True))
        / total
        for name in FEATURE_NAMES
    }


@dataclass(frozen=True)
class SessionEvidence:
    fields: tuple[KeystrokeTiming, ...]
    profile: ReferenceProfile | None
    profile_sessions: int
    context: SessionContext
    transfer: Transfer | None
    aggregates: UserAggregates | None
    edge: PayeeEdge | None
    risk: PayeeRisk | None
    replay_history: frozenset[int] | None = None


def channel_scores(evidence: SessionEvidence) -> dict[str, ChannelScore]:
    fields = evidence.fields
    has_transfer = evidence.transfer is not None
    return {
        "behaviour": (
            behaviour_channel(
                evidence.profile,
                pooled_extract(fields),
                sum(len(field.hold) for field in fields),
                evidence.profile_sessions,
            )
            if fields
            else NO_EVIDENCE
        ),
        "automation": session_automation_channel(fields, evidence.replay_history),
        "transaction": (
            transaction_channel(evidence.transfer, evidence.aggregates)
            if evidence.transfer is not None
            else NO_EVIDENCE
        ),
        "context": context_channel(evidence.context),
        "payee": payee_channel(evidence.edge, evidence.risk) if has_transfer else NO_EVIDENCE,
    }


@dataclass(frozen=True)
class Scored:
    scores: Mapping[str, ChannelScore]
    fusion: Fusion
    decision: Decision


def score_session(
    evidence: SessionEvidence,
    model: FusionModel,
    thresholds: Thresholds,
    checkpoint: Checkpoint,
) -> Scored:
    scores = channel_scores(evidence)
    fused = fuse(model, scores)
    # A batch flag on a payee only corroborates a decision about a transfer to that payee.
    batch_flag = (
        evidence.transfer is not None and evidence.risk is not None and evidence.risk.flagged
    )
    return Scored(scores, fused, decide(fused, model, thresholds, checkpoint, batch_flag))
