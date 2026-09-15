"""The twelve keystroke timing features, and the coarse device class a profile is keyed by.

One definition, imported by the scoring Lambda and by the research track. The benchmark numbers in
the report are only a statement about the deployed scorer if both compute these features with the
same code, so there is no second copy anywhere.

Input: timing only
------------------
A ``KeystrokeTiming`` carries timing deltas and three edit counts. It has no field for which key was
pressed, what was typed, or anything derived from content, because none may leave the browser
(CLAUDE.md section 2). The client resolves backspace, correction and paste events to counts before
the payload is built; only the counts travel.

Why aggregates
--------------
The benchmark's raw representation has one hold time per key of one fixed password. It reproduces
the published baseline but means nothing once the credential changes. These features are statistics
over the whole entry -- mean hold, flight variability, pauses -- so they transfer to any field. That
transfer costs accuracy, which ``research/baseline_eer.py`` measures rather than assumes.

Nine of the twelve are computable from the CMU benchmark. The three edit features are not: CMU
discarded every repetition containing a typing error and records no paste events. They are valid for
live capture but structurally absent offline, so ``BENCHMARK_FEATURE_NAMES`` excludes them and no
benchmark figure may include them.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

# A pause long enough to read as hesitation rather than rhythm. Absolute rather than relative to the
# session: normalising by the session's own median would cancel the between-user difference the
# detector exists to see.
PAUSE_THRESHOLD_SECONDS = 0.5

# Below this, successive keystrokes are one rolled motion rather than two separate presses. Up-down
# intervals are often negative, when the next key goes down before the previous comes up, and those
# count as bursts.
BURST_THRESHOLD_SECONDS = 0.05

# Sample standard deviation of flight time needs two flights, so three keystrokes is the least that
# yields every feature.
MIN_KEYSTROKES = 3

BENCHMARK_FEATURE_NAMES: tuple[str, ...] = (
    "mean_hold",
    "std_hold",
    "mean_flight",
    "std_flight",
    "burst_count",
    "pause_count",
    "longest_pause",
    "entry_duration",
    "typing_speed",
)

DEPLOYMENT_ONLY_FEATURE_NAMES: tuple[str, ...] = (
    "backspace_rate",
    "error_correction_rate",
    "paste_count",
)

FEATURE_NAMES: tuple[str, ...] = BENCHMARK_FEATURE_NAMES + DEPLOYMENT_ONLY_FEATURE_NAMES

DeviceClass = Literal["desktop", "mobile", "tablet"]
DEVICE_CLASSES: tuple[DeviceClass, ...] = ("desktop", "mobile", "tablet")

# CSS pixels on the short side of the screen. The conventional phone/tablet breakpoint; a tablet in
# either orientation clears it and a phone in either orientation does not.
TABLET_MIN_SHORT_SIDE = 600


@dataclass(frozen=True)
class KeystrokeTiming:
    """Timing for one field entry of ``n`` keystrokes, in seconds.

    ``hold[i]`` is how long key ``i`` was down. ``down_down[i]`` and ``up_down[i]`` run from key
    ``i`` to key ``i + 1``, so both have ``n - 1`` entries. Up-down may be negative; the others may
    not, since they are durations or orderings on one clock.
    """

    hold: tuple[float, ...]
    down_down: tuple[float, ...]
    up_down: tuple[float, ...]
    backspaces: int = 0
    corrections: int = 0
    pastes: int = 0

    def __post_init__(self) -> None:
        for name in ("hold", "down_down", "up_down"):
            values = tuple(float(value) for value in getattr(self, name))
            if not all(math.isfinite(value) for value in values):
                raise ValueError(f"{name} contains a non-finite value")
            object.__setattr__(self, name, values)

        count = len(self.hold)
        if count < MIN_KEYSTROKES:
            raise ValueError(f"need at least {MIN_KEYSTROKES} keystrokes, got {count}")
        if len(self.down_down) != count - 1 or len(self.up_down) != count - 1:
            raise ValueError(f"{count} holds need {count - 1} down_down and up_down intervals")
        if min(self.hold) < 0:
            raise ValueError("hold contains a negative duration")
        if min(self.down_down) < 0:
            raise ValueError("down_down contains a negative interval")
        if min(self.backspaces, self.corrections, self.pastes) < 0:
            raise ValueError("edit counts cannot be negative")

    @classmethod
    def from_events(
        cls,
        down_times: Sequence[float],
        up_times: Sequence[float],
        backspaces: int = 0,
        corrections: int = 0,
        pastes: int = 0,
    ) -> KeystrokeTiming:
        """Build timing from per-keystroke press and release timestamps, in press order."""
        downs = [float(value) for value in down_times]
        ups = [float(value) for value in up_times]
        if len(downs) != len(ups):
            raise ValueError(f"{len(downs)} key-down times but {len(ups)} key-up times")

        return cls(
            hold=tuple(up - down for down, up in zip(downs, ups, strict=True)),
            down_down=tuple(downs[i + 1] - downs[i] for i in range(len(downs) - 1)),
            up_down=tuple(downs[i + 1] - ups[i] for i in range(len(downs) - 1)),
            backspaces=backspaces,
            corrections=corrections,
            pastes=pastes,
        )


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _sample_std(values: Sequence[float]) -> float:
    # ddof=1, matching the research track's earlier numpy implementation figure for figure.
    mean = _mean(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


def extract(timing: KeystrokeTiming) -> dict[str, float]:
    """Return all twelve features, keyed by name, in ``FEATURE_NAMES`` order."""
    hold, flight = timing.hold, timing.up_down
    count = len(hold)

    # Summed down-down spans first press to last press; the final hold extends it to last release.
    entry_duration = sum(timing.down_down) + hold[-1]

    return {
        "mean_hold": _mean(hold),
        "std_hold": _sample_std(hold),
        "mean_flight": _mean(flight),
        "std_flight": _sample_std(flight),
        "burst_count": float(sum(1 for value in flight if value < BURST_THRESHOLD_SECONDS)),
        "pause_count": float(sum(1 for value in flight if value > PAUSE_THRESHOLD_SECONDS)),
        "longest_pause": max(flight),
        "entry_duration": entry_duration,
        # Zero rather than infinity for a zero duration, which a live clock anomaly can produce.
        "typing_speed": count / entry_duration if entry_duration > 0 else 0.0,
        "backspace_rate": timing.backspaces / count,
        "error_correction_rate": timing.corrections / count,
        "paste_count": float(timing.pastes),
    }


def interval_cv(timing: KeystrokeTiming) -> float:
    """Coefficient of variation of the down-down intervals.

    Humans are irregular: consecutive presses vary by tens of percent. A script with a fixed or
    lightly jittered delay is not, so a CV near zero is the automation signature. All-zero
    intervals are perfectly regular and report zero rather than dividing by zero.
    """
    mean = _mean(timing.down_down)
    if mean <= 0:
        return 0.0
    return _sample_std(timing.down_down) / mean


# Replay detection quantises timings to this resolution before hashing. At 1 ms an exact replay of
# recorded events matches and a human re-typing does not. A replay injected with scheduler jitter
# above the resolution evades it; that is a stated limit, and the reason regularity is scored too.
REPLAY_RESOLUTION_SECONDS = 0.001

# Consecutive quantised timings per shingle. Long enough that a human repeating a rhythm will not
# collide by chance, short enough that splicing a recorded fragment into a new entry still matches.
REPLAY_WINDOW = 6

# Polynomial rolling hash. The hashes are persisted and compared across processes, so Python's
# built-in hash is unusable; this is explicit and stable.
_HASH_BASE = 1_000_003
_HASH_MODULUS = (1 << 61) - 1


def timing_shingles(
    timing: KeystrokeTiming,
    window: int = REPLAY_WINDOW,
    resolution: float = REPLAY_RESOLUTION_SECONDS,
) -> frozenset[int]:
    """Return rolling hashes of every ``window`` consecutive quantised timings.

    The sequence interleaves holds and down-down intervals in keystroke order, so it describes the
    rhythm completely without describing a single key. An entry shorter than ``window`` yields no
    shingles, which the automation channel reports as no replay evidence.
    """
    if window < 1:
        raise ValueError("window must be at least 1")
    if resolution <= 0:
        raise ValueError("resolution must be positive")

    symbols: list[int] = []
    for index, hold in enumerate(timing.hold):
        symbols.append(round(hold / resolution))
        if index < len(timing.down_down):
            symbols.append(round(timing.down_down[index] / resolution))

    if len(symbols) < window:
        return frozenset()

    leading = pow(_HASH_BASE, window - 1, _HASH_MODULUS)
    current = 0
    for symbol in symbols[:window]:
        current = (current * _HASH_BASE + symbol) % _HASH_MODULUS

    shingles = {current}
    for outgoing, incoming in zip(symbols, symbols[window:], strict=False):
        current = ((current - outgoing * leading) * _HASH_BASE + incoming) % _HASH_MODULUS
        shingles.add(current)
    return frozenset(shingles)


def vector(features: dict[str, float], names: Sequence[str] = FEATURE_NAMES) -> list[float]:
    """Order a feature mapping into the vector a ``ReferenceProfile`` expects."""
    return [features[name] for name in names]


def device_class(max_touch_points: int, coarse_pointer: bool, short_side_px: int) -> DeviceClass:
    """Derive the coarse device class a profile is keyed by.

    Deliberately coarse. Its only job is to stop phone typing being scored against a keyboard
    profile; anything finer is a fingerprint and belongs to the context channel.
    """
    if max_touch_points <= 0 and not coarse_pointer:
        return "desktop"
    return "tablet" if short_side_px >= TABLET_MIN_SHORT_SIDE else "mobile"


# ---------------------------------------------------------------------------------------------
# Transaction, context and payee features
#
# These come from the transfer request and from state the fast path reads by key, never from the
# behavioural capture payload. The amount below is the amount of the transfer being scored; it is
# not, and must not become, a field of ``KeystrokeTiming``.
# ---------------------------------------------------------------------------------------------

HOURS_PER_DAY = 24

# One currency unit, or a tenth of the median, whichever is larger. Floors the spread for a user
# whose p50 and p95 coincide, so a slightly larger transfer reads as unusual rather than infinite.
AMOUNT_SPREAD_FLOOR = 1.0
AMOUNT_SPREAD_FLOOR_FRACTION = 0.1

# Mirrors c_device in architecture section 7.3: a device is enrolled once it has this many prior
# sessions.
ENROLLED_DEVICE_SESSIONS = 10

# A credential or contact change inside this window is the classic takeover preparation step.
STABILITY_WINDOW_DAYS = 7.0

# A payee first used within this window is still new.
NEW_PAYEE_WINDOW_DAYS = 30.0


def _clamp_unit(value: float) -> float:
    return min(1.0, max(0.0, value))


def _recency(days: float | None, window: float) -> float:
    """1.0 for an event just now, falling linearly to 0.0 at ``window`` days; 0.0 if none."""
    return 0.0 if days is None else _clamp_unit(1.0 - days / window)


@dataclass(frozen=True)
class UserAggregates:
    """A user's rolling transaction statistics, precomputed by the slow plane.

    Read from ``AGG#<uid> / WINDOW#<window>``. ``history_count`` is how many transfers the
    statistics were computed over, which is what the transaction channel's confidence rests on.
    """

    amount_p50: float
    amount_p95: float
    daily_count_p95: float
    hour_histogram: tuple[float, ...]
    history_count: int

    def __post_init__(self) -> None:
        histogram = tuple(float(value) for value in self.hour_histogram)
        object.__setattr__(self, "hour_histogram", histogram)
        if len(histogram) != HOURS_PER_DAY:
            raise ValueError(f"hour_histogram needs {HOURS_PER_DAY} bins, got {len(histogram)}")
        if min(histogram) < 0:
            raise ValueError("hour_histogram contains a negative count")
        if not 0 <= self.amount_p50 <= self.amount_p95:
            raise ValueError("amount quantiles must satisfy 0 <= p50 <= p95")
        if self.daily_count_p95 < 0 or self.history_count < 0:
            raise ValueError("counts cannot be negative")


@dataclass(frozen=True)
class Transfer:
    """The transfer being scored. ``transfers_24h`` includes this one."""

    amount: float
    hour: int
    transfers_24h: int

    def __post_init__(self) -> None:
        if not (math.isfinite(self.amount) and self.amount > 0):
            raise ValueError("amount must be positive and finite")
        if not 0 <= self.hour < HOURS_PER_DAY:
            raise ValueError(f"hour {self.hour} is outside 0 to 23")
        if self.transfers_24h < 1:
            raise ValueError("transfers_24h includes this transfer, so it is at least 1")


def transaction_features(transfer: Transfer, aggregates: UserAggregates) -> dict[str, float]:
    """Amount, hour and velocity relative to this user's own history.

    ``amount_z`` is in units of the user's median-to-p95 spread, so 1.0 is a p95-sized transfer.
    Quantiles rather than mean and standard deviation, because amounts are heavy-tailed and one
    large rent payment should not desensitise the channel for a year.
    """
    spread = max(
        aggregates.amount_p95 - aggregates.amount_p50,
        AMOUNT_SPREAD_FLOOR_FRACTION * aggregates.amount_p50,
        AMOUNT_SPREAD_FLOOR,
    )
    histogram = aggregates.hour_histogram
    return {
        "amount_z": (transfer.amount - aggregates.amount_p50) / spread,
        # Add-one smoothing: 0 at the user's busiest hour, approaching 1 at an hour never used, and
        # 0 everywhere for an empty histogram rather than a division by zero.
        "hour_rarity": 1.0 - (histogram[transfer.hour] + 1.0) / (max(histogram) + 1.0),
        "velocity": transfer.transfers_24h / max(aggregates.daily_count_p95, 1.0),
    }


@dataclass(frozen=True)
class DeviceRecord:
    """A device previously seen on this account, from ``USER#<uid> / DEV#<fingerprint>``."""

    session_count: int

    def __post_init__(self) -> None:
        if self.session_count < 0:
            raise ValueError("session_count cannot be negative")


