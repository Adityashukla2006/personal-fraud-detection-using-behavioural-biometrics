"""Tests for staging Lambda deployment directories."""

from __future__ import annotations

from pathlib import Path

import stage_functions


def test_every_handler_package_is_discovered() -> None:
    expected = {"scoring", "ledger", "transfers", "adaptation", "archive"}
    assert expected <= set(stage_functions.functions())


def test_a_stage_holds_the_handler_package_and_fraudcore_only(tmp_path: Path) -> None:
    staged = stage_functions.stage(output=tmp_path)
    scoring = tmp_path / "scoring"

    assert scoring in staged
    assert sorted(path.name for path in scoring.iterdir()) == ["fraudcore", "scoring"]
    assert (scoring / "scoring" / "handler.py").is_file()
    # models.json is data the handler loads at cold start, so it must travel with fraudcore.
    assert (scoring / "fraudcore" / "models.json").is_file()
    assert not list(scoring.rglob("__pycache__"))


def test_restaging_replaces_stale_files(tmp_path: Path) -> None:
    stage_functions.stage(output=tmp_path)
    stale = tmp_path / "scoring" / "scoring" / "stale.py"
    stale.write_text("", encoding="utf-8")
    stage_functions.stage(output=tmp_path)
    assert not stale.exists()
