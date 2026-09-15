"""Tests for action mapping and the policy rules outside the model.

The friction ceiling, the corroboration rule and fail-open are CLAUDE.md section 2 rules, so each
has a test here that fails if the rule is deleted. Fusion results are built directly rather than
fitted, so every case pins exactly which channels are above threshold.
"""

from __future__ import annotations

import pytest

from fraudcore.fusion import Contribution, Fusion, FusionModel
from fraudcore.policy import Checkpoint, Thresholds, alerting_channels, decide, fail_open
from fraudcore.scoring import CHANNELS

MODEL = FusionModel.from_dict(
    {
        "intercept": 0.0,
        "channels": {
            name: {"weight": 1.0, "mean": 0.0, "scale": 1.0, "alert_z": 2.0} for name in CHANNELS
        },
    }
)
THRESHOLDS = Thresholds(monitor=20.0, step_up=40.0, restrict=65.0, block=85.0)
ALERT = 2.0


def _fusion(risk: float, **z: float) -> Fusion:
    values = {name: z.get(name, 0.0) for name in CHANNELS}
    contributions = tuple(
        sorted(
            (Contribution(name, value) for name, value in values.items()),
            key=lambda c: abs(c.value),
            reverse=True,
        )
    )
    return Fusion(risk=risk, logit=0.0, confidence=0.8, z=values, contributions=contributions)


def _decide(
    fusion: Fusion, checkpoint: Checkpoint = "confirmation", batch_flag: bool = False
) -> tuple[str, tuple[str, ...]]:
    decision = decide(fusion, MODEL, THRESHOLDS, checkpoint, batch_flag)
    return decision.action, decision.constraints


class TestThresholds:
    @pytest.mark.parametrize(
        ("risk", "expected"),
        [
            (0.0, "allow"),
            (19.999, "allow"),
            (20.0, "monitor"),
            (39.999, "monitor"),
            (40.0, "step_up"),
            (65.0, "restrict"),
            (84.999, "restrict"),
            (85.0, "block"),
            (100.0, "block"),
        ],
    )
    def test_each_threshold_is_inclusive(self, risk: float, expected: str) -> None:
        assert THRESHOLDS.action_for(risk) == expected

    def test_out_of_order_thresholds_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="must satisfy"):
            Thresholds(monitor=20.0, step_up=70.0, restrict=65.0, block=85.0)

    def test_the_shipped_thresholds_load(self) -> None:
        Thresholds.load()


class TestAlertingChannels:
    def test_a_channel_exactly_at_its_alert_threshold_is_alerting(self) -> None:
        assert alerting_channels(_fusion(50.0, payee=ALERT), MODEL) == {"payee"}

    def test_a_channel_just_below_is_not(self) -> None:
        assert alerting_channels(_fusion(50.0, payee=ALERT - 1e-9), MODEL) == frozenset()


class TestFrictionCeiling:
    def test_behaviour_alone_cannot_exceed_step_up(self) -> None:
        assert _decide(_fusion(99.0, behaviour=9.0)) == ("step_up", ("friction_ceiling",))

    def test_behaviour_and_automation_together_are_still_behavioural(self) -> None:
        fused = _fusion(99.0, behaviour=9.0, automation=9.0)
        assert _decide(fused) == ("step_up", ("friction_ceiling",))

    def test_no_alerting_channel_at_all_is_also_capped(self) -> None:
        assert _decide(_fusion(99.0)) == ("step_up", ("friction_ceiling",))

    def test_a_non_behavioural_channel_lifts_the_ceiling(self) -> None:
        assert _decide(_fusion(70.0, behaviour=9.0, context=ALERT)) == ("restrict", ())

    def test_the_ceiling_does_not_touch_step_up_or_below(self) -> None:
        assert _decide(_fusion(50.0, behaviour=9.0)) == ("step_up", ())


class TestCorroboration:
    def test_one_non_behavioural_channel_alone_restricts_rather_than_blocks(self) -> None:
        assert _decide(_fusion(99.0, payee=ALERT)) == ("restrict", ("corroboration",))

    def test_two_alerting_channels_block(self) -> None:
        assert _decide(_fusion(99.0, payee=ALERT, transaction=ALERT)) == ("block", ())

    def test_a_behavioural_second_channel_corroborates(self) -> None:
        assert _decide(_fusion(99.0, payee=ALERT, behaviour=ALERT)) == ("block", ())

    def test_one_channel_plus_a_batch_flag_blocks(self) -> None:
        assert _decide(_fusion(99.0, payee=ALERT), batch_flag=True) == ("block", ())

    def test_a_batch_flag_without_any_alerting_channel_does_not_block(self) -> None:
        assert _decide(_fusion(99.0), batch_flag=True) == ("step_up", ("friction_ceiling",))


class TestCheckpoint:
    @pytest.mark.parametrize("checkpoint", ["login", "payee", "amount"])
    def test_only_confirmation_can_restrict_or_block(self, checkpoint: Checkpoint) -> None:
        fused = _fusion(99.0, payee=ALERT, transaction=ALERT)
        assert _decide(fused, checkpoint) == ("step_up", ("checkpoint",))

    def test_an_early_checkpoint_can_still_step_up(self) -> None:
        assert _decide(_fusion(50.0), "login") == ("step_up", ())

    def test_an_unknown_checkpoint_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown checkpoint"):
            _decide(_fusion(50.0), "checkout")  # type: ignore[arg-type]


class TestDecision:
    def test_the_decision_carries_risk_confidence_and_top_three(self) -> None:
        decision = decide(
            _fusion(99.0, payee=4.0, transaction=3.0, behaviour=-2.5, context=1.0),
            MODEL,
            THRESHOLDS,
            "confirmation",
        )
        assert decision.risk == 99.0
        assert decision.confidence == 0.8
        assert [(c.channel, c.value) for c in decision.contributions] == [
            ("payee", 4.0),
            ("transaction", 3.0),
            ("behaviour", -2.5),
        ]


class TestFailOpen:
    def test_a_dependency_failure_monitors_with_no_confidence(self) -> None:
        decision = fail_open()
        assert decision.action == "monitor"
        assert decision.confidence == 0.0
        assert decision.risk is None
        assert decision.constraints == ("fail_open",)
