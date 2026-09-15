"""The browser capture script emits timing only (CLAUDE.md section 2), checked by running it.

A typed secret -- a password and a card-like number -- is fed through the real capture module under
Node, and the behavioural payload it builds is inspected: exact keys, numbers only, and no trace of
the secret or of any key identity. This fails if anyone adds a key code, character or value to what
the page sends.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CAPTURE = REPO_ROOT / "client" / "capture.mjs"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None, reason="node is needed to execute the browser capture script"
)

SECRET = "Hunter2 4111 1111 1111 1111"

HARNESS = """
import * as capture from %(url)s;
const recorder = capture.createRecorder();
let t = 1000;
for (const ch of %(secret)s) {
  const code = ch === " " ? "Space" : /\\d/.test(ch) ? "Digit" + ch : "Key" + ch.toUpperCase();
  capture.keyDown(recorder, code, ch, t, false);
  t += 40 + (ch.charCodeAt(0) %% 17);
  capture.keyUp(recorder, code, t);
  t += 60 + (ch.charCodeAt(0) %% 23);
}
capture.keyDown(recorder, "Backspace", "Backspace", t, false);
capture.keyUp(recorder, "Backspace", t + 50);
t += 130;
capture.keyDown(recorder, "KeyX", "x", t, false);
capture.keyUp(recorder, "KeyX", t + 70);
capture.paste(recorder);
const behaviour = capture.buildBehaviour([recorder]);
process.stdout.write(JSON.stringify({ behaviour, fieldKeys: capture.FIELD_KEYS }));
"""


def _run(harness: str) -> dict[str, Any]:
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", harness],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def output() -> dict[str, Any]:
    return _run(HARNESS % {"url": json.dumps(CAPTURE.as_uri()), "secret": json.dumps(SECRET)})


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        # Keys are the pinned field names, checked separately; only values are payload content.
        return [s for item in value.values() for s in _strings(item)]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return []


def test_the_behaviour_object_has_only_fields(output: dict[str, Any]) -> None:
    assert set(output["behaviour"]) == {"fields"}


def test_every_field_has_exactly_the_timing_keys(output: dict[str, Any]) -> None:
    fields = output["behaviour"]["fields"]
    assert len(fields) == 1
    assert set(fields[0]) == set(output["fieldKeys"]) == {
        "hold",
        "down_down",
        "up_down",
        "backspaces",
        "corrections",
        "pastes",
    }


def test_the_payload_contains_no_string_values_at_all(output: dict[str, Any]) -> None:
    assert _strings(output["behaviour"]) == []


def test_no_trace_of_the_typed_secret_or_key_codes(output: dict[str, Any]) -> None:
    serialised = json.dumps(output["behaviour"])
    for fragment in ("Hunter", "hunter", "Key", "Digit", "Space", "Backspace"):
        assert fragment not in serialised


def test_timing_and_edit_counts_are_captured(output: dict[str, Any]) -> None:
    field = output["behaviour"]["fields"][0]
    # Every character plus the corrected "x"; the backspace is counted, not timed.
    assert len(field["hold"]) == len(SECRET) + 1
    assert len(field["down_down"]) == len(field["up_down"]) == len(SECRET)
    assert (field["backspaces"], field["corrections"], field["pastes"]) == (1, 1, 1)
