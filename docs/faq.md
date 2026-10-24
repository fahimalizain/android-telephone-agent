# FAQ

Short answers to common questions about android-telephone-agent. Each answer links to the document
with the full detail. Hardware behavior that is not fixed by a specification should be verified on your
device.

## Why not root?

Root removes Android's permission checks, but it does not create a PCM endpoint for the cellular uplink.
The voice path lives in the modem and vendor audio HAL, and many devices never expose it even with root.
The headset model avoids the modem entirely and works without root, so root is explicitly out of scope.
See the [design brief](./design-brief.md).

## Why not Twilio or SIP?

The project places a real cellular call from a real SIM, so the callee needs no app, data connection, or
VoIP number. A SIP or Twilio leg would avoid the phone and SIM but would not use the agent SIM's number
or exercise the same cellular path, and a PSTN gateway is outside the project's scope. The trade-off is
one phone and one SIM per line. See [hardware](./hardware.md).

## Does the callee need an app or data?

No. The callee answers an ordinary voice call on any phone. The caller ID is the agent SIM's number, and
nothing is installed or configured on the receiving side. See the [design brief](./design-brief.md).

## Can I use any Android phone, including an old one?

For the canonical USB path, the phone needs USB OTG (USB-C on the canonical device), a USB
audio class implementation that routes call audio, and an Android version that can run the
helper with the required permissions. For the Bluetooth alternative, it needs HFP support
and the **Calls and audio** setting; the Galaxy A23 is validated over HFP, but other devices
vary. An old phone can work as a dedicated device if it meets the requirements for the
chosen transport and its battery is healthy, but a worn battery or an older USB/Bluetooth
implementation may make it unreliable. See [Android setup](./android-setup.md),
[Bluetooth HFP](./bluetooth-hfp.md), and [hardware](./hardware.md).

## Why a Raspberry Pi?

The Pi Zero 2 W is small, inexpensive, runs Linux and Python, and its micro-USB port supports USB device
mode through `dwc2`. That lets it appear as a USB audio gadget, the canonical path. Bluetooth HFP is a
supported alternative that does not need USB device mode. A Pi 4 Model B is a supported alternative; the
Pi 5 cannot act as a USB device. See [Raspberry Pi setup](./raspberry-pi-setup.md) and
[Bluetooth HFP](./bluetooth-hfp.md).

## Can it run on the phone itself?

No. If the agent runs on the phone being called, there is no second endpoint: its audio goes to the local
speaker, not to the remote party. Android also restricts background audio and call control in ways that
make an unattended agent fragile. The architecture deliberately moves the agent to the headset side of
the call. See the [design brief](./design-brief.md).

## Is audio recorded?

No, not by default. Audio is streamed through the pipeline and is not written to disk. Audio is sent to
the configured STT provider; the resulting text is processed by the LLM and TTS provider, whose retention
policies apply. A debug capture should be opt-in, consented, short, and deleted afterward. See
[operations](./operations.md).

## Is this legal? What about TRAI and TCPA?

Personal use by the owner is the default intent, but outbound calling is regulated. In India, TRAI's
TCCCPR rules target commercial and unsolicited calls; in the US, the TCPA and FCC rules on AI-generated
voices apply. Recording calls requires consent that varies by jurisdiction. This is not legal advice;
confirm your obligations before calling anyone who has not agreed to be called. See the
[design brief](./design-brief.md).

## What languages does it support?

The reference pipeline is language-agnostic but depends on the providers. Sarvam AI is a candidate for
Malayalam, English, and Indian English; Azure Speech and Google Cloud are also options for India-region
service. Code-mixed Malayalam-English quality varies by vendor and needs testing. Prompts and voices are
configuration, not hard-coded features. See the [design brief](./design-brief.md).

## What latency should I expect?

The target is under 1.2 seconds from the end of the callee's utterance to the start of agent speech,
with a budget split across network, STT, LLM, TTS, and buffering. Real latency depends on the provider
region, Wi-Fi, and cellular codec, and can exceed 2 seconds on a poor link. Measure it from logs rather
than judging by ear. See [latency budget](./latency-budget.md).

## What does it cost to run?

