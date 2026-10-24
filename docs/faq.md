# FAQ

Short answers to common questions about android-telephone-agent. Each answer links to the
document with the full detail. Device behavior that is not fixed by a specification should
be verified on your device.

## Why not root?

Root removes Android's permission checks, but it does not create a PCM endpoint for the cellular uplink.
The voice path lives in the modem and vendor audio HAL, and many devices never expose it even with root.
The headset model avoids the modem entirely and works without root, so root is explicitly out of scope.
See the [design brief](./design-brief.md).

## Why not Twilio or SIP?

The project places a real cellular call from a real SIM, so the callee needs no app, data connection, or
VoIP number. A SIP or Twilio leg would avoid the phone and SIM but would not use the agent SIM's number
or exercise the same cellular path, and a PSTN gateway is outside the project's scope. The trade-off is
one phone and one SIM per line. See the [design brief](./design-brief.md).

## Does the callee need an app or data?

No. The callee answers an ordinary voice call on any phone. The caller ID is the agent SIM's number, and
nothing is installed or configured on the receiving side. See the [design brief](./design-brief.md).

## What host do I need?

A Linux machine with BlueZ, PipeWire, and WirePlumber in the same user session, plus a working
Bluetooth adapter. A laptop, desktop, or small Linux box works; the validated host was an
Ubuntu laptop with a Samsung Galaxy A23 4G. The agent must run as the same user that owns the
PipeWire session, normally as a systemd user service with lingering. See
[Bluetooth HFP](./bluetooth-hfp.md) for the prerequisites and setup.

## Can I use any Android phone, including an old one?

The phone needs Bluetooth HFP support and the **Calls and audio** setting, an Android
version that can run the helper with the required permissions, and a healthy battery. The
Galaxy A23 is validated over HFP; other devices vary. An old phone can work as a dedicated
device if it meets those requirements, but a worn battery or an older Bluetooth
implementation may make it unreliable. See [Android setup](./android-setup.md) and
[Bluetooth HFP](./bluetooth-hfp.md).

## Does it work over USB?

No. Bluetooth HFP is the only supported audio transport. The earlier Pi-based and USB
Audio Class route was removed on 2026-10-24; its scripts, units, hardware guide, and
power documentation no longer exist. The phone's USB port is used only for charging. See
[ADR 0009](./decisions/0009-bluetooth-only-linux-host.md).

## Can it run on the phone itself?

No. If the agent runs on the phone being called, there is no second endpoint: its audio goes to the local
speaker, not to the remote party. Android also restricts background audio and call control in ways that
make an unattended agent fragile. The architecture deliberately moves the agent to the headset side of
the call. See the [design brief](./design-brief.md).

## Does it record or transcribe calls?

It transcribes speech to drive the conversation, but it does not record audio: audio is
streamed and not written to disk. Local transcripts are opt-in with
`[transcript].enabled = true`. When enabled, the runtime writes one timestamped JSONL file
per call, created with mode `0600` in a `0700` directory, containing the full E.164 number
when known (inbound calls have no caller ID), verbatim callee and agent text, and call
metadata. The file stays on-device and is intentionally **not masked**, unlike the
operational stdout and journald logs. Delete transcripts on a retention schedule. On the
text path, audio goes to the configured STT provider and text to the LLM and TTS providers;
with `realtime.kind = "grok"`, audio goes to xAI instead. Provider retention policies apply
either way. A debug audio capture, if enabled, should be opt-in, consented, short, and
deleted afterward. See [operations](./operations.md) and [Inbound calls](./inbound.md).

## Is this legal? What about TRAI and TCPA?

Personal use by the owner is the default intent, but outbound calling is regulated. In India, TRAI's
TCCCPR rules target commercial and unsolicited calls; in the US, the TCPA and FCC rules on AI-generated
voices apply. Recording calls requires consent that varies by jurisdiction. This is not legal advice;
confirm your obligations before calling anyone who has not agreed to be called. See the
[design brief](./design-brief.md).

## What languages does it support?

The reference text pipeline is language-agnostic but depends on the providers. Sarvam AI is a candidate
for Malayalam, English, and Indian English; Azure Speech and Google Cloud are also options for
India-region service. Code-mixed Malayalam-English quality varies by vendor and needs testing. The
optional Grok Voice realtime adapter documents English, Hindi, and Bengali but not Malayalam as of
2026-10. Prompts and voices are configuration, not hard-coded features. See the
[design brief](./design-brief.md).

