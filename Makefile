SHELL := /bin/sh

PYTHON := .venv/bin/python
PIP := $(PYTHON) -m pip
AGENT := agent
CONFIG ?= config/config.toml
BT_ADDRESS ?=
BT_AUDIO_TEST_ARGS ?= list

.PHONY: venv install test lint format run doctor clean \
	bluetooth-setup bluetooth-check bluetooth-audio-test

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

# Bluetooth HFP is a user-level alternative to the UAC gadget. Do not use sudo.
# For audio-test arguments: make bluetooth-audio-test BT_AUDIO_TEST_ARGS="record 10"
bluetooth-setup:
	"./scripts/bluetooth-setup.sh"

bluetooth-check:
	BT_ADDRESS="$(BT_ADDRESS)" "./scripts/bluetooth-check.sh"

bluetooth-audio-test:
	BT_ADDRESS="$(BT_ADDRESS)" "./scripts/bluetooth-audio-test.sh" $(BT_AUDIO_TEST_ARGS)

clean:
	find agent -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache agent/.pytest_cache agent/.ruff_cache \
		agent/build agent/dist agent/*.egg-info
