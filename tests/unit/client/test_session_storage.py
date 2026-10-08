"""The browser keeps a sign-in across a refresh only while its access token is still valid."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SESSION = REPO_ROOT / "client" / "session.mjs"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is needed to run the session module")

NOW_MS = 1_767_249_000_000

HARNESS = """
import * as session from %(url)s;
const data = {};
globalThis.sessionStorage = {
  getItem: (k) => data[k] ?? null,
  setItem: (k, v) => { data[k] = String(v); },
  removeItem: (k) => { delete data[k]; },
};
const token = (exp) => "h." + btoa(JSON.stringify({ sub: "u", exp })).replaceAll("=", "") + ".s";
const now = %(now)d;
const out = {};
for (const [name, exp] of %(cases)s) {
  const tokens = { AccessToken: token(exp), IdToken: "id", RefreshToken: "refresh" };
  session.saveSession("k", tokens, "a@b.c");
  const stored = JSON.parse(data.k);
  out[name] = { stored, restored: session.restoreSession("k", now), left: "k" in data };
}
session.saveSession("k", { AccessToken: token(now / 1000 + 3600) }, "a@b.c");
session.clearSession("k");
out.cleared = session.restoreSession("k", now);
data.k = "not json";
out.garbage = session.restoreSession("k", now);
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def output() -> dict[str, Any]:
    now_s = NOW_MS // 1000
    margin_s = 60
    cases = [
        ["fresh", now_s + 3600],
        ["at_margin", now_s + margin_s],
        ["past_margin", now_s + margin_s + 1],
        ["expired", now_s - 1],
    ]
    harness = HARNESS % {
        "url": json.dumps(SESSION.as_uri()),
        "now": NOW_MS,
        "cases": json.dumps(cases),
    }
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", harness],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def test_only_the_access_token_is_stored(output: dict[str, Any]) -> None:
    stored = output["fresh"]["stored"]
    assert set(stored["tokens"]) == {"AccessToken"}
    assert stored["email"] == "a@b.c"


def test_a_valid_token_survives_a_refresh(output: dict[str, Any]) -> None:
    assert output["fresh"]["restored"] == output["fresh"]["stored"]
    assert output["past_margin"]["restored"] is not None


def test_a_token_at_or_past_the_margin_signs_out_and_is_forgotten(output: dict[str, Any]) -> None:
    for case in ("at_margin", "expired"):
        assert output[case]["restored"] is None
        assert output[case]["left"] is False


def test_signing_out_and_unreadable_storage_restore_nothing(output: dict[str, Any]) -> None:
    assert output["cleared"] is None
    assert output["garbage"] is None
