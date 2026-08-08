"""Pytest configuration for the offline track.

``ai-models`` contains a hyphen, so it cannot be imported as a Python package. The modules in it are
imported by name instead, which requires the folder itself to be on ``sys.path``. Doing it here
means the tests run from the repository root with a bare ``pytest`` and no PYTHONPATH juggling.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
