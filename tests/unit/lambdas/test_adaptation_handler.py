"""Tests for the adaptation Lambda, with a fake store.

The rule under test: a profile changes only for a passkey-verified session whose trust, computed
from non-behavioural evidence, reaches tau_min; and a redelivered event changes nothing twice.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from adaptation.handler import Dependencies, budget_saturated_metric, handle
from adaptation.store import Claim, DecisionRecord, UserContext
from fraudcore.adaptation import (
    PROFILE_FEATURES,
    AdaptationPolicy,
    BufferedSession,
    Profile,
)
from fraudcore.events import StepUpVerified
from fraudcore.fusion import FusionModel
from fraudcore.scoring import ChannelScore

NOW = 1_789_463_210.0
UID = "21d35d6a-a031-708a-d069-7ca41d1ac623"
WIDTH = len(PROFILE_FEATURES)
POLICY = AdaptationPolicy(budget=1.0, scale_budget=1.0)
EVENT = {
    "source": "bfd.workflow",
    "detail-type": "stepup.verified",
    "detail": {
        "schema": 1,
        "uid": UID,
        "transfer_id": "636de234abcd",
        "decision_id": "636de234abcd",
        "verification": {"verified": True, "method": "passkey", "verified_at": 1_789_463_206},
    },
}
QUIET = {name: ChannelScore(0.0, 1.0) for name in ("transaction", "context", "payee")}
DECISION = DecisionRecord(UID, "session-1", "desktop", "device-1", "step_up", QUIET)
ENROLLED = UserContext(None, None, 12, None, None)
FEATURES = tuple([0.1] * WIDTH)


class FakeStore:
    def __init__(
        self,
        decision: DecisionRecord | None = DECISION,
        context: UserContext = ENROLLED,
        features: tuple[float, ...] | None = FEATURES,
        buffered: int = 5,
        existing: Claim | None = None,
    ) -> None:
        self._decision = decision
        self._context = context
        self._features = features
        self._buffered = buffered
        self._existing = existing
        self.claims: list[tuple[float, tuple[float, ...] | None]] = []
        self.saved: list[tuple[Profile, int, int | None]] = []

    def decision(self, decision_id: str) -> DecisionRecord | None:
        return self._decision

    def session_features(self, session_id: str, uid: str) -> tuple[float, ...] | None:
        return self._features

    def context(self, uid: str, device_class: str, device_id: str, now: float) -> UserContext:
        return self._context

    def verification(self, uid: str, decision_id: str) -> Claim | None:
        return self._existing

    def claim(
        self,
        verified: StepUpVerified,
        decision: DecisionRecord,
        tau: float,
        features: tuple[float, ...] | None,
        now: float,
    ) -> Claim | None:
        self.claims.append((tau, features))
        return Claim(tau, features is not None)

    def buffer(self, uid: str, device_class: str, capacity: int) -> list[BufferedSession]:
        return [BufferedSession(FEATURES, 1.0) for _ in range(self._buffered)]

    def save_profile(
        self,
        uid: str,
        device_class: str,
        profile: Profile,
        sessions: int,
        expected_version: int | None,
        now: float,
    ) -> None:
        self.saved.append((profile, sessions, expected_version))


def _call(store: FakeStore, event: dict[str, Any] = EVENT) -> tuple[dict[str, Any], list[str]]:
    emitted: list[str] = []
    deps = Dependencies(
        store=store,
        model=FusionModel.load(),
        policy=POLICY,
        clock=lambda: NOW,
        emit=emitted.append,
    )
    return handle(event, deps), emitted


def _with_method(method: str) -> dict[str, Any]:
    detail = {**EVENT["detail"], "verification": {"verified": True, "method": method}}
    return {**EVENT, "detail": detail}


class TestWhatReachesTheProfile:
    def test_a_trusted_session_after_cold_start_bootstraps_the_profile(self) -> None:
        store = FakeStore()
        result, _ = _call(store)
        assert (result["trust"], result["admitted"]) == (1.0, True)
        assert result["outcome"] == "bootstrapped"
        profile, sessions, expected = store.saved[0]
        assert profile.centre == pytest.approx(FEATURES)
        assert (sessions, expected) == (5, None)

    def test_before_cold_start_the_session_is_buffered_but_no_profile_is_written(self) -> None:
        store = FakeStore(buffered=POLICY.cold_start_sessions - 1)
        result, _ = _call(store)
        assert result["outcome"] == "cold_start"
        assert store.claims[0][1] == FEATURES
        assert store.saved == []

    def test_a_new_device_has_zero_trust_and_is_never_admitted(self) -> None:
        store = FakeStore(context=dataclasses.replace(ENROLLED, device_sessions=None))
        result, _ = _call(store)
        assert (result["trust"], result["outcome"]) == (0.0, "not_admitted")
        assert store.claims == [(0.0, None)]
        assert store.saved == []

    def test_a_young_device_lands_exactly_on_tau_min_and_is_admitted(self) -> None:
        store = FakeStore(context=dataclasses.replace(ENROLLED, device_sessions=1))
        result, _ = _call(store)
        assert result["trust"] == POLICY.tau_min
        assert result["admitted"]

    def test_a_non_behavioural_alert_halves_trust(self) -> None:
        loud = {**QUIET, "payee": ChannelScore(1e6, 1.0)}
        store = FakeStore(
            decision=dataclasses.replace(DECISION, scores=loud),
            context=dataclasses.replace(ENROLLED, device_sessions=1),
        )
        result, _ = _call(store)
        assert (result["trust"], result["admitted"]) == (0.25, False)

    def test_a_restricted_session_is_never_learned(self) -> None:
        store = FakeStore(decision=dataclasses.replace(DECISION, action="restrict"))
        assert _call(store)[0]["trust"] == 0.0

    def test_a_recent_credential_change_vetoes(self) -> None:
        store = FakeStore(context=dataclasses.replace(ENROLLED, days_since_credential_change=2.0))
        assert _call(store)[0]["admitted"] is False

    def test_a_session_without_typing_is_recorded_but_not_admitted(self) -> None:
        store = FakeStore(features=None)
        result, _ = _call(store)
        assert (result["trust"], result["admitted"]) == (1.0, False)


class TestWhatIsIgnored:
    @pytest.mark.parametrize("method", ["password", "email_otp"])
    def test_weaker_verification_never_claims(self, method: str) -> None:
        store = FakeStore()
        assert _call(store, _with_method(method))[0]["reason"] == "not_passkey"
        assert store.claims == []

    def test_other_events_are_ignored(self) -> None:
        store = FakeStore()
        assert _call(store, {**EVENT, "source": "bfd.scoring"})[0]["reason"] == "unexpected_event"

    def test_a_decision_belonging_to_someone_else_is_not_learned(self) -> None:
        store = FakeStore(decision=dataclasses.replace(DECISION, uid="someone-else"))
        assert _call(store)[0]["reason"] == "decision_not_found"
        assert store.claims == []


class TestRedelivery:
    def test_an_existing_claim_is_reused_and_never_claimed_again(self) -> None:
        store = FakeStore(existing=Claim(1.0, True))
        result, _ = _call(store)
        assert store.claims == []
        assert result["outcome"] == "bootstrapped"

    def test_an_existing_unadmitted_claim_stays_unadmitted(self) -> None:
        store = FakeStore(existing=Claim(0.0, False))
        assert _call(store)[0]["outcome"] == "not_admitted"
        assert store.saved == []


class TestBudget:
    def _anchored(self, centre: float) -> Profile:
        vector = tuple([centre] * WIDTH)
        scale = tuple([0.01] * WIDTH)
        return Profile(vector, scale, vector, scale, 0.0, None, 0, 3)

    def test_a_saturated_update_is_written_projected_and_emits_the_metric(self) -> None:
        # Anchor at 5.0 with scale 0.01, buffer at 0.1: displacement 490, budget 1.
        context = dataclasses.replace(ENROLLED, profile=self._anchored(5.0), profile_version=3)
        store = FakeStore(context=context)
        result, emitted = _call(store)

        assert (result["saturated"], result["version"]) == (True, 4)
        profile, _, expected = store.saved[0]
        assert expected == 3
        assert profile.saturations == 1
        metric = json.loads(emitted[0])
        assert metric["budget_saturated"] == 1
        assert metric["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["DeviceClass"]]

    def test_an_update_within_budget_emits_nothing(self) -> None:
        context = dataclasses.replace(ENROLLED, profile=self._anchored(0.1), profile_version=3)
        result, emitted = _call(FakeStore(context=context))
        assert result["saturated"] is False
        assert emitted == []


def test_the_metric_line_carries_the_user_as_a_property_not_a_dimension() -> None:
    line = json.loads(budget_saturated_metric(UID, "mobile", 2.5, NOW))
    assert line["uid"] == UID
    assert line["DeviceClass"] == "mobile"
    assert line["_aws"]["Timestamp"] == int(NOW * 1000)
