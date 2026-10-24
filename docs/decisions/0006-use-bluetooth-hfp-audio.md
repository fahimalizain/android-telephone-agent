# ADR 0006: Use Bluetooth HFP as the audio transport

- Status: Accepted; Bluetooth HFP is now the only supported audio transport
- Date: 2026-10-24
- Deciders: Project owner

> **Update (2026-10-24).** [ADR 0009](./0009-bluetooth-only-linux-host.md) removed the
> earlier Pi-based and USB Audio Class route. Bluetooth HFP is now the only supported
> transport, not an alternative. The context and validation notes below are kept from the
> original decision, with a later validation note.

## Context

The earlier design made a small board a USB Audio Class 1 device. Phone A enumerated it
as a wired headset and routed call audio to it. That path needed USB device mode, a
data-capable port, and a powered hub, and it was never validated on hardware.

Bluetooth Hands-Free Profile (HFP) is the standard profile Android already uses for car
kits and headsets. The phone is the audio gateway (AG); the Linux host is the hands-free
unit (HF). The phone routes call audio to the HF over a synchronous SCO link, and the host
sees the downlink and uplink as PipeWire nodes. No root, vendor API, or modem access is
involved.

The original validation used a Linux laptop (MSI GL738SE) running Ubuntu with BlueZ 5.85,
PipeWire 1.6.2, and WirePlumber 0.5.13, paired with a Samsung Galaxy A23. After restarting
WirePlumber, the local adapter advertised the Headset (UUID 0x1108) and Handsfree (UUID
0x111e) services, and the phone showed **Calls and audio** enabled. The HFP service-level
connection was confirmed in WirePlumber debug logs, including `/Profile/HFPHF`,
`AT+CLIP`, `AT+CCWA`, `AT+CMEE`, `AT+CLCC`, `AT+VGS`, and `AT+VGM`, plus the AG indicators
for call, call setup, service, signal, and battery.

During an active Google Meet call, audio was bidirectional through the laptop's built-in
speaker and microphone. The SCO codec negotiated was mSBC at 16 kHz, with
`api.bluez5.profile = "headset-audio-gateway"` and `media.role = Communication`.
`pw-record` captured the SCO source, and `pw-play` injected a test tone into the SCO sink.
The far end of the Meet call was confirmed to hear the host audio.

A later validation on 2026-10-24 used outbound PSTN calls through the LAN helper. Audio
ran over HFP SCO at mSBC 16 kHz in both directions, and the Grok Voice realtime adapter
held a real conversation: five turns in 68.1 s, with the model requesting `end_call` and
the helper accepting `/hangup` with `202`. The link was weak (RSSI about -87 dBm); the
first call dropped during setup and needed a manual reconnect, and later calls were
stable.

Inbound was validated separately, once, on 2026-10-24, using the helper rather than HFP
call control: the helper answered a ringing PSTN call after the watcher's 1 s delay, audio
ran over the same HFP SCO path at mSBC 16 kHz, and the Grok Voice session ran four turns in
39.0 s before a model-requested `/hangup` returned the phone to idle.

What was **not** validated: end-to-end latency, HFP call control from Linux, and
locked-screen answering or vendor restrictions on `acceptRingingCall()`. Dialing and
hangup still go through the LAN helper app. Bluetooth carries audio only, and SCO nodes
exist only after a call is answered.

## Decision

Bluetooth HFP is the only supported audio transport. The Linux host advertises the HF
role and Phone A is the AG. The agent uses `[audio] backend = "pipewire"` and discovers
the call-scoped SCO nodes by Bluetooth address and factory name. There is no fallback
audio backend and no USB data link.

On the host, WirePlumber advertises the HF role with this fragment:

```text
monitor.bluez.properties = {
  bluez5.roles = [ hfp_hf hsp_hs a2dp_sink ]
  bluez5.hfphsp-backend = native
  bluez5.enable-msbc = true
  bluez5.enable-hw-volume = true
}
```

Phone A is paired, trusted, and connected, with **Calls and audio** enabled for the host.
While a call is active, PipeWire creates two SCO nodes:

| Direction | PipeWire node | Factory | Media class | Agent action |
|---|---|---|---|---|
| Callee to agent (downlink) | `bluez_input.<address>.<id>` | `api.bluez5.sco.source` | `Stream/Output/Audio` | Capture with `pw-record` |
| Agent to callee (uplink) | `bluez_output.<address>.<id>` | `api.bluez5.sco.sink` | `Stream/Input/Audio` | Play with `pw-play` |

The `.<id>` suffix is a SPA object id and can change per connection. The agent discovers
nodes by `api.bluez5.address` and `factory.name`, not by a hard-coded suffix. Because the
nodes exist only while a call or SCO link is active, the backend waits up to
`node_timeout_s` (default 30 s) for them before failing.

The agent runs as the same user as PipeWire and WirePlumber. On a headless install this
normally means a systemd **user** service with lingering enabled. A system-level service
for another user cannot see the SCO nodes.

HFP call control (ATD, ATA, CHUP, and similar) is not implemented. The LAN helper app is
the only call-control mechanism: `GET /health`, `GET /state`, `POST /call`, `POST /answer`,
and `POST /hangup`. Phone A must be reachable on the LAN.

