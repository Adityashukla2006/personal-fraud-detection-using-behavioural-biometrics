"""Stage each Lambda function's deployment directory under build/lambdas/<name>/.

A function is any package under src/lambdas with a handler.py. Its stage holds that package and
fraudcore, which is everything a handler may import besides the standard library and boto3.
Terraform zips the staged directories with archive_file; nothing is built inside Terraform.
"""

from __future__ import annotations

import shutil
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = SRC.parent / "build" / "lambdas"
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


def functions(src: Path = SRC) -> list[str]:
    return sorted(path.parent.name for path in (src / "lambdas").glob("*/handler.py"))


def stage(output: Path = DEFAULT_OUTPUT, src: Path = SRC) -> list[Path]:
    staged = []
    for name in functions(src):
        target = output / name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(src / "lambdas" / name, target / name, ignore=_IGNORE)
        shutil.copytree(src / "fraudcore", target / "fraudcore", ignore=_IGNORE)
        staged.append(target)
    return staged


if __name__ == "__main__":
    for path in stage():
        print(path)
