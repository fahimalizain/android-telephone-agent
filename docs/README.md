# Documentation

This index lists the canonical documentation for android-telephone-agent. The
[design brief](design-brief.md) is the source of truth for scope, constraints, and
hardware. Other documents expand it; where they disagree, the brief wins.

The project is at alpha, design stage. Some documents describe a target design rather
than verified behavior. Hardware claims are marked "verify on your device" unless the
USB or Android specification enforces them.

## Reading paths

- New to the project: [design brief](design-brief.md), then [architecture](architecture.md),
  then [hardware](hardware.md).
- Building the device: [hardware](hardware.md), [power and thermal](power-and-thermal.md),
  [Raspberry Pi setup](raspberry-pi-setup.md), [Android setup](android-setup.md), then
  [dial helper](dial-helper.md). For the Bluetooth alternative, read
  [Bluetooth HFP](bluetooth-hfp.md) after the Android setup.
- Running the agent: [voice agent](voice-agent.md), [operations](operations.md),
  [runbook](runbook.md), then [troubleshooting](troubleshooting.md).
- Reference: [latency budget](latency-budget.md), [prompts](prompts.md), [FAQ](faq.md),
  [glossary](glossary.md), and the [decision records](decisions/).

## Documents

| Document | What it covers |
|---|---|
| [Design brief](design-brief.md) | Authoritative scope, constraints, topology, canonical hardware, agent pipeline, operations, and security. |
| [Architecture](architecture.md) | How the parts fit together: process boundaries, data flow, call control, and failure handling. |
| [Hardware](hardware.md) | Canonical parts, supported alternatives, assembly, and checks to run before buying. |
| [Bill of materials](bom.csv) | Machine-readable BOM with quantities, notes, and indicative INR prices. |
| [Power and thermal](power-and-thermal.md) | Power topology, UPS coverage, phone battery care, and thermal limits. |
| [Raspberry Pi setup](raspberry-pi-setup.md) | OS image, boot configuration, USB gadget setup, and service installation. |
| [Bluetooth HFP](bluetooth-hfp.md) | Alternative audio transport: WirePlumber roles, pairing, SCO nodes, audio test, agent configuration, and user-session deployment. |
| [Android setup](android-setup.md) | Phone A configuration for unattended calls, SIM selection, and USB audio routing. |
| [Dial helper](dial-helper.md) | Reference helper app, its LAN API, bearer-token authentication, and call-control limits. |
| [Voice agent](voice-agent.md) | Capture, voice activity detection, STT, LLM, TTS, playback, barge-in, and timeouts. |
| [Latency budget](latency-budget.md) | Per-stage latency targets, measurement method, and the end-to-end budget. |
| [Prompts](prompts.md) | Prompt structure, persona, call flow, and conversational guardrails. |
| [Operations](operations.md) | Scheduling, health checks, alerting, logging, maintenance, and recovery. |
| [Runbook](runbook.md) | Incident procedures with symptom, impact, diagnostics, fix, and prevention. |
| [Troubleshooting](troubleshooting.md) | Symptom, likely cause, and fix for common hardware and software failures. |
| [FAQ](faq.md) | Short answers to recurring questions about scope, hardware, and behavior. |
| [Glossary](glossary.md) | Definitions of telephony, audio, Android, and project-specific terms. |
| [ADR-0001: USB audio gadget](decisions/0001-use-usb-audio-gadget-instead-of-root.md) | Use the Pi as a USB audio gadget instead of rooting the phone. |
| [ADR-0002: Power through a PD hub](decisions/0002-power-phone-and-gadget-through-pd-hub.md) | Power Phone A and the Pi through one USB-C PD hub. |
| [ADR-0003: Pi Zero 2 W](decisions/0003-run-on-pi-zero-2w.md) | Use the Raspberry Pi Zero 2 W as the reference runtime. |
| [ADR-0004: Cloud voice stack](decisions/0004-cloud-voice-stack-first.md) | Use cloud STT, LLM, and TTS providers before local speech models. |
| [ADR-0005: Real SIM origination](decisions/0005-originate-from-real-sim.md) | Originate calls from a real SIM rather than a VoIP service. |
| [ADR-0006: Bluetooth HFP audio](decisions/0006-use-bluetooth-hfp-audio.md) | Support Bluetooth HFP as an alternative audio transport while keeping USB as the default. |

## Related documents

- [Project README](../README.md)
- [Contributing guide](../CONTRIBUTING.md)
- [Security policy](../SECURITY.md)
- [Changelog](../CHANGELOG.md)
