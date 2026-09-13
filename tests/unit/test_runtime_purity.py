"""The runtime stays pure Python.

CLAUDE.md section 2 forbids numpy, pandas and scikit-learn in anything a Lambda can import. That
is a deployment constraint, not a style preference: those wheels are tens of megabytes of compiled
extension code, and pulling one into the scoring path would force a Lambda layer, inflate cold
start, and put the offline and deployed scorers on different implementations of the same
arithmetic.

The rule is easy to break by accident and invisible until a deployment fails, so it is asserted
here rather than left to review. This test is one of the section 2 rules that section 6 requires a
guard for.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RUNTIME_ROOT = REPO_ROOT / "src"

BANNED_ROOTS = frozenset({"numpy", "pandas", "sklearn", "scipy", "matplotlib", "research"})


def _imported_roots(module: Path) -> set[str]:
    """Return the top-level package name of every import in one module."""
    tree = ast.parse(module.read_text(encoding="utf-8"), filename=str(module))
    roots: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])

    return roots


def test_runtime_imports_nothing_banned() -> None:
    offences = [
        f"{module.relative_to(REPO_ROOT).as_posix()} imports {name}"
        for module in sorted(RUNTIME_ROOT.rglob("*.py"))
        for name in sorted(_imported_roots(module) & BANNED_ROOTS)
    ]

    assert not offences, "src/ must stay pure Python:\n  " + "\n  ".join(offences)


def test_the_guard_can_actually_see_imports(tmp_path: Path) -> None:
    """A guard that never detects anything would pass forever once src/ grew a violation."""
    sample = tmp_path / "sample.py"
    sample.write_text("import numpy as np\nfrom pandas.io import x\n", encoding="utf-8")

    assert _imported_roots(sample) & BANNED_ROOTS == {"numpy", "pandas"}
