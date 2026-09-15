"""Reference profiles and the scaled Manhattan anomaly score.

This is the detector, and it is the one piece of arithmetic that both tracks must agree on: the
research track measures it against the published benchmark, and the scoring Lambda runs it on live
sessions. Two implementations of it would mean the error rates in the report describe something
other than the deployed system, so there is exactly one, here.

Pure Python, standard library only. The vector sizes involved are tiny -- 31 features in the
benchmark representation, 12 in the deployed one -- so numpy would buy nothing at this scale while
costing a Lambda layer, a slower cold start and a second implementation of the same sums.

The detector
------------
A reference profile is a per-feature centre and a per-feature dispersion, estimated from a user's
enrolment repetitions. A session is scored by its total dispersion-normalised deviation from that
centre:

    score(y) = sum_i  |y_i - centre_i| / dispersion_i

Normalising per feature is what makes the sum meaningful. Hold times and flight times differ in
both scale and natural variability, so an unnormalised Manhattan distance would be dominated by
whichever features happen to be largest rather than by whichever are most inconsistent with this
particular user.

Choice of dispersion
--------------------
The published detector this project compares against uses the mean absolute deviation, which is
what "scaled" refers to in its name. Standard deviation is offered alongside it only so the two can
be measured rather than argued about. MAD is the default because reproducing the published figure
is the point of O3, and it is the more defensible choice on its own terms: it is far less sensitive
to occasional extreme repetitions, and keystroke timing is full of them. A user who pauses
mid-password produces one enormous flight time that would inflate a standard deviation and
desensitise that feature for every session afterwards.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from fraudcore.features import (
    DEVICE_CLASSES,
    DeviceClass,
    KeystrokeTiming,
    interval_cv,
    timing_shingles,
)

Scaling = Literal["mad", "std"]

# Timings in the benchmark are recorded to four decimal places, so 0.0001 s is the measurement
# resolution. A feature whose enrolment dispersion falls below that is constant as far as the
# instrument can tell, and dividing by it would produce an unbounded score from an immeasurably
# small deviation. Flooring at the resolution keeps such features highly sensitive but finite.
DISPERSION_FLOOR = 1e-4


@dataclass(frozen=True)
class ReferenceProfile:
    """A user's enrolled typing behaviour: a centre and a dispersion per feature.

    Deliberately small. It is a handful of statistics per feature rather than a trained model,
    which is why a profile fits in a single DynamoDB item.
    """

    feature_names: tuple[str, ...]
    centre: tuple[float, ...]
    dispersion: tuple[float, ...]
    scaling: Scaling
    floored_features: int

    @classmethod
    def fit(
        cls,
        enrolment: Sequence[Sequence[float]],
        feature_names: Sequence[str],
        scaling: Scaling = "mad",
    ) -> ReferenceProfile:
        """Estimate a reference profile from a user's enrolment repetitions.

        ``enrolment`` is a sequence of repetitions, each a sequence of feature values in
        ``feature_names`` order.
        """
        if scaling not in ("mad", "std"):
            raise ValueError(f"unknown scaling {scaling!r}, expected 'mad' or 'std'")

        names = tuple(feature_names)
        rows = [tuple(float(value) for value in row) for row in enrolment]

        if len(rows) < 2:
            raise ValueError("at least two enrolment repetitions are required")
        for index, row in enumerate(rows):
            if len(row) != len(names):
                raise ValueError(
                    f"enrolment row {index} has {len(row)} values but {len(names)} names"
                )

        count = len(rows)
        columns = list(zip(*rows, strict=True))
        centre = tuple(sum(column) / count for column in columns)

        if scaling == "mad":
            spread = [
                sum(abs(value - mean) for value in column) / count
                for column, mean in zip(columns, centre, strict=True)
            ]
        else:
            # Sample standard deviation, matching numpy's ddof=1, so the two implementations of
            # this profile are comparable figure for figure.
            spread = [
                (sum((value - mean) ** 2 for value in column) / (count - 1)) ** 0.5
                for column, mean in zip(columns, centre, strict=True)
            ]

        floored = sum(1 for value in spread if value < DISPERSION_FLOOR)

        return cls(
            feature_names=names,
            centre=centre,
            dispersion=tuple(max(value, DISPERSION_FLOOR) for value in spread),
            scaling=scaling,
            floored_features=floored,
        )

    def deviations(self, session: Sequence[float]) -> tuple[float, ...]:
        """Return the signed per-feature deviation of one session, in dispersion units.

        This is the raw material for the feature-level explanation required by O4: the sign gives
        the direction and the magnitude gives how far out the feature sits. Summing the absolute
        values reproduces ``score``.
        """
        values = self._checked(session)
        return tuple(
            (value - mean) / spread
            for value, mean, spread in zip(values, self.centre, self.dispersion, strict=True)
        )

    def score(self, session: Sequence[float]) -> float:
        """Return the anomaly score for one session.

        Higher means less like the enrolled user, which is the score convention applied throughout
        this project.
        """
        return sum(abs(deviation) for deviation in self.deviations(session))

    def top_deviations(
        self, session: Sequence[float], count: int = 3
    ) -> list[tuple[str, float]]:
        """Return the ``count`` features deviating most, as ``(name, signed_deviation)`` pairs."""
        signed = self.deviations(session)
        ranked = sorted(
            zip(self.feature_names, signed, strict=True),
            key=lambda pair: abs(pair[1]),
            reverse=True,
        )
        return ranked[:count]

    def _checked(self, session: Sequence[float]) -> list[float]:
        values = [float(value) for value in session]
        if len(values) != len(self.feature_names):
            raise ValueError(
                f"session has {len(values)} features, "
                f"profile expects {len(self.feature_names)}"
            )
        return values


@dataclass(frozen=True)
class DeviceProfiles:
    """One user's reference profiles, one per device class.

    Much of what looks like behavioural drift is a person switching from laptop to phone, so a
    session is only ever scored against the profile for its own device class. There is deliberately
    no fallback to another class: a missing profile returns ``None``, which the caller reports as
    no identity evidence rather than as an anomaly.
    """

    by_class: Mapping[DeviceClass, ReferenceProfile]

    def __post_init__(self) -> None:
        unknown = sorted(set(self.by_class) - set(DEVICE_CLASSES))
        if unknown:
            raise ValueError(f"unknown device classes {unknown}, expected {DEVICE_CLASSES}")
        if len({profile.feature_names for profile in self.by_class.values()}) > 1:
            raise ValueError("every device-class profile must share one feature set")
        object.__setattr__(self, "by_class", MappingProxyType(dict(self.by_class)))

    def profile_for(self, device_class: DeviceClass) -> ReferenceProfile | None:
        if device_class not in DEVICE_CLASSES:
            raise ValueError(f"unknown device class {device_class!r}")
        return self.by_class.get(device_class)

    def score(self, device_class: DeviceClass, session: Sequence[float]) -> float | None:
        """Score against this device class's profile, or ``None`` if the class has none yet."""
        profile = self.profile_for(device_class)
        return None if profile is None else profile.score(session)


