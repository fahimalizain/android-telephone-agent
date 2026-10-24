# Documentation

This index lists the canonical documentation for android-telephone-agent. The
[design brief](design-brief.md) is the source of truth for scope, constraints, and host
requirements. Other documents expand it; where they disagree, the brief wins.

The project is at alpha, design stage. Some documents describe a target design rather
than verified behavior. Device-specific claims are marked "verify on your device".

## Reading paths

- New to the project: [design brief](design-brief.md), then [architecture](architecture.md),
  then [Bluetooth HFP](bluetooth-hfp.md).
- Building the setup: [Bluetooth HFP](bluetooth-hfp.md) for the host and audio path,
  [Android setup](android-setup.md) for the phone, then
  [dial helper](dial-helper.md) and [voice agent](voice-agent.md).
- Running the agent: [voice agent](voice-agent.md), [operations](operations.md),
  [runbook](runbook.md), then [troubleshooting](troubleshooting.md).
- Answering calls: [Inbound calls](inbound.md).
- Integrating another agent or LLM host: [interoperability](interop.md), then
  [ADR 0007](decisions/0007-expose-agent-over-mcp-and-a2a.md).
- Reference: [latency budget](latency-budget.md), [prompts](prompts.md), [FAQ](faq.md),
  [glossary](glossary.md), and the [decision records](decisions/).

## Documents

| Document | What it covers |
|---|---|
| [Design brief](design-brief.md) | Authoritative scope, constraints, topology, host requirements, agent pipeline, operations, and security. |
| [Architecture](architecture.md) | How the parts fit together: process boundaries, data flow, call control, and failure handling. |
| [Bluetooth HFP](bluetooth-hfp.md) | Primary host and audio guide: WirePlumber roles, pairing, SCO nodes, audio test, agent configuration, and user-session deployment. |
| [Inbound calls](inbound.md) | Helper `/state` and `/answer`, the daemon's inbound watcher, auto-answer privacy limits, systemd user units, and inbound transcripts. |
| [Android setup](android-setup.md) | Phone A configuration for unattended calls, SIM selection, Bluetooth HFP routing, and helper provisioning over the LAN. |
| [Dial helper](dial-helper.md) | Reference helper app, its LAN API, bearer-token authentication, and call-control limits. |
| [Voice agent](voice-agent.md) | SCO capture, voice activity detection, STT, LLM, TTS, playback, barge-in, and timeouts, plus the realtime speech-to-speech alternative. |
| [Interoperability](interop.md) | MCP tools and A2A tasks for delegating outbound calls, mid-call consultation, security, and limits. |
| [Latency budget](latency-budget.md) | Per-stage latency targets, measurement method, and the end-to-end budget. |
| [Prompts](prompts.md) | Prompt structure, persona, call flow, and conversational guardrails. |
| [Operations](operations.md) | Scheduling, health checks, alerting, logging, maintenance, and recovery. |
| [Runbook](runbook.md) | Incident procedures with symptom, impact, diagnostics, fix, and prevention. |
| [Troubleshooting](troubleshooting.md) | Symptom, likely cause, and fix for common Bluetooth, helper, and runtime failures. |
| [FAQ](faq.md) | Short answers to recurring questions about scope, the host, and behavior. |
| [Glossary](glossary.md) | Definitions of telephony, audio, Android, and project-specific terms. |
| [ADR-0001: USB audio gadget](decisions/0001-use-usb-audio-gadget-instead-of-root.md) | Historical: use the Pi as a USB audio gadget instead of rooting the phone. Superseded by ADR 0009. |
| [ADR-0002: Power through a PD hub](decisions/0002-power-phone-and-gadget-through-pd-hub.md) | Historical: power Phone A and the Pi through one USB-C PD hub. Superseded by ADR 0009. |
| [ADR-0003: Pi runtime](decisions/0003-run-on-pi-zero-2w.md) | Historical: use the earlier Pi as the reference runtime. Superseded by ADR 0009. |
| [ADR-0004: Cloud voice stack](decisions/0004-cloud-voice-stack-first.md) | Use cloud STT, LLM, and TTS providers before local speech models. |
| [ADR-0005: Real SIM origination](decisions/0005-originate-from-real-sim.md) | Originate calls from a real SIM rather than a VoIP service. |
| [ADR-0006: Bluetooth HFP audio](decisions/0006-use-bluetooth-hfp-audio.md) | Introduce Bluetooth HFP; now the only supported audio transport. |
| [ADR-0007: MCP and A2A control plane](decisions/0007-expose-agent-over-mcp-and-a2a.md) | Expose the call agent over MCP and A2A through one shared CallService. |
| [ADR-0008: Grok Voice realtime adapter](decisions/0008-use-grok-voice-realtime-adapter.md) | Add xAI speech-to-speech as an independent full-duplex adapter beside the text pipeline. |
| [ADR-0009: Bluetooth-only Linux host](decisions/0009-bluetooth-only-linux-host.md) | Remove the earlier hardware route; Bluetooth HFP is the only audio transport. |

## Related documents

- [Project README](../README.md)
- [Contributing guide](../CONTRIBUTING.md)
- [Security policy](../SECURITY.md)
- [Changelog](../CHANGELOG.md)