## Consequences

### Positive

- No USB data cable or USB audio device is needed; the phone's USB port stays free for
  charging.
- The host does not need USB device mode, so a laptop, desktop, or mini PC can run the
  agent.
- HFP is a standard profile, and Android already routes call audio to a Bluetooth headset.
- mSBC provides a 16 kHz wideband link, better than narrowband CVSD SCO.
- PipeWire exposes both directions as inspectable nodes, and no root or vendor API is used.
- The transport works alongside the helper app and call state machine; only the audio
  backend changes.
- The phone UI gives a direct confirmation: **Calls and audio** is enabled for the host.

### Negative

- SCO nodes exist only during a call. The agent must wait for them, and a call that ends
  removes them while capture is running.
- The agent must run in the same user session as PipeWire and WirePlumber. This is a
  deployment constraint.
- Pairing, reconnection, and profile selection are additional failure modes. A phone can
  connect but fail to enable call audio, or route to its earpiece or speaker.
- Bluetooth shares the 2.4 GHz band with Wi-Fi. Coexistence can add latency, jitter, and
  dropouts, especially on a host with a single shared radio.
- HFP call control is not implemented, so the phone must still be reachable over the LAN.
- The link is point-to-point: one host, one phone, one call at a time.
- mSBC is 16 kHz, but the cellular codec still caps end-to-end fidelity. Bluetooth does not
  make the call wideband if the network leg is narrowband.
- mSBC is not guaranteed on every phone or adapter; the link can fall back to 8 kHz CVSD.
- Inbound was validated once, through the helper rather than HFP call control. Latency has
  not been measured, and locked-screen answering remains unverified.

### Risks

- **Adapter does not advertise the headset roles.** The WirePlumber fragment may be in the
  wrong directory, may not have been loaded, or may have been overridden. Confirm the
  Headset (0x1108) and Handsfree (0x111e) UUIDs with `bluetoothctl show` after a restart.
- **Phone connects but call audio stays local.** The **Calls and audio** toggle may be off,
  or the phone may have selected speaker or earpiece. Confirm the route during a live call.
- **SCO nodes never appear.** The call may not be using the Bluetooth route, the HFP
  profile may not be connected, or the agent may be in a different user session. Use
  `pw-dump` and the Bluetooth check script before changing the agent.
- **Node suffix changes.** Scripts or configuration that hard-code `.0` and `.1` will break
  on the next connection. Discover by address and factory name.
- **PipeWire session mismatch.** A system service does not share the user's PipeWire
  session or socket environment. Run the agent as a user service in the same session, and
  enable lingering for headless operation.
- **Reconnection is not automatic.** A phone reboot, adapter reset, or WirePlumber restart
  can leave the device paired but disconnected. Trust the device and verify the connection
  before a scheduled call.
- **2.4 GHz interference.** Wi-Fi retries and SCO retransmissions can degrade audio. Keep
  the host and phone close, and prefer a clean 2.4 GHz channel.
- **Codec fallback.** A device that does not negotiate mSBC uses CVSD at 8 kHz. Do not
  assume wideband audio from the profile alone.

## Alternatives considered

### USB Audio Class route (removed)

The earlier route made the host a USB Audio Class device. It avoided Bluetooth pairing
and codec negotiation but required USB device mode, a data-capable phone port, and a
powered hub, and it was never hardware-validated for this project. It was removed on
2026-10-24; see [ADR 0009](./0009-bluetooth-only-linux-host.md).

### Linux host as HFP audio gateway, phone as HF

The roles could be reversed, with Linux as the AG and the phone as the hands-free unit.
That is not the direction Android expects for a headset, changes the phone-side UI and
routing behavior, and was not validated. It is not supported by this decision.

### Acoustic coupling

Holding the phone near a speaker and microphone avoids Bluetooth entirely, but it is the
least deterministic option: levels drift, room noise enters both directions, and the
phone's echo cancellation and noise suppression interfere. It remains rejected.

### USB Bluetooth adapter

A dedicated USB Bluetooth adapter could improve range or isolate Bluetooth from the
host's Wi-Fi radio. It adds hardware, power draw, and another failure domain, and was not
tested. An onboard adapter is sufficient for the validated setup.

### Root or private Android APIs

Root does not create a writable uplink endpoint, and private APIs vary by build. Both
remain out of scope for the same reasons as before.

## References

- [Bluetooth HFP guide](../bluetooth-hfp.md).
- [Architecture](../architecture.md), sections 4, 7, and 10.
- [ADR 0009: Bluetooth-only Linux host](./0009-bluetooth-only-linux-host.md).
- [Design brief](../design-brief.md), sections 3, 4, and 7.
- WirePlumber fragment: [`config/wireplumber/51-bt-headset.conf`](../../config/wireplumber/51-bt-headset.conf).
- Bluetooth check script: [`scripts/bluetooth-check.sh`](../../scripts/bluetooth-check.sh).
- [WirePlumber Bluetooth configuration](https://pipewire.pages.freedesktop.org/wireplumber/daemon/configuration/bluetooth.html).
- [WirePlumber debug logging](https://pipewire.pages.freedesktop.org/wireplumber/daemon/logging.html).
- [Glossary](../glossary.md).
