# FieldDeck developer commands.
#
# Everything here runs with no hardware attached. `make check` is exactly what
# CI runs, so a green local check means a green pull request.

VENV    ?= .venv
PY      := $(VENV)/bin/python
PIP     := $(VENV)/bin/pip
RUFF    := $(VENV)/bin/ruff
MYPY    := $(VENV)/bin/mypy
PYTEST  := $(VENV)/bin/pytest

.DEFAULT_GOAL := help
.PHONY: help venv install check lint format typecheck test test-fast coverage sim daemon ui clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

venv: ## Create the development virtualenv
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip

install: venv ## Install FieldDeck in editable mode with dev extras
	$(PIP) install -e '.[dev,compress]'

check: lint typecheck test ## Everything CI runs

lint: ## ruff check + format verification
	$(RUFF) check fielddeck tests
	$(RUFF) format --check fielddeck tests

format: ## Apply ruff formatting and safe fixes
	$(RUFF) check --fix fielddeck tests
	$(RUFF) format fielddeck tests

typecheck: ## mypy over the package
	$(MYPY)

test: ## Full test suite
	$(PYTEST)

test-fast: ## Skip anything marked slow
	$(PYTEST) -m 'not slow'

coverage: ## Test suite with a coverage report
	$(PYTEST) --cov=fielddeck --cov-report=term-missing

sim: ## Run instrumentd with the simulated bench
	FIELDDECK_SIM=1 $(PY) -m fielddeck.daemon

ui: ## Run the HMI against a running daemon
	FIELDDECK_SIM=1 $(PY) -m fielddeck.ui

clean: ## Remove caches and build artefacts
	rm -rf build dist *.egg-info .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
