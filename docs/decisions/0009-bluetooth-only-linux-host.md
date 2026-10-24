# ADR 0009: Use a Bluetooth-only Linux host

- Status: Accepted
- Date: 2026-10-24
- Deciders: Project owner

## Context

The original design made a Raspberry Pi Zero 2 W a USB Audio Class 1 gadget that appeared
to Phone A as a wired headset. It required USB device mode, a data-capable phone port, a
powered hub, and a separate power arrangement for the Pi. The path was never validated on
hardware, and the project accumulated hardware, BOM, power, thermal, and Pi setup
documentation for a build that was not working end to end.

On 2026-10-24 the Bluetooth HFP path was validated end to end on a Linux laptop with a
Samsung Galaxy A23 4G over outbound PSTN calls. The host acted as the HFP hands-free unit
(HF), Phone A as the audio gateway (AG), and PipeWire exposed the SCO source and sink.
mSBC carried 16 kHz audio in both directions, the LAN helper placed and ended calls, and
the Grok Voice realtime adapter held a five-turn conversation. The link was weak (RSSI
about -87 dBm); the first call dropped during setup and needed a manual reconnect, and
later calls were stable.

The validated host was a laptop, not a Raspberry Pi. Bluetooth does not need USB device
mode, a hub, or a Pi-specific power topology, and the phone can charge from its own
charger because the USB port is free. Keeping two audio routes also doubles the setup,
test, and troubleshooting surface for no validated benefit.

## Decision

The project supports one deployment: a Linux host plus an Android phone over Bluetooth
HFP.

- The host runs BlueZ, PipeWire, and WirePlumber in the same user session as the agent.
- The host advertises the HFP **hands-free unit (HF)** role; Phone A is the **audio
  gateway (AG)**. The WirePlumber fragment in
  [`config/wireplumber/51-bt-headset.conf`](../../config/wireplumber/51-bt-headset.conf)
  enables the role.
- The agent uses `[audio] backend = "pipewire"`, discovers the call-scoped SCO source and
  sink by Bluetooth address and factory name, and resamples between the SCO rate and the
  pipeline rate.
- Dialing, answering, and hangup go through the LAN helper app on Phone A. HFP AT call
  control is not implemented.
- Outbound and inbound work run as systemd **user** units so they share the PipeWire and
  WirePlumber session. Lingering is required on a headless host.
- The Raspberry Pi and USB Audio Class gadget route is removed, together with its scripts,
  system units, hardware guide, bill of materials, and power/thermal documentation.

## Consequences

### Positive

- One validated audio transport and one setup path, with no USB device mode, hub, or Pi
  power topology.
- A laptop, desktop, or mini PC can run the agent; the host does not need a USB device
  controller.
- The phone's USB port stays free for charging, and the phone does not have to act as a USB
  host for audio.
- PipeWire exposes both SCO directions, so capture and playback are inspectable with
  standard tools and the Bluetooth scripts.
- Fewer hardware variables to document, buy, power, cool, and troubleshoot.

### Negative

- The host must run the agent in the user's PipeWire/WirePlumber session. A root system
  service or a different account cannot see the SCO nodes.
- Bluetooth audio exists only after a call is answered. There is no ringing signal or
  caller ID in user space, so inbound answering depends on the LAN helper.
- Pairing, reconnection, codec negotiation, and 2.4 GHz coexistence are additional failure
  modes compared with a cable.
- The host needs a working Bluetooth adapter, and mSBC is not guaranteed; the link can
  fall back to CVSD at 8 kHz.
- One host supports one phone and one call at a time.

### Risks

- **Weak link during setup.** The validated run at about -87 dBm dropped the first call and
  needed a manual reconnect. Treat a setup drop as a known failure mode and verify the link
  before a scheduled call.
- **Inbound is not live-validated.** The inbound watcher and helper endpoints are
  implemented and unit-tested, but no live inbound PSTN call has been exercised. Bluetooth
  cannot signal a ringing call, so the helper must answer before SCO nodes exist.
- **User-session coupling.** Without lingering, the user manager and PipeWire stop when the
  last session ends, and timers or SCO nodes disappear.
- **Host hardware varies.** Bluetooth adapter behavior, codec support, and Wi-Fi
  coexistence differ by machine. Verify adapter roles and SCO audio on the host you deploy.
- **No Pi support.** The Raspberry Pi route is removed, not merely deprecated. A Pi is not
  a supported host, and the project does not provide Pi hardware, BOM, or power guidance.

## Alternatives considered

### Keep the USB gadget as the canonical path

The USB route avoided pairing and codec negotiation but required USB device mode, a hub,
and a power topology, and it was never validated. Keeping it would preserve a second
untested path and the hardware documentation that supported it. Rejected.

### Support both USB and Bluetooth

Running both paths keeps the larger test matrix, the hub and power failure modes, and the
Pi-specific scripts. It also makes it unclear which path a bug report describes. Rejected
in favor of one validated transport.

### Use a Raspberry Pi as the Bluetooth host

A Pi could run BlueZ and PipeWire, but the validated host is a laptop, the Pi has a single
shared 2.4 GHz radio, and the project no longer supports Pi hardware. Reusing the Pi would
retain hardware and power documentation for an unverified configuration. Rejected.

### Acoustic coupling

Holding the phone near a speaker and microphone avoids Bluetooth and USB, but levels
drift, room noise enters both directions, and the phone's echo cancellation interferes.
Rejected as the least deterministic option.

## References

- [Bluetooth HFP guide](../bluetooth-hfp.md).
- [Architecture](../architecture.md), sections 4, 7, and 11.
- [Design brief](../design-brief.md), sections 4 through 7.
- [ADR 0006: Bluetooth HFP audio](./0006-use-bluetooth-hfp-audio.md).
- [ADR 0001: USB audio gadget](./0001-use-usb-audio-gadget-instead-of-root.md) (superseded).
- [Operations](../operations.md) and [Inbound calls](../inbound.md).
- [Glossary](../glossary.md).
