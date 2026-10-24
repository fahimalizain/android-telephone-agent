# ADR 0006: Support Bluetooth HFP as an alternative audio transport

- Status: Accepted as a supported alternative; USB remains the default
- Date: 2026-10-24
- Deciders: Project owner

## Context

The canonical topology makes the Pi Zero 2 W a USB Audio Class 1 gadget. Phone A
enumerates it as a wired headset and routes call audio to it. That path is deterministic,
needs no pairing, and is the default for this project.

USB is not always available or convenient. A host that cannot act as a USB device (a
laptop or a mini PC) cannot present the gadget. A hub can be unstable, a port can
be occupied, or the phone can refuse to route call audio to a particular USB device. Some
installations also want the phone's USB port free for charging or for `adb` during
development. A second transport is useful in those cases.

Bluetooth Hands-Free Profile (HFP) is the standard profile Android already uses for car
kits and headsets. The phone is the audio gateway (AG); the Linux host is the hands-free
unit (HF). The phone routes call audio to the HF over a synchronous SCO link, and the host
sees the downlink and uplink as PipeWire nodes. No root, vendor API, or modem access is
involved.

This alternative was validated on a Linux laptop (MSI GL738SE) running Ubuntu with BlueZ
5.85, PipeWire 1.6.2, and WirePlumber 0.5.13, paired with a Samsung Galaxy A23. After
restarting WirePlumber, the local adapter advertised the Headset (UUID 0x1108) and
Handsfree (UUID 0x111e) services, and the phone showed **Calls and audio** enabled. The
HFP service-level connection was confirmed in WirePlumber debug logs, including
`/Profile/HFPHF`, `AT+CLIP`, `AT+CCWA`, `AT+CMEE`, `AT+CLCC`, `AT+VGS`, and `AT+VGM`, plus
the AG indicators for call, call setup, service, signal, and battery.

During an active Google Meet call, audio was bidirectional through the laptop's built-in
speaker and microphone. The SCO codec negotiated was mSBC at 16 kHz, with
`api.bluez5.profile = "headset-audio-gateway"` and `media.role = Communication`.
`pw-record` captured the SCO source, and `pw-play` injected a test tone into the SCO sink.
The far end of the Meet call was confirmed to hear the host audio.

What was **not** validated: a PSTN call over Bluetooth (the test call was Google Meet),
the Raspberry Pi Zero 2 W (the test host was a laptop), and HFP call control from Linux.
Dialing and hangup still go through the LAN helper app.

## Decision

Bluetooth HFP is a supported alternative audio transport. It is opt-in with
`[audio] backend = "pipewire"`; the default remains `backend = "alsa"` with the USB
gadget's `card` and `device`. The USB path, its scripts, and its documentation remain
canonical and are not removed.

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
normally means a systemd **user** service with lingering enabled; the existing system-level
one-shot unit for the USB path does not work unchanged for Bluetooth.

HFP call control (ATD, ATA, CHUP, and similar) is not implemented. The existing LAN helper
app remains the only call-control mechanism: `GET /health`, `POST /call`, `POST /hangup`.
Phone A must be reachable on the LAN whether the audio transport is USB or Bluetooth.

## Consequences

### Positive

- No USB data cable or UAC gadget is needed; the phone's USB port stays free for charging.
- The host does not need USB device mode, so a laptop or mini PC can run the agent.
- HFP is a standard profile, and Android already routes call audio to a Bluetooth headset.
- mSBC provides a 16 kHz wideband link, better than narrowband CVSD SCO.
- PipeWire exposes both directions as inspectable nodes, and no root or vendor API is used.
- The transport works alongside the existing helper app and call state machine; only the
  audio backend changes.
- The phone UI gives a direct confirmation: **Calls and audio** is enabled for the host.

### Negative

- SCO nodes exist only during a call. The agent must wait for them, and a call that ends
  removes them while capture is running.
- The agent must run in the same user session as PipeWire and WirePlumber. This is a
  deployment constraint the USB path does not have.
- Pairing, reconnection, and profile selection are additional failure modes. A phone can
  connect but fail to enable call audio, or route to its earpiece or speaker.
- Bluetooth shares the 2.4 GHz band with Wi-Fi. Coexistence can add latency, jitter, and
  dropouts, especially on a Pi Zero 2 W with a single 2.4 GHz radio.
- HFP call control is not implemented, so the phone must still be reachable over the LAN.
- The link is point-to-point: one host, one phone, one call at a time.
- mSBC is 16 kHz, but the cellular codec still caps end-to-end fidelity. Bluetooth does not
  make the call wideband if the network leg is narrowband.
- mSBC is not guaranteed on every phone or adapter; the link can fall back to 8 kHz CVSD.
- The Raspberry Pi Zero 2 W path is expected to work but is unverified. The validation used
  a laptop.
- No PSTN call has been tested over Bluetooth; only a Google Meet call and the SCO audio
  path were validated.

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

### USB Audio Class 1 gadget (canonical)

The USB path remains the default. It avoids pairing and codec negotiation, has explicit
playback and capture endpoints, and is easier to make deterministic for unattended
operation. Bluetooth is an alternative for hosts or installations where USB device mode is
unavailable or unsuitable, not a replacement.

### Linux host as HFP audio gateway, phone as HF

The roles could be reversed, with Linux as the AG and the phone as the hands-free unit.
That is not the direction Android expects for a headset, changes the phone-side UI and
routing behavior, and was not validated. It is not supported by this decision.

### Acoustic coupling

Holding the phone near a speaker and microphone avoids USB and Bluetooth entirely, but it
is the least deterministic option: levels drift, room noise enters both directions, and
the phone's echo cancellation and noise suppression interfere. It remains rejected for
both transports.

### USB Bluetooth adapter

A dedicated USB Bluetooth adapter could improve range or isolate Bluetooth from the
onboard Wi-Fi radio. It adds hardware, power draw, and another failure domain, and was not
tested. The onboard adapter is sufficient for the validated laptop setup, and the Pi's
onboard Bluetooth is expected to work but is unverified.

### Root or private Android APIs

Root does not create a writable uplink endpoint, and private APIs vary by build. Both
remain out of scope for the same reasons as the USB path.

## References

- [Bluetooth HFP guide](../bluetooth-hfp.md).
- [Architecture](../architecture.md), sections 4.1 and 10.
- [ADR 0001: Use a USB Audio Class gadget instead of root](./0001-use-usb-audio-gadget-instead-of-root.md).
- [Design brief](../design-brief.md), sections 3 and 7.
- WirePlumber fragment: [`config/wireplumber/51-bt-headset.conf`](../../config/wireplumber/51-bt-headset.conf).
- Bluetooth check script: [`scripts/bluetooth-check.sh`](../../scripts/bluetooth-check.sh).
- [WirePlumber Bluetooth configuration](https://pipewire.pages.freedesktop.org/wireplumber/daemon/configuration/bluetooth.html).
- [WirePlumber debug logging](https://pipewire.pages.freedesktop.org/wireplumber/daemon/logging.html).
- [Glossary](../glossary.md).
