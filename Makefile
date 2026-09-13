# Entry points for every routine task in this repository. CLAUDE.md refers to these targets by
# name, so a target here is a contract, not a convenience.

PYTHON ?= python

# fraudcore lives under src/ and the project is deliberately not packaged, so src/ goes on the
# import path here rather than through an editable install. pyproject.toml sets the same path for
# pytest, so the two entry points agree.
export PYTHONPATH := src

.PHONY: help install dataset test test-integration lint format baseline clean

help:
	@echo "install           create .venv and install research and dev dependencies"
	@echo "dataset           download and verify the CMU benchmark"
	@echo "test              unit tests, no AWS, no network"
	@echo "test-integration  integration tests against the deployed dev stack"
	@echo "lint              ruff check"
	@echo "format            ruff format and import sort"
	@echo "baseline          reproduce the published EER baseline"
	@echo "clean             remove bytecode and pytest caches"

install:
	$(PYTHON) -m pip install -r requirements-dev.txt -r requirements-research.txt

dataset:
	$(PYTHON) research/download_dataset.py

test:
	$(PYTHON) -m pytest

test-integration:
	$(PYTHON) -m pytest tests/integration -m integration

lint:
	$(PYTHON) -m ruff check .

format:
	$(PYTHON) -m ruff check --select I --fix .
	$(PYTHON) -m ruff format .

baseline:
	$(PYTHON) -m research.baseline_eer

clean:
	$(PYTHON) -c "import pathlib, shutil; [shutil.rmtree(p) for p in pathlib.Path('.').rglob('__pycache__')]"
	$(PYTHON) -c "import pathlib, shutil; [shutil.rmtree(p) for p in pathlib.Path('.').rglob('.pytest_cache')]"
