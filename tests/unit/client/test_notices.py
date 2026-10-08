"""The customer is told when a transfer they were waiting on settles, and only then."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
NOTICES = REPO_ROOT / "client" / "notices.mjs"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is needed to run the notices module")

HARNESS = """
import { settledNotices } from %(url)s;
const describe = (t) => `Rs ${t.amount} to ${t.payee_id}`;
const out = %(cases)s.map(([seen, transfers, started]) =>
  settledNotices(seen, transfers, describe, started ?? undefined));
process.stdout.write(JSON.stringify(out));
"""


def _transfer(status: str, reason: str | None = None, transfer_id: str = "t1") -> dict[str, Any]:
    return {
        "transfer_id": transfer_id,
        "status": status,
        "reason": reason,
        "amount": 500,
        "payee_id": "Maa",
    }


Case = (
    tuple[dict[str, str] | None, list[dict[str, Any]]]
    | tuple[dict[str, str] | None, list[dict[str, Any]], dict[str, str]]
)


def _run(*cases: Case) -> list[dict[str, Any]]:
    harness = HARNESS % {"url": json.dumps(NOTICES.as_uri()), "cases": json.dumps(cases)}
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", harness],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def test_an_analyst_release_is_announced_as_approved() -> None:
    (result,) = _run(({"t1": "under_review"}, [_transfer("released")]))
    (notice,) = result["notices"]
    assert (notice["transferId"], notice["level"], notice["title"]) == (
        "t1",
        "good",
        "Transfer approved",
    )
    assert "Rs 500 to Maa" in notice["text"]
    assert result["seen"] == {"t1": "released"}


def test_an_analyst_denial_is_announced_as_declined() -> None:
    (result,) = _run(({"t1": "under_review"}, [_transfer("cancelled", "denied")]))
    (notice,) = result["notices"]
    assert (notice["level"], notice["title"]) == ("critical", "Transfer declined")
    assert "Nothing was debited" in notice["text"]


def test_a_timed_out_hold_says_why() -> None:
    reviewed, verified = _run(
        ({"t1": "under_review"}, [_transfer("cancelled", "timeout")]),
        ({"t1": "awaiting_step_up"}, [_transfer("cancelled", "timeout")]),
    )
    assert "reviewed in time" in reviewed["notices"][0]["text"]
    assert "verified in time" in verified["notices"][0]["text"]


def test_nothing_is_announced_without_a_change_from_open() -> None:
    first_look, still_open, already_final, new_and_final = _run(
        (None, [_transfer("released")]),
        ({"t1": "under_review"}, [_transfer("under_review")]),
        ({"t1": "released"}, [_transfer("released")]),
        ({}, [_transfer("cancelled", "denied")]),
    )
    for result in (first_look, still_open, already_final, new_and_final):
        assert result["notices"] == []
    assert first_look["seen"] == {"t1": "released"}


def test_each_settled_transfer_gets_its_own_notice() -> None:
    (result,) = _run(
        (
            {"a": "under_review", "b": "under_review", "c": "under_review"},
            [
                _transfer("released", transfer_id="a"),
                _transfer("cancelled", "denied", "b"),
                _transfer("under_review", transfer_id="c"),
            ],
        )
    )
    assert [n["transferId"] for n in result["notices"]] == ["a", "b"]


def test_a_hold_not_yet_in_the_statement_is_watched_until_it_settles() -> None:
    # Right after confirming: the workflow has not written the hold to the ledger yet.
    (just_confirmed,) = _run((None, [], {"t1": "under_review"}))
    assert just_confirmed["notices"] == []
    assert just_confirmed["seen"] == {"t1": "under_review"}

    # The analyst released it before the statement was ever read with it listed.
    (released,) = _run(({}, [_transfer("released")], {"t1": "under_review"}))
    assert [n["title"] for n in released["notices"]] == ["Transfer approved"]
    assert released["seen"] == {"t1": "released"}
