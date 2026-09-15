"""Adaptation Lambda: learns a behavioural profile only from passkey-verified sessions.

Triggered by the ``stepup.verified`` rule (architecture section 5.3), the one rule that makes
profile learning conditional on independent verification rather than on repetition. For each
verified step-up (section 7):

    1. read the decision and the user's device, account and profile state
    2. compute trust from evidence outside the behavioural channel
    3. claim atomically: record the verification, count the device session, and admit the session's
       keystroke features to the buffer if trust reaches tau_min
    4. rebuild the profile from the buffer: bootstrap after cold start, or a robust update bounded
       by the displacement budget, re-anchoring on full trust
    5. write the profile under optimistic concurrency, and emit ``budget_saturated`` when the budget
       bound the update

The live function rebuilds on every admitted session rather than every ``rebuild_every``. Verified
step-ups are rare, a rebuild over at most 200 short vectors is cheap, and the budget is measured
from the anchor, not the last rebuild, so rebuilding more often buys an attacker nothing.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from adaptation.store import AdaptationStore, Claim, DecisionRecord, UserContext
from fraudcore.adaptation import (
    AdaptationPolicy,
    BufferedSession,
    Profile,
    TrustEvidence,
    admit,
    non_behavioural_alert,
    rebuild,
    trust,
)
from fraudcore.events import STEPUP_VERIFIED, WORKFLOW_SOURCE, StepUpVerified, parse_stepup_verified
from fraudcore.fusion import FusionModel
from fraudcore.policy import ACTIONS

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

METRIC_NAMESPACE = "bfd/adaptation"


class Store(Protocol):
    def decision(self, decision_id: str) -> DecisionRecord | None: ...

    def session_features(self, session_id: str, uid: str) -> tuple[float, ...] | None: ...

    def context(self, uid: str, device_class: str, device_id: str, now: float) -> UserContext: ...

    def verification(self, uid: str, decision_id: str) -> Claim | None: ...

    def claim(
        self,
        verified: StepUpVerified,
        decision: DecisionRecord,
        tau: float,
        features: tuple[float, ...] | None,
        now: float,
    ) -> Claim | None: ...

    def buffer(self, uid: str, device_class: str, capacity: int) -> list[BufferedSession]: ...

    def save_profile(
        self,
        uid: str,
        device_class: str,
        profile: Profile,
        sessions: int,
        expected_version: int | None,
        now: float,
    ) -> None: ...


@dataclass(frozen=True)
class Dependencies:
    store: Store
    model: FusionModel
    policy: AdaptationPolicy
    clock: Callable[[], float] = time.time
    emit: Callable[[str], None] = print


def budget_saturated_metric(
    uid: str, device_class: str, displacement: float, now: float
) -> str:
    """A CloudWatch embedded-metric log line.

    The metric is dimensioned by device class only. A per-user dimension would create one billed
    custom metric per user; the user id rides along as a searchable property instead.
    """
    return json.dumps(
        {
            "_aws": {
                "Timestamp": int(now * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": METRIC_NAMESPACE,
                        "Dimensions": [["DeviceClass"]],
                        "Metrics": [{"Name": "budget_saturated", "Unit": "Count"}],
                    }
                ],
            },
            "DeviceClass": device_class,
            "budget_saturated": 1,
            "uid": uid,
            "candidate_displacement": displacement,
        }
    )


def handle(event: Mapping[str, Any], deps: Dependencies) -> dict[str, Any]:
    if event.get("source") != WORKFLOW_SOURCE or event.get("detail-type") != STEPUP_VERIFIED:
        LOGGER.warning("ignoring unexpected event %s", event.get("detail-type"))
        return {"recorded": False, "reason": "unexpected_event"}

    verified = parse_stepup_verified(event.get("detail") or {})
    # The rule matches passkey verification only. Checking again here means a rule edited by mistake
    # cannot let weaker evidence gate a profile update.
    if verified.method != "passkey":
        LOGGER.warning("ignoring %s verification for %s", verified.method, verified.decision_id)
        return {"recorded": False, "reason": "not_passkey"}

    now = deps.clock()
    decision = deps.store.decision(verified.decision_id)
    if decision is None or decision.uid != verified.uid or decision.action not in ACTIONS:
        LOGGER.warning("no usable decision %s for this user", verified.decision_id)
        return {"recorded": False, "reason": "decision_not_found"}

    context = deps.store.context(verified.uid, decision.device_class, decision.device_id, now)

    claim = deps.store.verification(verified.uid, verified.decision_id)
    if claim is None:
        tau = trust(
            TrustEvidence(
                verification="stepup",
                device_sessions=context.device_sessions,
                days_since_credential_change=context.days_since_credential_change,
                days_since_contact_change=context.days_since_contact_change,
                non_behavioural_alert=non_behavioural_alert(decision.scores, deps.model),
                action=decision.action,  # type: ignore[arg-type]
            )
        )
        features = deps.store.session_features(decision.session_id, verified.uid)
        admitted_features = features if admit(tau, deps.policy) else None
        claim = deps.store.claim(verified, decision, tau, admitted_features, now)
        if claim is None:
            # Lost a race with a concurrent delivery of the same event: use what it recorded.
            claim = deps.store.verification(verified.uid, verified.decision_id)
            if claim is None:
                raise RuntimeError("verification claim vanished")

    result: dict[str, Any] = {"recorded": True, "trust": claim.trust, "admitted": claim.admitted}
    if not claim.admitted:
        return {**result, "outcome": "not_admitted"}

    buffer = deps.store.buffer(verified.uid, decision.device_class, deps.policy.buffer_capacity)
    rebuilt = rebuild(context.profile, buffer, claim.trust, now, deps.policy)
    if rebuilt.profile is None:
        return {**result, "outcome": rebuilt.outcome, "buffered": len(buffer)}

    deps.store.save_profile(
        verified.uid,
        decision.device_class,
        rebuilt.profile,
        len(buffer),
        context.profile_version,
        now,
    )
    if rebuilt.saturated:
        deps.emit(
            budget_saturated_metric(
                verified.uid, decision.device_class, rebuilt.candidate_displacement, now
            )
        )
    return {
        **result,
        "outcome": rebuilt.outcome,
        "saturated": rebuilt.saturated,
        "reanchored": rebuilt.reanchored,
        "version": rebuilt.profile.version,
    }


_dependencies: Dependencies | None = None


def lambda_handler(event: Mapping[str, Any], context: Any) -> dict[str, Any]:
    global _dependencies
    if _dependencies is None:
        import boto3

        _dependencies = Dependencies(
            store=AdaptationStore(boto3.client("dynamodb"), os.environ["TABLE_NAME"]),
            model=FusionModel.load(),
            policy=AdaptationPolicy.load(),
        )
    return handle(event, _dependencies)
