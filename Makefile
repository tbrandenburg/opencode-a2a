.DEFAULT_GOAL := help
UV ?= uv
RUN_DIR ?= run/gateway

.PHONY: help install run stop status test lint check build conformance clean

help:
	@printf '%s\n' \
	  'make install      Install locked development dependencies' \
	  'make run          Start only the configured gateway in the background (Linux)' \
	  'make stop         Stop only the gateway started by make run' \
	  'make status       Show the managed gateway process status' \
	  'make test         Run tests and coverage' \
	  'make lint         Run the canonical lint/format/type-check pipeline' \
	  'make check        Run doctor (lint, types, tests, coverage, build, wheel smoke)' \
	  'make build        Build distributions with hash-locked build dependencies' \
	  'make conformance  Run repository-owned A2A compatibility probes' \
	  'make clean        Remove build/test caches, never runtime state or databases'

install:
	$(UV) sync --all-extras --frozen

run stop status:
	@$(UV) run --frozen python scripts/gateway_process.py $@ "$(RUN_DIR)"

test:
	$(UV) run pytest

lint:
	bash scripts/lint.sh

check:
	bash scripts/doctor.sh

build:
	$(UV) build --no-sources --force-pep517 \
	  --build-constraints build-requirements/requirements.txt --require-hashes

conformance:
	bash scripts/conformance.sh

clean:
	rm -rf build dist htmlcov .pytest_cache .mypy_cache .ruff_cache
	rm -f .coverage .coverage.json