## What latency should I expect?

The target is under 1.2 seconds from the end of the callee's utterance to the start of agent speech,
with a budget split across network, STT, LLM, TTS, and buffering. Real latency depends on the provider
region, Wi-Fi, Bluetooth codec, and cellular path, and can exceed 2 seconds on a poor link. No
end-to-end measurement exists yet, including on the validated Bluetooth run. Measure it from logs
rather than judging by ear. See [latency budget](./latency-budget.md).

## What does it cost to run?

The host is hardware you supply; there is no fixed bill of materials. Ongoing costs are the
SIM plan, electricity for the host and phone charging, and provider usage for STT, LLM, and
TTS on the text path. Realtime speech-to-speech is billed per minute of audio and can cost
several times more. Provider pricing changes, so estimate from current rates rather than a
fixed figure. See [voice agent](./voice-agent.md) for the cost model.

## Can it call multiple people?

One host and one phone place one call at a time. You can schedule sequential calls with
enough gap for the phone to return to idle, or run one phone and one host per number.
Parallel calls on one Bluetooth link are not supported, and a conferencing bridge is out of
scope. See [operations](./operations.md).

## Can it answer inbound calls?

Yes, as an opt-in mode. `telephone-agent inbound`, or the
`telephone-agent-inbound.service` user unit, polls the helper's `GET /state`, calls
`POST /answer` on a ringing call, and runs one Grok Voice realtime session. The daemon is
unit-tested, and one live inbound PSTN call was validated on 2026-10-24: the helper
answered after the watcher's 1 s delay, the session ran four turns in 39.0 s, and a
model-requested hangup returned the phone to idle.

It answers **every** ringing call on the agent SIM. There is no caller-ID filter, allowlist,
or screening because the helper does not request `READ_CALL_LOG`. `acceptRingingCall()` is
best effort for a non-default dialer and may fail on a locked screen or a vendor build
(`403`/`500`). Bluetooth carries audio only: HFP has no ringing signal, and SCO nodes appear
only after the call is answered. Inbound and outbound share one call slot. Use a SIM
dedicated to the agent. See [Inbound calls](./inbound.md).

## Can it send SMS or WhatsApp?

No. The project is scoped to voice calls. SMS would require additional helper work and is not
implemented; WhatsApp requires the Business API and a different architecture. See
[Android setup](./android-setup.md).

## What happens on a power cut?

The phone survives on its battery, but the host and router stop unless they have their own
backup. A call in progress fails, and a missed slot is skipped rather than placed late. On
restore, the system waits for time synchronization, reconnects Bluetooth, and alerts on the
missed call. See [operations](./operations.md).

## Is the helper endpoint secure?

It is designed for the LAN only, and call control requires a bearer token. The reference helper's
`/health` is unauthenticated and exposes only the device model and SIM label; firewall port 8765 so only
the host can reach it. Keep the token in an owner-readable environment file, rotate it by rebuilding the
APK, and never port-forward the endpoint. Plain HTTP is acceptable only on a trusted LAN; use a separate
VLAN or TLS on a shared network. See [operations](./operations.md).

## What if the phone reboots?

The helper should start at boot, keep its battery exemption, and restore the foreground service. The host
should wait for `/health` before dialing and may need to reconnect the Bluetooth link; pairing and trust
help, but reconnection is best effort. A reboot during a slot usually means a missed call and an alert.
Verify autostart behavior on your device. See [Android setup](./android-setup.md) and
[runbook](./runbook.md).

## How do I change the schedule?

Edit the user timer's `OnCalendar` expression or the application's slot configuration, keep the zone as
an IANA name such as `Asia/Kolkata`, then run `systemctl --user daemon-reload` and confirm with
`systemctl --user list-timers --all`. Keep `Persistent=false` and the grace window so a missed run is
skipped rather than placed late. Test the new slot with a supervised call. See
[operations](./operations.md).

## Related documents

[Design brief](./design-brief.md), [Bluetooth HFP](./bluetooth-hfp.md),
[Android setup](./android-setup.md), [Inbound calls](./inbound.md),
[operations](./operations.md), [runbook](./runbook.md),
[troubleshooting](./troubleshooting.md), and the
[Bluetooth user service](../scripts/systemd/telephone-agent-bluetooth.service).
