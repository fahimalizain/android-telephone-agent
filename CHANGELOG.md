# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
The project is at alpha; minor versions may change APIs and file layout.

## [Unreleased]

### Added

- Bluetooth HFP audio path: the Linux host acts as the Hands-Free Profile headset
  (HF) and Phone A as the audio gateway (AG).
- `PipeWireAudio` runtime backend using `pw-record`/`pw-play` and SCO node
  discovery for both call directions.
- `audio.backend`, `audio.bluetooth_address`, and `audio.node_timeout_s`
  configuration, with Bluetooth examples and validation.
- Bluetooth HFP setup, check, and audio-test scripts plus an ADR and host guide.
- systemd user units for the long-lived daemon, the Bluetooth outbound client, and the
  timer that triggers it.
- Tests for SCO node discovery, Bluetooth configuration validation, and CLI
  backend wiring.
- `CallService`, a long-lived, protocol-agnostic control plane with a FIFO task queue,
  one active call at a time, in-memory task state, a consultation broker, and an
  optional append-only JSONL audit log.
- MCP Streamable HTTP server with `place_call`, `get_call`, `answer_call`,
  `cancel_call`, and `list_calls`; `place_call` returns a task id and `queued` state
  immediately, and the destination is masked in task snapshots.
- A2A JSON-RPC adapter with a public Agent Card, task creation from a text or
  `objective`/`number` data part, mid-call questions in
  `TASK_STATE_INPUT_REQUIRED`, cooperative cancellation, and a `call-summary`
  artifact.
- Deliberative call actions (`speak`, `consult`, `hangup`) and `[brain].action_mode`
  `"text"` or `"json"`, with a configured holding phrase,
  `[agent].consult_timeout_s`, and a fallback line when a consultation times out.
- `serve` command and `[server]` configuration for two listeners in one process, a
  shared bearer token, public `/health` and Agent Card routes, loopback defaults, and
  the non-loopback `public_url` guard.
- `HttpDeliberativeBrain`, which requests one JSON action per turn from an
  OpenAI-compatible model and treats plain prose as speech.
- Tests for the call service, MCP tools, A2A executor, consultations, actions, server
  configuration, and the `serve` command.
- Interoperability guide and ADR 0007 updated to describe the implemented control
  plane, its single shared admin token, and the v1 limits: no destination allowlist,
  budgets, requester identity, rate limits, push notifications, or bundled STT/TTS.
- Independent Grok Voice realtime adapter (`telephone_agent/grok_realtime.py`) for
  xAI's full-duplex speech-to-speech WebSocket API, with server VAD, barge-in,
  PCM binary transport, `end_call`/`consult` tools, and the `run`/`serve` wiring.
- `[realtime]` configuration, a `realtime` optional dependency extra, streaming
  playback with an interrupt path in the ALSA and PipeWire audio backends, and
  `doctor` checks for the realtime key and WebSocket client. The key is read from
  `XAI_API_KEY`; no credential is stored in the repository.
- Inbound call answering: helper `GET /state` and `POST /answer`, an `inbound`
  debug command and `InboundWatcher` that answers ringing calls and runs one Grok Voice
  session, served by the long-lived daemon unit. Auto-answer has no caller-ID filter
  because the helper does not request `READ_CALL_LOG`.
- Opening greeting: the configured `[realtime].greeting` is spoken once SCO audio is
  ready.
- Opt-in local call transcripts: `[transcript]` writes one timestamped JSONL file per
  call with full conversation detail, mode `0600` in a `0700` directory under
  `runtime/transcripts` by default. Audio is not persisted.

### Changed

- one long-lived daemon for inbound and outbound; `run` submits to it by default,
  `--direct` for in-process debugging; outbound timer is a client.
- Inbound calls are live-validated once: on 2026-10-24 the helper answered a ringing PSTN
  call over Bluetooth HFP at mSBC, the Grok Voice session ran four turns in 39.0 s, and a
  model-requested `/hangup` returned the phone to idle. Locked-screen answering remains
  unverified, caller ID is still unavailable, latency is unmeasured, and HFP call control
  is not implemented.
- The supported deployment is now a Linux host plus an Android phone over Bluetooth HFP.
  Documentation, scripts, and systemd units were rewritten around BlueZ, PipeWire,
  WirePlumber, systemd user units, and the LAN helper.
- `docs/decisions/0006-use-bluetooth-hfp-audio.md` now records Bluetooth HFP as the only
  supported audio transport. ADR 0009 records the pivot from the earlier hardware route.
- Repository maps, health checks, runbooks, and issue templates now describe the Linux host
  and the Bluetooth link instead of the earlier single-board-computer and USB audio route.

### Removed

- The Raspberry Pi + USB Audio Class gadget route, including `scripts/uac-gadget.sh`,
  `scripts/install.sh`, `scripts/audio-loopback-test.sh`,
  `scripts/systemd/uac-gadget.service`, `scripts/systemd/telephone-agent.service`, and
  `scripts/systemd/telephone-agent.timer`.
- The Raspberry Pi setup, hardware, bill-of-materials, and power-and-thermal documents:
  `docs/raspberry-pi-setup.md`, `docs/hardware.md`, `docs/bom.csv`, and
  `docs/power-and-thermal.md`.
- The ALSA `GadgetAudio` backend and its USB gadget configuration path.

### Fixed

- The dial helper no longer rejects `/call` from a stale ten-minute window. It checks
  Telecom call state before rejecting and clears the window on a refused hangup when no
  call is active.
- Local call transcripts keep full details, including full E.164 numbers and verbatim
  callee and agent text, instead of masking them. Operational stdout and journald logs
  remain masked.

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