The hardware is approximately ₹7,000 in India, excluding the phone, SIM, and charger; verify current
prices. Ongoing costs are the SIM plan, electricity for the Pi and phone charging, and provider usage
for STT, LLM, and TTS. Provider pricing changes, so estimate from current rates rather than a fixed
figure. See [hardware](./hardware.md) and [BOM](./bom.csv).

## Can it call multiple people?

One Pi and one phone place one call at a time. You can schedule sequential calls with enough gap for the
phone to return to idle, or run one phone and one Pi per number. Parallel calls on one gadget are not
supported, and a conferencing bridge is out of scope. See [operations](./operations.md).

## Can it receive calls?

Not in the reference version, which is outbound-only. Inbound answering is an open question in the
design, and the helper's call control would need additional work. Do not assume the agent will answer
if someone calls the SIM. See the [design brief](./design-brief.md).

## Can it send SMS or WhatsApp?

No. The project is scoped to voice calls. SMS would require additional helper work and is not
implemented; WhatsApp requires the Business API and a different architecture. See
[Android setup](./android-setup.md).

## What happens on a power cut?

The phone survives on its battery, but the Pi and router stop unless they are on a UPS. A call in
progress fails, and a missed slot is skipped rather than placed late. On restore, the system waits for
time synchronization, re-arms the gadget, and alerts on the missed call. See
[operations](./operations.md).

## Is the helper endpoint secure?

It is designed for the LAN only, and call control requires a bearer token. The reference helper's
`/health` is unauthenticated and exposes only the device model and SIM label; firewall port 8765 so only
the Pi can reach it. Keep the token in a root-readable environment file, rotate it by rebuilding the
APK, and never port-forward the endpoint. Plain HTTP is acceptable only on a trusted LAN; use a separate
VLAN or TLS on a shared network. See [operations](./operations.md).

## Can I use Bluetooth instead of USB?

Yes. Bluetooth HFP is a supported alternative transport: the Linux host acts as the
hands-free unit (HF), Phone A as the audio gateway (AG), and PipeWire exposes the call as
a SCO source and sink. USB remains the default and canonical path. The caveats are that
pairing and reconnection add failure modes, SCO nodes exist only during a call, the agent
must run in the same user session as PipeWire/WirePlumber, and Bluetooth shares 2.4 GHz
spectrum with Wi-Fi. mSBC gives a 16 kHz link, but the cellular codec still caps fidelity.
Dialing and hangup still go through the LAN helper. The path was validated on a laptop
with a Galaxy A23 and a Google Meet call, not on a Pi Zero 2 W or a PSTN call. See
[Bluetooth HFP](./bluetooth-hfp.md) and
[ADR 0006](./decisions/0006-use-bluetooth-hfp-audio.md).

## Why not a Raspberry Pi 5?

The Pi 5 cannot operate as a USB device, so it cannot present the UAC gadget to the phone. Use a Pi Zero
2 W, or a Pi 4 Model B powered through the GPIO header so its USB-C port remains available. A Pi 5 could
use the Bluetooth HFP alternative in principle, but that path is unverified on a Pi 5 and is not the
canonical build. See [Raspberry Pi setup](./raspberry-pi-setup.md) and
[Bluetooth HFP](./bluetooth-hfp.md).

## What if the phone reboots?

The helper should start at boot, keep its battery exemption, and restore the foreground service. The Pi
must detect the re-enumeration and re-arm the gadget, and the agent should wait for `/health` before
dialing. A reboot during a slot usually means a missed call and an alert. Verify autostart behavior on
your device. See [Android setup](./android-setup.md) and [runbook](./runbook.md).

## How do I change the schedule?

Edit the timer's `OnCalendar` expression or the application's slot configuration, keep the zone as an
IANA name such as `Asia/Kolkata`, then run `sudo systemctl daemon-reload` and confirm with
`systemctl list-timers --all`. Keep `Persistent=false` and the grace window so a missed run is skipped
rather than placed late. Test the new slot with a supervised call. See [operations](./operations.md).

## Related documents

[Design brief](./design-brief.md), [hardware](./hardware.md),
[Raspberry Pi setup](./raspberry-pi-setup.md), [Bluetooth HFP](./bluetooth-hfp.md),
[Android setup](./android-setup.md), [operations](./operations.md), [runbook](./runbook.md),
[troubleshooting](./troubleshooting.md), and the
[service unit](../scripts/systemd/telephone-agent.service).
