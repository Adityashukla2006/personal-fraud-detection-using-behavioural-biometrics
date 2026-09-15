"""Low-and-slow siphoning (attack class C), fixed before the batch detector was tuned (sec. 15.1).

Siphoning plays out over weeks, and a live run cannot wait weeks, so history is replayed. Every
transfer below is written to the audit lake as the pair of events the live system would have
archived for it, a confirmation decision and a released transfer, dated on the day it happened. The
deployed aggregator is then invoked as of each day in turn, and a live confirmation is scored
through the real endpoint after each run. Nothing about detection is simulated: the aggregator,
the table it writes, the scoring Lambda and the decision are all the deployed ones.

Who does what
-------------
    victim    a CMU subject with 60 days of ordinary history: two transfers a week, spread over
              three established payees, amounts around 1,500, typed by the owner.
    siphon    an attacker operating the victim's own enrolled device (malware or an insider, so the
              device and context channels see nothing new) sends 900 to one mule account every two
              days for 20 days, at the same hour. 900 is below the victim's median: no single
              transfer is unusual. Every victim's siphon goes to the same mule. The attacker's
              typing is another CMU subject's real typing.
    control   a genuine CMU subject with the same 60 days of history, who starts paying a new
              payee, a tutor, on exactly the siphon's schedule and amount, typing it themselves.
              Volume, regularity and novelty all look like siphoning. It is the hard negative, and
              any flag it receives is a false positive.

Identity scores in the replayed history are the behaviour channel's real scores for real CMU typing
against the victim's real profile, computed with fraudcore exactly as scoring computes them.
"""

from __future__ import annotations

import math
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fraudcore.events import (
    DECISION_SCORED,
    SCHEMA_VERSION,
    SCORING_SOURCE,
    TRANSFER_COMPLETED,
    WORKFLOW_SOURCE,
)
from simulator import attacks

SEED = 20260916

HISTORY_DAYS = 63
TRANSFERS_PER_WEEK = 2
KNOWN_PAYEES = 3
GENUINE_AMOUNT = 1500.0
GENUINE_AMOUNT_SPREAD = 0.35
GENUINE_HOURS = (9, 21)

ATTACK_DAYS = 20
SIPHON_EVERY_DAYS = 2
SIPHON_AMOUNT = 900.0
SIPHON_HOUR = 10
MULE_ACCOUNT = "SIPHON-MULE-0001"


@dataclass(frozen=True)
class ScheduledTransfer:
    """``day`` counts from the start of the attack window, fractional part the time of day; history
    is negative."""

    day: float
    account: str
    amount: float


def known_accounts(subject: str) -> list[str]:
    return [attacks.known_payee(subject)] + [
        f"KNOWN-{subject}-{index}" for index in range(1, KNOWN_PAYEES)
    ]


def control_account(subject: str) -> str:
    return f"TUTOR-{subject}"


def genuine_history(subject: str, rng: random.Random) -> list[ScheduledTransfer]:
    """Two transfers a week for nine weeks before the attack window, to established payees."""
    accounts = known_accounts(subject)
    transfers = []
    for week in range(HISTORY_DAYS // 7):
        for _ in range(TRANSFERS_PER_WEEK):
            hour = rng.randint(*GENUINE_HOURS)
            day = -HISTORY_DAYS + week * 7 + rng.randrange(7) + hour / 24
            amount = round(GENUINE_AMOUNT * math.exp(rng.gauss(0.0, GENUINE_AMOUNT_SPREAD)), 2)
            transfers.append(ScheduledTransfer(day, rng.choice(accounts), amount))
    return sorted(transfers, key=lambda transfer: transfer.day)


def _schedule(account: str) -> list[ScheduledTransfer]:
    return [
        ScheduledTransfer(day + SIPHON_HOUR / 24, account, SIPHON_AMOUNT)
        for day in range(0, ATTACK_DAYS, SIPHON_EVERY_DAYS)
    ]


def siphon_schedule() -> list[ScheduledTransfer]:
    return _schedule(MULE_ACCOUNT)


def control_schedule(subject: str) -> list[ScheduledTransfer]:
    return _schedule(control_account(subject))


def lake_events(
    uid: str,
    transfer_id: str,
    payee_id: str,
    amount: float,
    at: datetime,
    identity_score: float,
) -> list[dict[str, Any]]:
    """The two events the live system archives for one released transfer, in EventBridge shape.

    Like the live events, they carry scores and the transaction, never keystroke timing.
    """
    moment = at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    decision = {
        "version": "0",
        "id": uuid.uuid4().hex,
        "detail-type": DECISION_SCORED,
        "source": SCORING_SOURCE,
        "time": moment,
        "detail": {
            "schema": SCHEMA_VERSION,
            "decision_id": transfer_id,
            "session_id": f"sim-{transfer_id}",
            "uid": uid,
            "checkpoint": "confirmation",
            "device_class": "desktop",
            "action": "allow",
            "risk": None,
            "confidence": None,
            "contributions": [],
            "constraints": ["replayed_history"],
            "scores": {"behaviour": {"score": identity_score, "confidence": 1.0}},
            "created_at": moment,
            "transaction": {"payee_id": payee_id, "amount": amount},
        },
    }
    completed = {
        "version": "0",
        "id": uuid.uuid4().hex,
        "detail-type": TRANSFER_COMPLETED,
        "source": WORKFLOW_SOURCE,
        "time": moment,
        "detail": {
            "schema": SCHEMA_VERSION,
            "transfer_id": transfer_id,
            "decision_id": transfer_id,
            "uid": uid,
            "action": "allow",
            "response": "release",
            "status": "released",
            "reason": None,
            "amount": amount,
            "payee_id": payee_id,
        },
    }
    return [decision, completed]