# ---------------------------------------------------------------------------------------------
# Channel scorers
#
# Every channel reports a raw score, higher meaning riskier, and a confidence in [0, 1] saying how
# much evidence stands behind it. Confidence is evidence sufficiency, never certainty of guilt:
# fusion multiplies it into the standardised score, so a channel with no evidence pulls towards the
# base rate instead of in an arbitrary direction (architecture section 8.1).
# ---------------------------------------------------------------------------------------------

CHANNELS: tuple[str, ...] = ("behaviour", "automation", "transaction", "context", "payee")


@dataclass(frozen=True)
class ChannelScore:
    score: float
    confidence: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.score):
            raise ValueError("channel score must be finite")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence {self.confidence} is outside [0, 1]")


NO_EVIDENCE = ChannelScore(score=0.0, confidence=0.0)


def _saturating(amount: float, full: float) -> float:
    """Linear ramp from 0 at no evidence to 1 at ``full``, clamped."""
    return min(1.0, max(0.0, amount / full))


# The benchmark password is 11 keystrokes including Return; roughly four field entries of that
# length is where the identity evidence stops being dominated by one short field.
IDENTITY_FULL_KEYSTROKES = 40

# Cold start, architecture section 7.7: a device-class profile is not trusted until it has been
# built from five sessions.
IDENTITY_FULL_SESSIONS = 5


def behaviour_channel(
    profile: ReferenceProfile | None,
    features: Mapping[str, float],
    keystrokes: int,
    profile_sessions: int,
) -> ChannelScore:
    """Identity: scaled Manhattan distance from this device class's profile.

    A missing profile is no evidence, not an anomaly. Confidence is the product of how much typing
    was seen and how established the profile is, so a three-session profile cannot carry a
    confident verdict however far the session sits from it.
    """
    if profile is None:
        return NO_EVIDENCE
    session = [features[name] for name in profile.feature_names]
    confidence = _saturating(keystrokes, IDENTITY_FULL_KEYSTROKES) * _saturating(
        profile_sessions, IDENTITY_FULL_SESSIONS
    )
    return ChannelScore(score=profile.score(session), confidence=confidence)


# Human inter-key CV sits well above this; a fixed-delay script sits at zero. Regularity scores 1 at
# zero CV and falls linearly to 0 here.
AUTOMATION_HUMAN_CV = 0.2

# Intervals needed before a CV is a stable statistic rather than an accident of a short field.
AUTOMATION_FULL_INTERVALS = 15


def automation_channel(
    timing: KeystrokeTiming, replay_history: frozenset[int] | None = None
) -> ChannelScore:
    """Bot and replay: timing regularity, and overlap with previously seen timing shingles.

    The score is the stronger of the two signals, each in [0, 1]: a bot needs only one of the two
    tells to be caught. ``replay_history`` is the set of shingles from the user's recent sessions;
    ``None`` means it could not be read, and only regularity is scored.
    """
    regularity = max(0.0, (AUTOMATION_HUMAN_CV - interval_cv(timing)) / AUTOMATION_HUMAN_CV)

    replay = 0.0
    if replay_history:
        shingles = timing_shingles(timing)
        if shingles:
            replay = len(shingles & replay_history) / len(shingles)

    return ChannelScore(
        score=max(regularity, replay),
        confidence=_saturating(len(timing.down_down), AUTOMATION_FULL_INTERVALS),
    )
