# ADR 0001: Use a USB Audio Class gadget instead of root

- Status: Accepted
- Date: 2026-10-24
- Deciders: Project owner

## Context

The agent must put speech into the uplink of a cellular call and capture the remote
party's downlink. Android exposes no public API for either direction during a call.
`CAPTURE_AUDIO_OUTPUT`, `VOICE_CALL`, `VOICE_UPLINK`, and `VOICE_DOWNLINK` are signature
or privileged permissions; a normal app cannot hold them, and holding them would still
not provide a writable uplink endpoint.

Root changes the permission check, not the audio architecture. The vendor HAL may expose
call capture, but it rarely exposes a PCM sink for transmit, because the modem's TX path
is internal. `/dev/diag` can tap modem traffic on some rooted Qualcomm devices but cannot
inject audio, and `Incall_Music` is a device-specific ADSP mixer that is a lottery:
frequently broken, sometimes local-only, and absent on most recent platforms.

Android does have a supported path: a wired headset. A USB audio accessory supplies the
microphone for the uplink and receives the downlink. If the Pi is that accessory, it owns
both directions without entering the modem.

## Decision

The Pi runs a USB Audio Class 1 gadget with one speaker interface and one microphone
interface. Phone A enumerates it as a wired headset and routes call audio to it. The
agent uses `arecord` for the downlink and `aplay` for the uplink. Dialing and hangup
remain in Android, through a small helper app that calls `TelecomManager`.

The gadget uses 48 kHz, two channels, 16-bit, with `f_uac1` and configfs. UAC2 is the
fallback if a host behaves better with it. The agent does not use root, Xposed, a
custom ROM, a modified audio HAL, or modem diagnostics.

## Consequences

### Positive

- No root, bootloader unlock, or custom firmware is required on Phone A.
- The approach uses a standard USB class rather than a vendor-specific audio path.
- Both directions are available at the same boundary, so no separate capture and
  injection mechanisms are needed.
- It survives Android security patches and most One UI updates better than framework
  hooks, because it depends on audio routing rather than permission bypasses.
- The wired link is deterministic compared with Bluetooth, and ALSA tools make the
  path inspectable with `aplay`, `arecord`, and loopback tests.

### Negative

- Phone A must be the USB data host, so `adb` over the same cable is unavailable.
- One Pi supports one phone and one call at a time.
- The gadget must be re-enumerated after phone reboots or USB resets; a systemd unit
  and a re-arm check are required.
- The gadget format is fixed at 48 kHz stereo, so the pipeline must resample for STT
  and TTS.
- Vendor audio policy can change between builds, and some devices may not route call
  audio to USB at all.
- The phone's UI can override the route, for example if a user selects speakerphone.

### Risks

- **No call audio on some builds.** The loopback test in the setup guide is the gate;
  if it fails, the device is unsupported for this design.
- **USB audio routing disabled.** The Android developer setting "Disable USB audio
  routing" must be off. Verify on your device after every major update.
- **Route fallback to speaker.** If the headset is detached or rejected, call audio
  may play aloud, which is a privacy risk. Monitor enumeration.
- **Gadget enumeration failures.** A phone reboot, hub reset, or cable disturbance can
  leave the gadget absent. The heartbeat should check for the ALSA card.
- **UAC1 descriptor quirks.** Some hubs or phones may behave better with UAC2; keep
  `f_uac2` as a tested fallback.

## Alternatives considered

### Root with Xposed or a custom ROM

Root can grant signature permissions, and Xposed can hook framework methods. This may
enable call recording or rerouting on a compatible device, but it does not create a
writable uplink PCM endpoint. It also requires an unlocked bootloader, weakens device
security, breaks across OTA updates, and produces a different result on every model.
It was rejected as less reliable and less portable than the headset route.

### Modem-level paths

Some older Qualcomm platforms expose an ADSP `Incall_Music` mixer that mixes
application audio into the call path. It is device-specific, frequently broken,
sometimes local-only, and absent on most recent devices. `/dev/diag` and vendor
diagnostic protocols can capture modem traffic on some rooted devices, but they are
taps, not audio injection points. Both were rejected as unportable and firmware-bound.

### Bluetooth HFP and acoustic coupling

Bluetooth HFP provides both directions without a cable, but pairing, reconnection,
codec negotiation, and 2.4 GHz coexistence make it less deterministic. It was rejected
here as the default for that reason, but it is now supported as an opt-in alternative;
see [ADR 0006](./0006-use-bluetooth-hfp-audio.md). Acoustic coupling avoids USB entirely
but is sensitive to levels, echo, and room noise, and remains rejected.

## References

- [Design brief](../design-brief.md), sections 3 and 7.
- [Architecture](../architecture.md), sections 3 and 4.
- [Raspberry Pi setup](../raspberry-pi-setup.md).
- [`uac-gadget.sh`](../../scripts/uac-gadget.sh).
- [ADR 0002: PD hub power topology](./0002-power-phone-and-gadget-through-pd-hub.md).
- [Glossary](../glossary.md).
