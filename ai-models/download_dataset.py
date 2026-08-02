"""Download and verify the CMU keystroke dynamics benchmark.

The dataset is not committed to the repository (see .gitignore), so this script fetches it into
``data/raw/`` and proves it arrived intact before anything downstream is allowed to use it.

Verification is not optional here. A partial download of this file still parses cleanly as CSV and
still looks like a plausible dataset -- it simply contains fewer subjects. Silently training on a
truncated benchmark would produce error rates that are wrong in a way no later step would catch, so
every downloaded copy is checked against a known size, digest and shape.

Usage, from the repository root::

    python ai-models/download_dataset.py

Dataset citation:
    Killourhy, K. S. and Maxion, R. A. (2009). Comparing Anomaly-Detection Algorithms for
    Keystroke Dynamics. DSN 2009.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

URL = "https://www.cs.cmu.edu/~keystroke/DSL-StrongPasswordData.csv"

# Verified 2026-08-01 against the copy served by cs.cmu.edu.
EXPECTED_BYTES = 4_669_935
EXPECTED_SHA256 = "b11d23538b1865fa6ecf4e8b78567caa312e9c1027604bb022fcc6ad7eaa7a33"

# Shape the benchmark is documented to have: 51 subjects x 8 sessions x 50 repetitions.
EXPECTED_SUBJECTS = 51
EXPECTED_ROWS = 20_400
EXPECTED_TIMING_COLUMNS = 31

REPO_ROOT = Path(__file__).resolve().parent.parent
DESTINATION = REPO_ROOT / "data" / "raw" / "DSL-StrongPasswordData.csv"


def sha256_of(path: Path) -> str:
    """Return the hex SHA-256 digest of a file, read in chunks so memory stays flat."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_bytes(path: Path) -> None:
    """Fail unless the file matches the expected byte count and digest.

    Size is checked first purely because it gives a far more legible error on the common failure,
    which is a connection dropping partway through.
    """
    actual_bytes = path.stat().st_size
    if actual_bytes != EXPECTED_BYTES:
        raise ValueError(
            f"size mismatch: got {actual_bytes:,} bytes, expected {EXPECTED_BYTES:,}. "
            "The download was truncated -- delete the file and run this script again."
        )

    actual_sha = sha256_of(path)
    if actual_sha != EXPECTED_SHA256:
        raise ValueError(
            f"checksum mismatch:\n  got      {actual_sha}\n  expected {EXPECTED_SHA256}\n"
            "The file is the right length but the wrong content."
        )


def check_shape(path: Path) -> None:
    """Fail unless the parsed CSV has the documented subjects, rows and timing columns."""
    import pandas as pd

    frame = pd.read_csv(path)

    subjects = frame["subject"].nunique()
    if subjects != EXPECTED_SUBJECTS:
        raise ValueError(f"expected {EXPECTED_SUBJECTS} subjects, found {subjects}")

    if len(frame) != EXPECTED_ROWS:
        raise ValueError(f"expected {EXPECTED_ROWS:,} rows, found {len(frame):,}")

    timing_columns = [c for c in frame.columns if c.startswith(("H.", "DD.", "UD."))]
    if len(timing_columns) != EXPECTED_TIMING_COLUMNS:
        raise ValueError(
            f"expected {EXPECTED_TIMING_COLUMNS} timing columns, found {len(timing_columns)}"
        )

    missing = int(frame.isnull().sum().sum())
    if missing:
        raise ValueError(f"expected no missing values, found {missing}")

    reps = frame.groupby("subject").size().unique()
    if len(reps) != 1 or reps[0] != 400:
        raise ValueError(f"expected 400 repetitions for every subject, found counts {list(reps)}")


def download(url: str, destination: Path) -> None:
    """Download to a temporary file and move it into place only once it is complete.

    Downloading via a temporary file means an interrupted run can never leave a half-written file
    at the destination path, where a later run would mistake it for a usable dataset.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)

    with urllib.request.urlopen(url, timeout=120) as response:
        if response.status != 200:
            raise RuntimeError(f"server returned HTTP {response.status}")

        with tempfile.NamedTemporaryFile(delete=False, dir=destination.parent) as temporary:
            temporary_path = Path(temporary.name)
            shutil.copyfileobj(response, temporary)

    try:
        check_bytes(temporary_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    temporary_path.replace(destination)


def main() -> int:
    if DESTINATION.exists():
        print(f"Found existing copy at {DESTINATION.relative_to(REPO_ROOT)}, verifying...")
        try:
            check_bytes(DESTINATION)
            check_shape(DESTINATION)
        except ValueError as error:
            print(f"Existing copy is invalid: {error}", file=sys.stderr)
            return 1
        print("Existing copy is valid. Nothing to do.")
        return 0

    print(f"Downloading {URL}")
    try:
        download(URL, DESTINATION)
        check_shape(DESTINATION)
    except Exception as error:
        print(f"Download failed: {error}", file=sys.stderr)
        return 1

    print(
        f"Saved to {DESTINATION.relative_to(REPO_ROOT)}\n"
        f"  {EXPECTED_ROWS:,} rows, {EXPECTED_SUBJECTS} subjects, "
        f"{EXPECTED_TIMING_COLUMNS} timing columns -- verified."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
