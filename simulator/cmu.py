"""The CMU benchmark, read for the simulator with the standard library only.

Each CMU repetition is one entry of the same 11-key password, with per-key hold, down-down and
up-down timings. That is exactly the shape of one captured field in the live payload, so real
recorded typing can be sent to the deployed endpoint unchanged. No pandas here: the simulator drives
the deployed system and needs only boto3 and fraudcore.
"""

from __future__ import annotations

import csv
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATH = REPO_ROOT / "research" / "data" / "raw" / "DSL-StrongPasswordData.csv"

# The benchmark records timings to four decimal places; the live payload is rounded the same way.
DECIMALS = 4


@dataclass(frozen=True)
class Repetition:
    subject: str
    session: int
    rep: int
    hold: tuple[float, ...]
    down_down: tuple[float, ...]
    up_down: tuple[float, ...]


def load(path: Path = DEFAULT_PATH) -> dict[str, list[Repetition]]:
    """Every subject's repetitions, in recording order."""
    if not path.exists():
        raise FileNotFoundError(f"benchmark not found at {path}; run 'make dataset' first")
    subjects: dict[str, list[Repetition]] = defaultdict(list)
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames or []
        holds = [c for c in columns if c.startswith("H.")]
        down_downs = [c for c in columns if c.startswith("DD.")]
        up_downs = [c for c in columns if c.startswith("UD.")]
        for row in reader:
            subjects[row["subject"]].append(
                Repetition(
                    subject=row["subject"],
                    session=int(row["sessionIndex"]),
                    rep=int(row["rep"]),
                    hold=tuple(float(row[c]) for c in holds),
                    down_down=tuple(float(row[c]) for c in down_downs),
                    up_down=tuple(float(row[c]) for c in up_downs),
                )
            )
    return {
        subject: sorted(reps, key=lambda r: (r.session, r.rep))
        for subject, reps in sorted(subjects.items())
    }


def field(repetition: Repetition) -> dict[str, Any]:
    """One captured field, in the live payload's exact shape."""
    return {
        "hold": [round(value, DECIMALS) for value in repetition.hold],
        "down_down": [round(value, DECIMALS) for value in repetition.down_down],
        "up_down": [round(value, DECIMALS) for value in repetition.up_down],
        "backspaces": 0,
        "corrections": 0,
        "pastes": 0,
    }
