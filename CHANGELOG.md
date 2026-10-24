# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The project is at alpha, design stage; minor versions may change APIs and file layout.

## [Unreleased]

### Added

- Bluetooth HFP alternative to the USB gadget: the Linux host acts as the
  Hands-Free Profile headset (HF) and Phone A as the audio gateway (AG).
- `PipeWireAudio` runtime backend using `pw-record`/`pw-play` and SCO node
  discovery for both call directions; the USB gadget backend remains the
  default.
- `audio.backend`, `audio.bluetooth_address`, and `audio.node_timeout_s`
  configuration, with Bluetooth examples and validation.
- Bluetooth HFP setup, check, and audio-test scripts plus an ADR and setup guide.
- systemd user unit and timer examples for the Bluetooth backend; the existing
  system unit remains for the USB gadget path.
- Tests for SCO node discovery, Bluetooth configuration validation, and CLI
  backend wiring.

## [0.1.0] - 2026-10-24

### Added

- Initial public design release.
- Design brief covering scope, constraints, topology, canonical hardware, power,
  USB gadget behavior, dialing, call control, the agent pipeline, operations, and
  security.
- Architecture, hardware, bill of materials, power and thermal, Raspberry Pi setup,
  Android setup, dial helper, voice agent, latency budget, prompts, operations, runbook,
  troubleshooting, FAQ, and glossary documents.
- Architecture decision records for the first five design decisions.
- Reference Python runtime scaffold using the standard library, with development
  dependencies limited to `ruff` and `pytest`.
- Reference Android helper app for placing calls over the LAN with a bearer token.
- USB gadget, installation, audio loopback, and dial scripts plus systemd units.
- Example configuration and environment files containing placeholders only.
- Project policies: contributing guide, code of conduct, security policy, issue and
  pull request templates, and Markdown linting in CI.
