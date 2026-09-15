"""Fixtures shared by every integration test against the deployed dev stack.

Names, ARNs and endpoints come from ``terraform output -json`` and nothing else, so a test can never
pass against a resource the stack does not actually own. Test data is keyed with ``TEST_PREFIX`` so
cleanup is unambiguous.
"""

from __future__ import annotations

import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

import boto3
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEV_ENV = REPO_ROOT / "infra" / "envs" / "dev"
TEST_PREFIX = "test-"


@pytest.fixture(scope="session")
def outputs() -> dict[str, Any]:
    # TERRAFORM overrides the binary for machines where it is not on PATH.
    terraform = os.environ.get("TERRAFORM", "terraform")
    result = subprocess.run(
        [terraform, f"-chdir={DEV_ENV}", "output", "-json"],
        capture_output=True,
        text=True,
        check=True,
    )
    return {name: entry["value"] for name, entry in json.loads(result.stdout).items()}


@pytest.fixture(scope="session")
def aws(outputs: dict[str, Any]) -> boto3.Session:
    # Credentials come from AWS_PROFILE, which the Makefile exports.
    return boto3.Session(region_name=outputs["region"])


@pytest.fixture
def test_key() -> str:
    return f"{TEST_PREFIX}{uuid.uuid4().hex}"
