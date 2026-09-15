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
