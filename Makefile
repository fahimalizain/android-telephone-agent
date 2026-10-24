SHELL := /bin/sh

PYTHON := .venv/bin/python
PIP := $(PYTHON) -m pip
AGENT := agent
CONFIG ?= config/config.toml

.PHONY: venv install test lint format run doctor clean

venv:
	@test -x $(PYTHON) || python3 -m venv .venv

install: venv
	$(PIP) install --upgrade pip
	cd $(AGENT) && ../$(PYTHON) -m pip install -e ".[dev]"

test:
	$(PYTHON) -m pytest agent/tests -q

lint:
	$(PYTHON) -m ruff check $(AGENT)
	$(PYTHON) -m ruff format --check $(AGENT)

format:
	$(PYTHON) -m ruff check --fix $(AGENT)
	$(PYTHON) -m ruff format $(AGENT)

run:
	cd $(AGENT) && ../$(PYTHON) -m telephone_agent --config "$(abspath $(CONFIG))" run

doctor:
	cd $(AGENT) && ../$(PYTHON) -m telephone_agent --config "$(abspath $(CONFIG))" doctor

clean:
	find agent -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache agent/.pytest_cache agent/.ruff_cache \
		agent/build agent/dist agent/*.egg-info
