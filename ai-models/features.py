"""Feature set B: the deployable aggregate keystroke representation.

PROVISIONAL LOCATION, as with ``scoring.py``. These definitions belong in ``backend/shared/`` so the
score Lambda and the offline evaluation cannot diverge. Moved there once issue #1 is acknowledged.

Two representations, and why both exist
---------------------------------------
**Set A** is the 31 raw timing columns the benchmark ships: one hold time per key, one down-down and
one up-down latency per key transition. It is what the published baseline is computed over, so it is
the only representation that makes the O3 comparison meaningful. It is also unusable in deployment,
because it is defined by one specific 11-character password: change the credential and every feature
changes meaning.

**Set B**, defined here, aggregates timing into statistics that are independent of what was typed --
mean hold time, variability of flight time, how often the typist paused. Those transfer to any input
field, which is what the deployed client needs.

Set B therefore costs accuracy, because aggregating discards which *particular* key was slow. That
cost is worth measuring rather than assuming: it is the price of deployability, and no paper in the
survey reports it.

Three of the twelve features in the project's architecture diagram are omitted
------------------------------------------------------------------------------
Backspace rate, error correction rate and paste count cannot be computed from this benchmark. CMU
discarded every repetition containing a typing error, so there are no corrections, and there is no
paste event in the capture. They remain valid features for the deployed client, which sees real
editing behaviour, but they are structurally absent offline and so cannot appear in any number
compared against the benchmark. See issue #1.

The remaining nine are all derivable from the timing columns alone.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# A pause long enough that a human would perceive it as hesitation rather than as ordinary typing
# rhythm. Deliberately absolute rather than relative to the session: normalising by the session's own
# median would cancel out exactly the between-user difference the detector is trying to see.
PAUSE_THRESHOLD_SECONDS = 0.5

# Below this, successive keystrokes are effectively a single rolled motion rather than two separately
# initiated presses. Note that up-down intervals in this benchmark are frequently negative, because
# the next key goes down before the previous comes up, and those count as bursts.
BURST_THRESHOLD_SECONDS = 0.05

FEATURE_NAMES = [
    "mean_hold",
    "std_hold",
    "mean_flight",
    "std_flight",
    "burst_count",
    "pause_count",
    "longest_pause",
    "entry_duration",
    "typing_speed",
]

# Present in the architecture diagram, computable only from live browser capture.
DEPLOYMENT_ONLY_FEATURES = ["backspace_rate", "error_correction_rate", "paste_count"]


def _columns_by_prefix(frame: pd.DataFrame, prefix: str) -> list[str]:
    return [c for c in frame.columns if c.startswith(prefix)]


def extract(frame: pd.DataFrame) -> pd.DataFrame:
    """Aggregate raw benchmark timing columns into the Set B representation.

    Takes a frame with the benchmark's ``H.*``, ``DD.*`` and ``UD.*`` columns and returns one row of
    nine features per input row, with columns in ``FEATURE_NAMES`` order.
    """
    hold_columns = _columns_by_prefix(frame, "H.")
    down_down_columns = _columns_by_prefix(frame, "DD.")
    up_down_columns = _columns_by_prefix(frame, "UD.")

    if not (hold_columns and down_down_columns and up_down_columns):
        raise ValueError("frame does not contain H.*, DD.* and UD.* timing columns")

    hold = frame[hold_columns].to_numpy(dtype=float)
    down_down = frame[down_down_columns].to_numpy(dtype=float)
    flight = frame[up_down_columns].to_numpy(dtype=float)

    # Total entry time: summing consecutive down-down latencies spans first keypress to last
    # keypress, and adding the final hold extends it to the moment the last key is released.
    entry_duration = down_down.sum(axis=1) + hold[:, -1]

    keystrokes = len(hold_columns)

    # Divide only where the duration is positive. np.where would evaluate the division everywhere
    # first and warn on the degenerate rows even though it then discards them.
    typing_speed = np.divide(
        keystrokes,
        entry_duration,
        out=np.zeros_like(entry_duration, dtype=float),
        where=entry_duration > 0,
    )

    features = {
        "mean_hold": hold.mean(axis=1),
        "std_hold": hold.std(axis=1, ddof=1),
        "mean_flight": flight.mean(axis=1),
        "std_flight": flight.std(axis=1, ddof=1),
        "burst_count": (flight < BURST_THRESHOLD_SECONDS).sum(axis=1).astype(float),
        "pause_count": (flight > PAUSE_THRESHOLD_SECONDS).sum(axis=1).astype(float),
        "longest_pause": flight.max(axis=1),
        "entry_duration": entry_duration,
        # Zero for a non-positive duration, which cannot occur in this benchmark but would produce
        # a silent infinity in live capture from a clock anomaly.
        "typing_speed": typing_speed,
    }

    return pd.DataFrame(features, columns=FEATURE_NAMES, index=frame.index)


def with_features(frame: pd.DataFrame) -> pd.DataFrame:
    """Return the benchmark frame with the Set B columns appended.

    Keeps ``subject``, ``sessionIndex`` and ``rep`` alongside the aggregates so the result flows
    through the same split protocol as the raw representation.
    """
    return pd.concat([frame, extract(frame)], axis=1)
