"""Phase 2 exit: a fixture set run end to end through channels, fusion and policy.

Each fixture is a whole session -- typing, device, account stability, transfer and payee -- scored
by the real channel scorers, fused and decided. Every decision must carry a score, the top three
contributions, a confidence and an action, and the action must be the one the design demands.

The fusion model here is a fixture, not ``models.json``. The shipped coefficients are placeholders
that research will refit, and a refit must not break a test about the shape of the pipeline.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import pytest

from fraudcore import features, scoring
from fraudcore.features import (
    BENCHMARK_FEATURE_NAMES,
    DeviceRecord,
    KeystrokeTiming,
    PayeeEdge,
    PayeeRisk,
    SessionContext,
    Transfer,
    UserAggregates,
)
from fraudcore.fusion import FusionModel, fuse
from fraudcore.policy import Checkpoint, Decision, Thresholds, decide
from fraudcore.scoring import ReferenceProfile

MODEL = FusionModel.from_dict(
    {
        "intercept": -3.0,
        "z_limit": 6.0,
        "channels": {
            "behaviour": {"weight": 1.0, "mean": 12.0, "scale": 6.0, "alert_z": 2.0},
            "automation": {"weight": 1.5, "mean": 0.1, "scale": 0.2, "alert_z": 2.0},
            "transaction": {"weight": 0.8, "mean": 0.5, "scale": 1.0, "alert_z": 2.0},
            "context": {"weight": 1.0, "mean": 0.2, "scale": 0.5, "alert_z": 2.0},
            "payee": {"weight": 0.9, "mean": 0.3, "scale": 0.6, "alert_z": 2.0},
        },
    }
)
THRESHOLDS = Thresholds(monitor=20.0, step_up=40.0, restrict=65.0, block=85.0)

BASE_DOWN_DOWN = (0.21, 0.35, 0.18, 0.42, 0.27, 0.31, 0.24, 0.39, 0.22, 0.33, 0.29)
BASE_HOLD = (0.09, 0.11, 0.08, 0.10, 0.12, 0.09, 0.10, 0.11, 0.08, 0.10, 0.09, 0.11)


def _typing(tempo: float, wobble: float, hold_scale: float = 1.0) -> KeystrokeTiming:
    """A deterministic twelve-key entry: a fixed rhythm, stretched by tempo and perturbed."""
    down_down = tuple(
        value * tempo * (1 + wobble * ((i * 7 % 5) - 2) / 10)
        for i, value in enumerate(BASE_DOWN_DOWN)
    )
    hold = tuple(
        value * hold_scale * (1 + wobble * ((i * 3 % 5) - 2) / 10)
        for i, value in enumerate(BASE_HOLD)
    )
    return KeystrokeTiming(
        hold=hold,
        down_down=down_down,
        up_down=tuple(dd - h for dd, h in zip(down_down, hold, strict=False)),
    )


ENROLMENT = [
    _typing(tempo, wobble)
    for tempo, wobble in [(0.95, 0.1), (1.0, -0.1), (1.05, 0.2), (0.98, 0.0), (1.02, -0.2)]
]
PROFILE = ReferenceProfile.fit(
    [features.vector(features.extract(t), BENCHMARK_FEATURE_NAMES) for t in ENROLMENT],
    BENCHMARK_FEATURE_NAMES,
)
PROFILE_SESSIONS = 20

GENUINE_TYPING = _typing(1.01, 0.15)
IMPOSTOR_TYPING = _typing(1.7, 0.3, hold_scale=1.5)
BOT_TYPING = KeystrokeTiming(hold=(0.08,) * 12, down_down=(0.15,) * 11, up_down=(0.07,) * 11)

AGGREGATES = UserAggregates(
    amount_p50=400.0,
    amount_p95=1400.0,
    daily_count_p95=3.0,
    hour_histogram=tuple(9.0 if 9 <= hour <= 20 else 1.0 for hour in range(24)),
    history_count=60,
)
HOME = SessionContext(DeviceRecord(30), None, None, account_sessions=40)
KNOWN_PAYEE = PayeeEdge(days_since_first_seen=200.0, verified=True)
CLEAN_RISK = PayeeRisk(risk_score=0.05, hours_since_computed=12.0)


@dataclass(frozen=True)
class Session:
    typing: KeystrokeTiming
    profile: ReferenceProfile | None
    context: SessionContext
    transfer: Transfer
    aggregates: UserAggregates | None
    edge: PayeeEdge | None
    risk: PayeeRisk | None


GENUINE = Session(
    GENUINE_TYPING, PROFILE, HOME, Transfer(500.0, 14, 1), AGGREGATES, KNOWN_PAYEE, CLEAN_RISK
)


def _run(session: Session, checkpoint: Checkpoint) -> Decision:
    scores = {
        "behaviour": scoring.behaviour_channel(
            session.profile,
            features.extract(session.typing),
            len(session.typing.hold) * 4,
            PROFILE_SESSIONS,
        ),
        "automation": scoring.automation_channel(session.typing, frozenset()),
        "transaction": scoring.transaction_channel(session.transfer, session.aggregates),
        "context": scoring.context_channel(session.context),
        "payee": scoring.payee_channel(session.edge, session.risk),
    }
    batch_flag = session.risk is not None and session.risk.flagged
    return decide(fuse(MODEL, scores), MODEL, THRESHOLDS, checkpoint, batch_flag)


def _replace(**changes: object) -> Callable[[], Session]:
    from dataclasses import replace

    return lambda: replace(GENUINE, **changes)


TAKEOVER = _replace(
    typing=IMPOSTOR_TYPING,
    context=SessionContext(None, 1.0, None, account_sessions=40),
    transfer=Transfer(45000.0, 3, 1),
    edge=None,
    risk=PayeeRisk(risk_score=0.7, hours_since_computed=6.0),
)

FIXTURES = [
    # name, session, checkpoint, expected action, constraints that must have applied
    ("genuine user, home device, known payee", _replace(), "confirmation", "allow", ()),
    ("takeover at confirmation", TAKEOVER, "confirmation", "block", ()),
    ("takeover before confirmation", TAKEOVER, "payee", "step_up", ("checkpoint",)),
    (
        "behavioural drift only",
        _replace(typing=IMPOSTOR_TYPING),
        "confirmation",
        "step_up",
        ("friction_ceiling",),
    ),
    ("scripted typing only", _replace(typing=BOT_TYPING), "confirmation", "step_up", None),
    (
        "new payee flagged by batch, otherwise genuine",
        _replace(
            edge=None,
            transfer=Transfer(20000.0, 14, 1),
            risk=PayeeRisk(risk_score=0.9, hours_since_computed=6.0, flagged=True),
        ),
        "confirmation",
        "block",
        (),
    ),
    (
        "brand-new account on a new device",
        _replace(
            profile=None,
            aggregates=None,
            context=SessionContext(None, None, None, account_sessions=0),
            edge=None,
            risk=None,
        ),
        "confirmation",
        "allow",
        None,
    ),
]


@pytest.mark.parametrize(
    ("build", "checkpoint", "expected", "constraints"),
    [fixture[1:] for fixture in FIXTURES],
    ids=[fixture[0] for fixture in FIXTURES],
)
def test_fixture_decision(
    build: Callable[[], Session],
    checkpoint: Checkpoint,
    expected: str | None,
    constraints: tuple[str, ...] | None,
) -> None:
    decision = _run(build(), checkpoint)

    assert decision.risk is not None and 0.0 <= decision.risk <= 100.0
    assert 0.0 <= decision.confidence <= 1.0
    assert len(decision.contributions) == 3
    if expected is not None:
        assert decision.action == expected
    if constraints is not None:
        assert decision.constraints == constraints
    # Bounded representation: no single contribution exceeds weight * z_limit.
    for contribution in decision.contributions:
        limit = MODEL.channels[contribution.channel].weight * MODEL.z_limit
        assert abs(contribution.value) <= limit + 1e-9


def test_a_new_account_is_never_restricted_or_blocked() -> None:
    """Cold start produces extra verification at most, never a block (architecture section 7.7)."""
    session = FIXTURES[-1][1]()
    assert _run(session, "confirmation").action in {"allow", "monitor", "step_up"}


def test_scripted_typing_is_caught_by_the_automation_channel() -> None:
    # A bot's rhythm is also far from the victim's profile, so behaviour may lead; the point is that
    # automation independently alerts and is named in the explanation.
    decision = _run(_replace(typing=BOT_TYPING)(), "confirmation")
    automation = [c for c in decision.contributions if c.channel == "automation"]
    assert automation and automation[0].value > 0
