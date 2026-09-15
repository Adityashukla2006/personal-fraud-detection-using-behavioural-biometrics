"""Tests for the frozen attack classes: each must mean what its docstring says, and every payload
must pass the deployed endpoint's own validation."""

from __future__ import annotations

import json
import random
import statistics
from pathlib import Path

import pytest

from scoring.request import INTERVAL_TOLERANCE_SECONDS, parse
from simulator import attacks, cmu
from simulator.cmu import Repetition


def _rep(subject: str, session: int, rep: int, scale: float) -> Repetition:
    hold = tuple(round(0.08 + 0.01 * ((k * 3 + rep) % 5) * scale, 4) for k in range(11))
    down_down = tuple(round(0.2 + 0.05 * ((k * 7 + rep) % 4) * scale, 4) for k in range(10))
    up_down = tuple(round(d - h, 4) for d, h in zip(down_down, hold, strict=False))
    return Repetition(subject, session, rep, hold, down_down, up_down)


def _reps(subject: str, scale: float) -> list[Repetition]:
    return [_rep(subject, s, r, scale) for s in range(1, 9) for r in range(1, 51)]


VICTIM = _reps("s002", 1.0)
ATTACKER = _reps("s003", 1.6)


def _plan(attack: str, index: int = 0) -> attacks.SessionPlan:
    return attacks.plan_session(
        attack, "s002", VICTIM, ATTACKER, index, "device-genuine", random.Random(1)
    )


@pytest.mark.parametrize("attack", attacks.ATTACK_CLASSES)
def test_every_class_sends_four_valid_checkpoints(attack: str) -> None:
    plan = _plan(attack)
    assert [c.name for c in plan.checkpoints] == list(attacks.CHECKPOINTS)
    for checkpoint in plan.checkpoints:
        # The deployed parser is the arbiter: a payload it rejects would measure nothing.
        parse(json.dumps(attacks.request_body(plan, checkpoint, "session-0001")))
    assert plan.checkpoints[-1].transaction is not None
    assert plan.checkpoints[0].transaction is None


def test_genuine_is_the_victim_on_their_device_to_a_known_payee() -> None:
    plan = _plan("genuine")
    assert plan.device_id == "device-genuine"
    assert plan.checkpoints[-1].transaction == {
        "payee_id": attacks.payee_id(attacks.known_payee("s002")),
        "amount": attacks.ORDINARY_AMOUNT,
    }


def test_takeover_is_another_typist_on_a_new_device_to_a_new_payee() -> None:
    plan = _plan("takeover")
    assert plan.device_id != "device-genuine"
    transaction = plan.checkpoints[-1].transaction
    assert transaction is not None
    assert transaction["payee_id"] != attacks.payee_id(attacks.known_payee("s002"))
    assert transaction["amount"] == attacks.TAKEOVER_AMOUNT
    later = [r for r in ATTACKER if r.session in attacks.LATER_SESSIONS]
    assert plan.checkpoints[0].field == cmu.field(later[0])


def test_the_bot_rhythm_is_perfectly_regular() -> None:
    entry = _plan("bot").checkpoints[0].field
    assert statistics.pstdev(entry["down_down"]) == 0.0
    assert statistics.pstdev(entry["hold"]) == 0.0


def test_replay_is_an_exact_copy_of_enrolment_typing() -> None:
    enrolment = [r for r in VICTIM if r.session == attacks.ENROLMENT_SESSION]
    fields = [c.field for c in _plan("replay").checkpoints]
    assert fields == [cmu.field(r) for r in enrolment[:4]]


def test_mimicry_blends_halfway_and_stays_physically_consistent() -> None:
    victim, attacker = VICTIM[200], ATTACKER[200]
    entry = attacks.mimic_field(victim, attacker, weight=0.5)
    assert entry["hold"][0] == pytest.approx((victim.hold[0] + attacker.hold[0]) / 2, abs=1e-4)
    for dd, h, ud in zip(entry["down_down"], entry["hold"], entry["up_down"], strict=False):
        assert abs(ud - (dd - h)) <= INTERVAL_TOLERANCE_SECONDS


def test_consecutive_sessions_use_fresh_repetitions() -> None:
    first, second = _plan("genuine", 0), _plan("genuine", 1)
    assert first.checkpoints[0].field != second.checkpoints[0].field


def test_an_unknown_class_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown attack class"):
        _plan("phishing")


def test_detection_counts_step_up_and_stronger() -> None:
    assert attacks.detection_rate(["allow", "monitor", "step_up", "restrict", "block"]) == 0.6
    assert attacks.detection_rate([]) == 0.0


def test_the_cmu_reader_produces_payload_fields(tmp_path: Path) -> None:
    header = [
        "subject", "sessionIndex", "rep",
        "H.a", "DD.a.b", "UD.a.b", "H.b", "DD.b.c", "UD.b.c", "H.c",
    ]
    row = ["s002", "1", "1", "0.1", "0.3", "0.2", "0.12", "0.25", "0.13", "0.09"]
    path = tmp_path / "cmu.csv"
    path.write_text(",".join(header) + "\n" + ",".join(row) + "\n", encoding="utf-8")
    [rep] = cmu.load(path)["s002"]
    assert rep.hold == (0.1, 0.12, 0.09)
    assert cmu.field(rep)["up_down"] == [0.2, 0.13]
