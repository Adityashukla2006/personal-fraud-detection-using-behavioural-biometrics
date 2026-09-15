"""Tests for logistic fusion: the risk arithmetic, exact attribution and the confidence summary.

The model below is chosen so that every standardised score is a small integer and the logit is
exactly zero, which puts the risk at exactly 50 and can be checked by hand.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from fraudcore import fusion
from fraudcore.fusion import Contribution, FusionModel
from fraudcore.scoring import CHANNELS, NO_EVIDENCE, ChannelScore

SPEC: dict[str, Any] = {
    "intercept": -2.0,
    "z_limit": 6.0,
    "channels": {
        "behaviour": {"weight": 2.0, "mean": 10.0, "scale": 10.0, "alert_z": 2.0},
        "automation": {"weight": 1.0, "mean": 0.0, "scale": 1.0, "alert_z": 2.0},
        "transaction": {"weight": 1.0, "mean": 1.0, "scale": 2.0, "alert_z": 2.0},
        "context": {"weight": 1.0, "mean": 0.0, "scale": 1.0, "alert_z": 2.0},
        "payee": {"weight": 1.0, "mean": 1.0, "scale": 1.0, "alert_z": 2.0},
    },
}
MODEL = FusionModel.from_dict(SPEC)

# behaviour    z = 0.5 * (30 - 10) / 10 = 1.0    contribution  2.0
# automation   z = 1.0 * (0 - 0) / 1    = 0.0    contribution  0.0
# transaction  z = 1.0 * (3 - 1) / 2    = 1.0    contribution  1.0
# context      absent, no evidence      = 0.0    contribution  0.0
# payee        z = 1.0 * (0 - 1) / 1    = -1.0   contribution -1.0
#
# logit      = -2 + 2 + 0 + 1 + 0 - 1 = 0, so risk = 50
# confidence = (2 * 0.5 + 1 + 1 + 0 + 1) / 6 = 2/3
SCORES = {
    "behaviour": ChannelScore(30.0, 0.5),
    "automation": ChannelScore(0.0, 1.0),
    "transaction": ChannelScore(3.0, 1.0),
    "payee": ChannelScore(0.0, 1.0),
}


@pytest.fixture(scope="module")
def fused() -> fusion.Fusion:
    return fusion.fuse(MODEL, SCORES)


class TestKnownAnswers:
    def test_risk_and_logit_match_the_hand_computed_values(self, fused: fusion.Fusion) -> None:
        assert fused.logit == pytest.approx(0.0, abs=1e-12)
        assert fused.risk == pytest.approx(50.0)

    def test_a_logit_of_ln_3_is_a_risk_of_75(self) -> None:
        # sigmoid(ln 3) = 3 / (1 + 3)
        model = FusionModel.from_dict({**SPEC, "intercept": math.log(3)})
        assert fusion.fuse(model, {}).risk == pytest.approx(75.0)

    def test_confidence_is_the_weighted_mean_of_channel_confidences(
        self, fused: fusion.Fusion
    ) -> None:
        assert fused.confidence == pytest.approx(2 / 3)


class TestAttribution:
    def test_contributions_sum_exactly_to_the_logit(self, fused: fusion.Fusion) -> None:
        total = MODEL.intercept + sum(c.value for c in fused.contributions)
        assert total == pytest.approx(fused.logit, abs=1e-12)

    def test_top_three_are_ranked_by_magnitude_and_keep_their_sign(
        self, fused: fusion.Fusion
    ) -> None:
        # transaction and payee tie at magnitude 1.0; the declared channel order breaks the tie.
        assert fused.top() == (
            Contribution("behaviour", pytest.approx(2.0)),  # type: ignore[arg-type]
            Contribution("transaction", pytest.approx(1.0)),  # type: ignore[arg-type]
            Contribution("payee", pytest.approx(-1.0)),  # type: ignore[arg-type]
        )

    def test_every_channel_is_attributed(self, fused: fusion.Fusion) -> None:
        assert {c.channel for c in fused.contributions} == set(CHANNELS)


class TestConfidenceShrinkage:
    def test_a_zero_confidence_channel_contributes_nothing_however_extreme(self) -> None:
        extreme = fusion.fuse(MODEL, {"behaviour": ChannelScore(1e9, 0.0)})
        assert extreme.z["behaviour"] == 0.0
        assert extreme.risk == pytest.approx(fusion.fuse(MODEL, {}).risk)

    def test_an_absent_channel_is_the_same_as_no_evidence(self) -> None:
        assert fusion.fuse(MODEL, {"context": NO_EVIDENCE}) == fusion.fuse(MODEL, {})

    def test_an_unknown_channel_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown channels"):
            fusion.fuse(MODEL, {"scam_intent": ChannelScore(1.0, 1.0)})


class TestBoundedRepresentation:
    def test_an_extreme_score_is_clipped_at_the_limit(self) -> None:
        # behaviour standardises to (1e6 - 10) / 10, far beyond 6; at confidence 0.5, z = 3.0
        fused = fusion.fuse(MODEL, {"behaviour": ChannelScore(1e6, 0.5)})
        assert fused.z["behaviour"] == pytest.approx(3.0)
        assert fused.top(1)[0].value == pytest.approx(6.0)

    def test_a_score_exactly_at_the_limit_is_unchanged(self) -> None:
        # (70 - 10) / 10 = 6.0 exactly
        assert fusion.fuse(MODEL, {"behaviour": ChannelScore(70.0, 1.0)}).z[
            "behaviour"
        ] == pytest.approx(6.0)

    def test_the_clip_is_symmetric(self) -> None:
        fused = fusion.fuse(MODEL, {"payee": ChannelScore(-1e6, 1.0)})
        assert fused.z["payee"] == pytest.approx(-6.0)

    def test_the_observation_itself_is_not_modified(self) -> None:
        score = ChannelScore(1e6, 1.0)
        fusion.fuse(MODEL, {"behaviour": score})
        assert score.score == 1e6

    def test_attribution_stays_exact_when_clipped(self) -> None:
        fused = fusion.fuse(MODEL, {"behaviour": ChannelScore(1e6, 1.0), **SCORES})
        total = MODEL.intercept + sum(c.value for c in fused.contributions)
        assert total == pytest.approx(fused.logit, abs=1e-12)

    def test_an_alert_threshold_above_the_limit_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="alert_z exceeds z_limit"):
            FusionModel.from_dict({**SPEC, "z_limit": 1.9})

    def test_an_alert_threshold_exactly_at_the_limit_is_allowed(self) -> None:
        FusionModel.from_dict({**SPEC, "z_limit": 2.0})

    def test_a_non_positive_limit_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="z_limit"):
            FusionModel.from_dict({**SPEC, "z_limit": 0.0})


class TestNumericalStability:
    @pytest.mark.parametrize(("intercept", "expected"), [(-1000.0, 0.0), (1000.0, 100.0)])
    def test_extreme_logits_saturate_without_overflow(
        self, intercept: float, expected: float
    ) -> None:
        model = FusionModel.from_dict({**SPEC, "intercept": intercept})
        assert fusion.fuse(model, {}).risk == pytest.approx(expected)


class TestModelValidation:
    def _channels(self, **override: float) -> dict[str, Any]:
        channels = {name: dict(spec) for name, spec in SPEC["channels"].items()}
        channels["payee"].update(override)
        return {**SPEC, "channels": channels}

    def test_a_missing_channel_is_rejected(self) -> None:
        spec = {**SPEC, "channels": dict(list(SPEC["channels"].items())[:4])}
        with pytest.raises(ValueError, match="exactly the channels"):
            FusionModel.from_dict(spec)

    def test_a_negative_weight_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="negative"):
            FusionModel.from_dict(self._channels(weight=-0.1))

    def test_a_zero_weight_is_allowed(self) -> None:
        FusionModel.from_dict(self._channels(weight=0.0))

    def test_a_zero_scale_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="scale"):
            FusionModel.from_dict(self._channels(scale=0.0))

    def test_all_zero_weights_report_zero_confidence(self) -> None:
        channels = {name: {**spec, "weight": 0.0} for name, spec in SPEC["channels"].items()}
        model = FusionModel.from_dict({**SPEC, "channels": channels})
        assert fusion.fuse(model, SCORES).confidence == 0.0


def test_the_shipped_model_file_loads_and_covers_every_channel() -> None:
    model = FusionModel.load()
    assert set(model.channels) == set(CHANNELS)