@dataclass(frozen=True)
class SessionContext:
    """Device and account-stability facts for one session.

    ``device`` is ``None`` for a device never seen on this account. The ``days_since`` fields are
    ``None`` when the credential or contact details have never changed.
    """

    device: DeviceRecord | None
    days_since_credential_change: float | None
    days_since_contact_change: float | None
    account_sessions: int

    def __post_init__(self) -> None:
        for days in (self.days_since_credential_change, self.days_since_contact_change):
            if days is not None and days < 0:
                raise ValueError("days since a change cannot be negative")
        if self.account_sessions < 0:
            raise ValueError("account_sessions cannot be negative")


def context_features(context: SessionContext) -> dict[str, float]:
    if context.device is None:
        novelty = 1.0
    elif context.device.session_count < ENROLLED_DEVICE_SESSIONS:
        novelty = 0.5
    else:
        novelty = 0.0
    return {
        "device_novelty": novelty,
        "credential_recency": _recency(context.days_since_credential_change, STABILITY_WINDOW_DAYS),
        "contact_recency": _recency(context.days_since_contact_change, STABILITY_WINDOW_DAYS),
    }


@dataclass(frozen=True)
class PayeeEdge:
    """This user's history with this payee, from ``USER#<uid> / PAYEE#<pid>``."""

    days_since_first_seen: float
    verified: bool

    def __post_init__(self) -> None:
        if self.days_since_first_seen < 0:
            raise ValueError("days_since_first_seen cannot be negative")


@dataclass(frozen=True)
class PayeeRisk:
    """Cross-user destination risk from ``PAYEE#<pid> / RISK``, computed by the batch aggregator.

    ``flagged`` is the batch-derived flag that the corroboration rule accepts in place of a second
    channel above threshold (architecture section 8.3).
    """

    risk_score: float
    hours_since_computed: float
    flagged: bool = False

    def __post_init__(self) -> None:
        if not 0.0 <= self.risk_score <= 1.0:
            raise ValueError(f"risk_score {self.risk_score} is outside [0, 1]")
        if self.hours_since_computed < 0:
            raise ValueError("hours_since_computed cannot be negative")


def payee_features(edge: PayeeEdge | None, risk: PayeeRisk | None) -> dict[str, float]:
    """``edge`` is ``None`` for a payee never sent to; ``risk`` is ``None`` for one never scored."""
    return {
        "payee_novelty": (
            1.0 if edge is None else _recency(edge.days_since_first_seen, NEW_PAYEE_WINDOW_DAYS)
        ),
        "payee_unverified": 0.0 if edge is not None and edge.verified else 1.0,
        "global_risk": 0.0 if risk is None else risk.risk_score,
    }
