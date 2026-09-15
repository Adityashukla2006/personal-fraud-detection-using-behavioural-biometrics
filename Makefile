# Entry points for every routine task in this repository. CLAUDE.md refers to these targets by
# name, so a target here is a contract, not a convenience.

PYTHON ?= python

# fraudcore lives under src/ and the project is deliberately not packaged, so src/ goes on the
# import path here rather than through an editable install. pyproject.toml sets the same path for
# pytest, so the two entry points agree.
export PYTHONPATH := src

AWS_PROFILE ?= bfd-admin
export AWS_PROFILE

# Override where terraform is not on PATH. Exported so integration tests call the same binary.
TERRAFORM ?= terraform
export TERRAFORM

DEV := infra/envs/dev

.PHONY: help install dataset test test-integration lint format baseline clean build latency \
	bootstrap infra-init infra-validate plan apply

help:
	@echo "install           create .venv and install research and dev dependencies"
	@echo "dataset           download and verify the CMU benchmark"
	@echo "test              unit tests, no AWS, no network"
	@echo "test-integration  integration tests against the deployed dev stack"
	@echo "lint              ruff check"
	@echo "format            ruff format and import sort"
	@echo "baseline          reproduce the published EER baseline"
	@echo "clean             remove bytecode and pytest caches"
	@echo "build             stage Lambda packages under build/lambdas for terraform"
	@echo "latency           measure warm scoring latency against the dev stack"
	@echo "bootstrap         one-time: create the terraform state bucket"
	@echo "infra-init        terraform init for envs/dev"
	@echo "infra-validate    terraform fmt check and validate"
	@echo "plan              terraform plan for envs/dev"
	@echo "apply             terraform apply for envs/dev, after reviewing plan"

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

build:
	$(PYTHON) src/lambdas/stage_functions.py

latency:
	$(PYTHON) simulator/measure_latency.py

clean:
	$(PYTHON) -c "import pathlib, shutil; [shutil.rmtree(p) for p in pathlib.Path('.').rglob('__pycache__')]"
	$(PYTHON) -c "import pathlib, shutil; [shutil.rmtree(p) for p in pathlib.Path('.').rglob('.pytest_cache')]"

bootstrap:
	terraform -chdir=infra/bootstrap init
	terraform -chdir=infra/bootstrap apply

infra-init:
	terraform -chdir=$(DEV) init

infra-validate:
	terraform fmt -check -recursive infra
	terraform -chdir=$(DEV) validate

plan: build
	$(TERRAFORM) -chdir=$(DEV) plan

apply: build
	$(TERRAFORM) -chdir=$(DEV) apply
