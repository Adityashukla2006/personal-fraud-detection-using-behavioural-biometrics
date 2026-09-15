"""Attack classes for the live simulator, fixed before any run (architecture section 15.1).

Each session is planned in full, then sent through the deployed endpoint checkpoint by checkpoint
exactly as the browser would: login, payee, amount, confirmation, one captured field each. The plan
is pure data, so what every class does can be read, tested and frozen here, apart from the code
that sends it.

    genuine    the user's own typing from later recording days, on their enrolled device, to a
               known payee, for an ordinary amount. Measures genuine friction.
    takeover   another subject's real typing, on a device never seen, to a new payee, for a large
               amount. Account takeover (class A).
    bot        a perfectly regular scripted rhythm on the victim's own device and payee, so only
               the automation channel can catch it (class B).
    replay     an exact copy of the victim's own enrolment typing, on their device and payee. The
               behavioural channel sees a perfect match, so only replay detection could catch it
               (class B). The protocol nonce (section 13) is not built, so nothing masks the result.
    poisoning  attacker typing blended halfway toward the victim's, on the victim's device and
               payee: sessions engineered to look acceptable, whose goal is to be learned (class D).

Detection is a confirmation decision of step-up or stronger.
"""

from __future__ import annotations

import hashlib
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from simulator.cmu import DECIMALS, Repetition, field

ATTACK_CLASSES = ("genuine", "takeover", "bot", "replay", "poisoning")
DETECTED_ACTIONS = frozenset({"step_up", "restrict", "block"})
CHECKPOINTS = ("login", "payee", "amount", "confirmation")

# Recording days: enrolment builds the profile and is what a replay copies; later days are the
# genuine user typing on a different day.
ENROLMENT_SESSION = 1
LATER_SESSIONS = (3, 4, 5, 6, 7, 8)

ORDINARY_AMOUNT = 1500.0
TAKEOVER_AMOUNT = 25000.0
MIMICRY_WEIGHT = 0.5
BOT_HOLD_SECONDS = 0.08
BOT_INTERVAL_SECONDS = 0.15


@dataclass(frozen=True)
class Checkpoint:
    name: str
    field: dict[str, Any]
    transaction: dict[str, Any] | None


@dataclass(frozen=True)
class SessionPlan:
    attack: str
    victim: str
    device_id: str
    checkpoints: tuple[Checkpoint, ...]


def payee_id(account: str) -> str:
    """What the browser sends: SHA-256 of the account number."""
    return hashlib.sha256(account.encode("utf-8")).hexdigest()


def known_payee(victim: str) -> str:
    return f"KNOWN-{victim}"


def bot_field(keys: int = 11) -> dict[str, Any]:
    """A scripted entry: identical holds and identical intervals."""
    hold = [BOT_HOLD_SECONDS] * keys
    down_down = [BOT_INTERVAL_SECONDS] * (keys - 1)
    return {
        "hold": hold,
        "down_down": down_down,
        "up_down": [round(BOT_INTERVAL_SECONDS - BOT_HOLD_SECONDS, DECIMALS)] * (keys - 1),
        "backspaces": 0,
        "corrections": 0,
        "pastes": 0,
    }


def mimic_field(
    victim: Repetition, attacker: Repetition, weight: float = MIMICRY_WEIGHT
) -> dict[str, Any]:
    """The attacker's timing moved ``weight`` of the way toward the victim's, key by key.

    Up-down is recomputed from the blended hold and down-down, so the entry stays one physically
    consistent keystroke sequence rather than three independently blended series.
    """
    if not 0.0 <= weight <= 1.0:
        raise ValueError("weight must be in [0, 1]")
    hold = [a + weight * (v - a) for v, a in zip(victim.hold, attacker.hold, strict=True)]
    down_down = [
        a + weight * (v - a) for v, a in zip(victim.down_down, attacker.down_down, strict=True)
    ]
    return {
        "hold": [round(value, DECIMALS) for value in hold],
        "down_down": [round(value, DECIMALS) for value in down_down],
        "up_down": [round(dd - h, DECIMALS) for dd, h in zip(down_down, hold, strict=False)],
        "backspaces": 0,
        "corrections": 0,
        "pastes": 0,
    }


def _checkpoints(
    fields: Sequence[dict[str, Any]], account: str, amount: float
) -> tuple[Checkpoint, ...]:
    transaction = {"payee_id": payee_id(account), "amount": amount}
    return tuple(
        Checkpoint(name, entry, transaction if name in ("amount", "confirmation") else None)
        for name, entry in zip(CHECKPOINTS, fields, strict=True)
    )


def plan_session(
    attack: str,
    victim: str,
    victim_reps: Sequence[Repetition],
    attacker_reps: Sequence[Repetition],
    index: int,
    genuine_device: str,
    rng: random.Random,
) -> SessionPlan:
    """The ``index``-th session of one attack class against one victim."""
    if attack not in ATTACK_CLASSES:
        raise ValueError(f"unknown attack class {attack!r}")

    later = [r for r in victim_reps if r.session in LATER_SESSIONS]
    enrolment = [r for r in victim_reps if r.session == ENROLMENT_SESSION]
    attacker_later = [r for r in attacker_reps if r.session in LATER_SESSIONS]
    offset = index * len(CHECKPOINTS)

    def take(reps: Sequence[Repetition]) -> list[Repetition]:
        if len(reps) < offset + len(CHECKPOINTS):
            raise ValueError("not enough repetitions for this session index")
        return list(reps[offset : offset + len(CHECKPOINTS)])

    account, amount, device = known_payee(victim), ORDINARY_AMOUNT, genuine_device
    if attack == "genuine":
        fields = [field(r) for r in take(later)]
    elif attack == "takeover":
        fields = [field(r) for r in take(attacker_later)]
        account = f"MULE-{rng.randrange(10**9):09d}"
        amount = TAKEOVER_AMOUNT
        device = f"sim-new-{rng.randrange(16**12):012x}"
    elif attack == "bot":
        fields = [bot_field() for _ in CHECKPOINTS]
    elif attack == "replay":
        fields = [field(r) for r in take(enrolment)]
    else:
        fields = [
            mimic_field(v, a) for v, a in zip(take(later), take(attacker_later), strict=True)
        ]

    return SessionPlan(attack, victim, device, _checkpoints(fields, account, amount))


def request_body(plan: SessionPlan, checkpoint: Checkpoint, session_id: str) -> dict[str, Any]:
    body: dict[str, Any] = {
        "session_id": session_id,
        "checkpoint": checkpoint.name,
        "device": {
            "device_id": plan.device_id,
            "max_touch_points": 0,
            "coarse_pointer": False,
            "short_side_px": 1080,
        },
        "behaviour": {"fields": [checkpoint.field]},
    }
    if checkpoint.transaction is not None:
        body["transaction"] = checkpoint.transaction
    return body


def detection_rate(actions: Sequence[str]) -> float:
    return sum(action in DETECTED_ACTIONS for action in actions) / len(actions) if actions else 0.0
