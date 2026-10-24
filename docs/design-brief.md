# Design Brief — android-telephone-agent

Status: alpha; control plane implemented, voice adapters not bundled
Last updated: 2026-10-24
Owner: Fahim Ali Zain

This document is the source of truth for the project. Every other document must be
consistent with it. If a detail is not here, do not invent it; mark it as an open
question or a placeholder.

## 1. What this is

A programmable voice agent that places a scheduled outbound telephone call over the
public switched telephone network (PSTN) to its owner's mobile phone, holds a natural
spoken conversation, and hangs up.

The call originates from a real SIM card in a spare Android phone. The agent runs on a
Raspberry Pi Zero 2 W that presents itself to that phone as a USB-C audio headset
(USB Audio Class 1 gadget). The called party answers on an ordinary phone; no app,
no data connection, and no root are required on the receiving side.

A long-lived runtime can also expose the agent to other agents. MCP and A2A adapters
share one call queue, so an LLM host or a requester agent can delegate an outbound call,
observe it, answer a mid-call question, and collect a structured summary. The control
plane is asynchronous; the voice conversation remains real-time. This does not add
inbound PSTN calling.

The reference runtime implements the control plane and both listeners. It does not yet
enforce a destination allowlist, per-requester budgets, or requester identity, and the
default text pipeline does not bundle STT or TTS adapters. An independent Grok Voice
realtime adapter is available instead. See [Interoperability](./interop.md) for what
ships in the alpha and what remains follow-up work.

## 2. What this is not

- Not an interception or injection tool for a call already in progress on the owner's phone.
- Not a root/Magisk/Xposed project. Root is explicitly out of scope.
- Not a call recorder or surveillance product.
- Not a VoIP or SIP client. The call leg is a genuine cellular call.
- Not a finished product. The repository is a design document, a hardware build guide,
  and a reference software scaffold.
- Not an inbound PSTN call service in the first milestone. Inbound **control-plane
  tasking** over MCP and A2A is in scope: another agent may ask this agent to place an
  outbound call. A person dialing the agent's SIM is still not answered by the agent.

## 3. Why the obvious approaches do not work

These are established constraints, not opinions. They motivate the architecture.

1. **Android apps cannot write audio into a cellular call's uplink.** The modem's TX
   path is inside the modem/baseband and DSP firmware. There is no userspace PCM
   endpoint for the uplink.
2. **Root only removes permission checks.** `CAPTURE_AUDIO_OUTPUT`, `VOICE_CALL`,
   `VOICE_UPLINK`, and `VOICE_DOWNLINK` are signature/privileged permissions. Root can
   bypass the framework check, but if the vendor audio HAL does not expose the voice
   path, the result is silence.
3. **VoLTE does not help.** Speech is encoded by the modem and carried as RTP inside
   the modem's packet data network. It is not visible to the application processor's
   network stack. `/dev/diag` can sometimes capture modem traffic on rooted Qualcomm
   devices; it cannot inject audio.
4. **The Qualcomm `Incall_Music` path is a lottery.** Some older Qualcomm platforms
   expose an ADSP mixer that mixes application audio into the call path. It is
   device-specific, frequently broken, sometimes only audible locally, and absent on
   most recent devices. It is not a foundation for a product.
5. **A phone cannot call itself usefully.** If the agent runs on the phone being
   called, there is no second endpoint. The agent's audio goes to the local speaker,
   not to a remote party.

The architecture therefore moves the problem: instead of getting into the modem, the
Pi becomes the phone's **headset**. Android already supports routing a call's audio to
an external audio device. A headset supplies the microphone signal for the uplink and
receives the downlink. Owning the headset means owning both directions.

## 4. Topology

```
                    PSTN / cellular network
  Phone A  ──────────────────────────────────────►  Phone B
  (SIM A,                                            (SIM B, owner)
   the "agent's phone")
     │
     │ USB-C (UAC1 gadget: speaker + microphone)
     │
  Raspberry Pi Zero 2 W
  (audio bridge + agent runtime)
     │
     └── Wi-Fi ──► LAN ──► STT / LLM / TTS (India preferred)
                           or Grok Voice realtime

  Power: 25 W charger ──► USB-C hub (PD passthrough) ──► Phone A
                                      └── USB-A ──► Pi Zero 2 W
```

Caller ID seen by Phone B is SIM A's number. Phone B needs no software.

### Supported alternative: Bluetooth HFP

USB remains the canonical audio path, but it is not the only supported one. The host can
instead present itself as a Bluetooth HFP hands-free unit (HF), with Phone A as the audio
gateway (AG). PipeWire exposes the call as a SCO source (downlink) and SCO sink (uplink),
and the agent captures and plays those nodes with `pw-record` and `pw-play`. The phone can
charge from a separate supply because its USB port is not the audio link.

The trade-offs are pairing and reconnection, call-scoped SCO nodes, a shared 2.4 GHz radio,
and the requirement that the agent run in the same user session as PipeWire and
WirePlumber. Dialing and hangup still go through the LAN helper; HFP AT call control is not
implemented. The path was validated on a Linux laptop with a Galaxy A23 and a Google Meet
call, not on the Pi Zero 2 W and not on a PSTN call. See
[Bluetooth HFP](./bluetooth-hfp.md) and
[ADR 0006](./decisions/0006-use-bluetooth-hfp-audio.md).

## 5. Canonical hardware

| Role | Device | Notes |
|---|---|---|
| Phone A | Samsung Galaxy A23 4G, SM-A235F | 5000 mAh, 25 W wired charging, USB-C 2.0, OTG supported, no wireless charging. One UI. |
| Gadget / runtime | Raspberry Pi Zero 2 W | Quad Cortex-A53, 512 MB RAM, 2.4 GHz Wi-Fi, micro-USB OTG port, separate PWR port. |
| Storage | 32 GB microSD, A1/A2 | Raspberry Pi OS Lite 64-bit (Bookworm or later). |
| Hub | USB-C hub with PD passthrough and at least one USB-A port | Must pass data downstream while accepting charger power. |
| Charger | Samsung 25 W USB-C PD charger | Already owned. |
| Cables | USB-C→USB-C 60 W (charger→hub); USB-A→micro-USB (hub→Pi) | Short, strain-relieved. |
| Power backup | Mini UPS with 5 V output (router UPS class) | Covers hub and Wi-Fi router; the phone has its own battery. |
| Test aid | USB-C earphones with microphone | Phase 0 routing validation before buying the Pi. |

Budget: approximately ₹7,000 in India, excluding the phone, SIM, and charger.
Pi 4 Model B 2 GB is a supported alternative (+₹3,000) for Ethernet and headroom for
local speech models; power it through the GPIO header so the USB-C port remains
available for gadget mode (the Pi 5 cannot do USB device mode).

## 6. Power topology

- The charger feeds the hub's PD input. The hub is the power source for both Phone A
  (PD) and the Pi (5 V on the downstream USB-A port).
- Phone A is a **USB data host** but a **power sink**. It never sources VBUS to the
  gadget. This is standard USB-C dock behavior.
- The Pi Zero 2 W is bus-powered from the hub (150–300 mA typical, ~400 mA peak).
  If the hub port is unstable, power the Pi through its separate PWR port with a
  5 V/2 A supply.
- During a power cut, Phone A survives on its battery; the Pi and router do not.
  The UPS must cover the hub and the router.
- Phone A runs Samsung's Protect battery (80–85% depending on One UI build) to reduce
  swelling. The phone stands upright, without a case, in ventilated space.

## 7. USB gadget (canonical audio path)

- Implemented with configfs and the `f_uac1` function.
- Format: 48 kHz, 2 channels, 16-bit, `MaxPower` 250 (500 mA), `bcdUSB` 0x0200.
- `idVendor` 0x1d6b, `idProduct` 0x0104.
- Boot configuration: `dtoverlay=dwc2,dr_mode=peripheral` in
  `/boot/firmware/config.txt`; `modules-load=dwc2,libcomposite` in
  `/boot/firmware/cmdline.txt`.
- Started by a systemd unit at boot and re-armed if the phone re-enumerates.
- UAC1 is preferred for Android compatibility. UAC2 (`f_uac2`) is a fallback.
- No audio codec HAT and no USB sound card are required. Audio exists only as PCM
  inside the Pi.

**ALSA direction is inverted relative to a normal sound card.** On the Pi:

- `aplay` to the gadget card sends audio to Phone A's **microphone** input, i.e. the
  remote party hears it.
- `arecord` from the gadget card receives what Phone A would play in its earpiece,
  i.e. the remote party's speech.

This must be verified with a loopback test before the agent is connected.

## 8. Dialing and call control

Because Phone A is the USB host in the canonical topology, `adb` over USB is unavailable.
Two supported paths:

1. **Reference path (preferred): a small helper app on Phone A.**
   - Foreground service, battery optimization exempt.
   - Triggered over the LAN by HTTP or MQTT with a bearer token.
   - Places calls with `TelecomManager.placeCall()`.
   - On dual-SIM devices it must pass `TelecomManager.EXTRA_PHONE_ACCOUNT_HANDLE`
     for the agent SIM, otherwise One UI shows a SIM picker.
   - Requires the `CALL_PHONE` permission.
   - Optionally hangs up via `TelecomManager.endCall()` (requires privileged access on
     modern Android; otherwise the agent ends the call by other means and the remote
     side hangs up).
2. **Development path: wireless debugging.**
   `adb connect <phone-ip>:<port>` then
   `adb shell am start -a android.intent.action.CALL -d tel:+91XXXXXXXXXX`.
   Not suitable for unattended production because the pairing port and authorization
   do not survive reliably across reboots.

Tasker or MacroDroid with a webhook trigger is an acceptable no-code fallback.

## 9. Phone A configuration

- Protect battery ON.
- Developer options → Stay awake while charging ON.
- Developer options → "Disable USB audio routing" OFF (USB path; for Bluetooth HFP,
  enable **Calls and audio** for the host instead).
- Auto restart OFF (Settings → General management → Reset).
- Play Store auto-update OFF; software update auto-download OFF.
- SIM manager → default for calls = agent SIM; VoLTE ON.
- Helper app battery setting → Unrestricted.
- Screen lock may remain enabled; calls are placed by the helper service.
- Case removed; phone upright; USB cable strain-relieved.

## 10. Agent pipeline

Reference pipeline:

```
gadget capture (48 kHz) → downmix/resample to 16 kHz mono
  → voice activity detection → streaming STT (partial transcripts)
  → LLM (with call context and tools)
  → streaming TTS (sentence chunks)
  → resample to 48 kHz → gadget playback
```

The pipeline is transport-neutral. The USB gadget runs at 48 kHz; the Bluetooth HFP SCO
link is 16 kHz with mSBC or 8 kHz with CVSD, and the boundary resamples to the same
16 kHz mono processing format.

The reference runtime implements the control plane, the deliberative action loop, and
mid-call consultation. The default text path does not bundle STT/TTS adapters,
streaming, or barge-in; `run` and `serve` use scaffold-mode speech until adapters are
wired. An independent Grok Voice realtime adapter is available for full-duplex
speech-to-speech (`realtime.kind = "grok"`). The requirements below remain the target
for the voice path.

Requirements:

- **Barge-in.** If the callee speaks while TTS is playing, stop playback immediately.
- **Turn taking.** Endpointing window 300–500 ms; do not talk over the callee.
- **Timeouts.** Hard maximum call duration; hang up after prolonged silence.
- **Mid-call consultation.** If a required fact is missing, the agent may ask the
  requester through the control plane, speak a short holding phrase, and resume after a
  bounded wait. On timeout it uses a local fallback. The callee is never left in
  unbounded silence.
- **Idempotency.** One scheduled call per slot; retry at most twice with backoff.
- **No audio persistence by default.** Audio is streamed, not written to disk.

Latency budget (target, measured from end of callee utterance to start of agent speech):

| Stage | Budget |
|---|---|
| Network to India-region provider | 30–80 ms |
| STT finalization | 150–250 ms |
| LLM time to first token | 300–600 ms |
| TTS time to first audio | 150–300 ms |
| Playback and buffering | 50–100 ms |
| **Total** | **< 1.2 s** |

Provider guidance:

- STT/TTS: India-region services are strongly preferred from Kerala. Sarvam AI
  (Malayalam and English, Indian English), Azure Speech (Central/South India), or
  Google Cloud (Mumbai) are candidates. US-only endpoints add ~200–250 ms RTT.
- LLM: any low-latency streaming provider. Tool calls are optional for v0.
- Realtime speech-to-speech: the optional Grok Voice adapter
  (`realtime.kind = "grok"`) collapses STT, LLM, and TTS into one WebSocket. xAI
  does not document Malayalam for this endpoint as of 2026-10; test code-mixed
  speech before choosing it over an India-region STT/TTS pair.
- Local fallback: Whisper.cpp + Piper can run on a Pi 4/5, not comfortably on a
  Zero 2 W. Treat local models as a stretch goal.

The network codec (AMR-NB, AMR-WB, or EVS) caps audio fidelity regardless of the
gadget's 48 kHz rate. Do not optimize for hi-fi; optimize for latency and turn-taking.

## 11. Operations

- 24/7 operation on a Pi Zero 2 W. A systemd timer triggers a one-shot unit for each
  call slot; the unit must not restart automatically, or it would redial. The hardware
  watchdog reboots a hung Pi.
- Hardware watchdog enabled; journald `Storage=volatile` to reduce SD card wear.
- Daily heartbeat to Telegram/ntfy: call placed, duration, outcome. Alert if the call
  did not connect.
- Failure modes to handle: no cellular signal, phone rebooted, USB gadget not
  enumerated, Bluetooth disconnected or SCO nodes missing, helper app killed, provider
  outage, power cut, Wi-Fi down, SIM out of credit, call answered by voicemail.
- Weekly review of logs; monthly check of the phone battery for swelling.

## 12. Security, privacy, and legal

- Never commit phone numbers, SIM identifiers, API keys, or helper tokens.
  `config/config.example.toml` and `.env.example` contain placeholders only.
- The helper endpoint stays on the LAN, binds to a private interface, and requires a
  bearer token. Do not port-forward it.
- The MCP and A2A control plane binds to loopback by default, or to the LAN when
  configured, and requires a bearer token. The v1 design requires a destination
  allowlist and per-requester budgets; the alpha runtime does not enforce them yet. A
  holder of the shared token can dial any valid E.164 number and can read, answer, or
  cancel any task. Keep the listeners on a trusted network until allowlists, budgets,
  and per-requester tokens exist. The objective supplied by a remote agent is untrusted
  input; prompt instructions are not a technical policy boundary. Do not expose the
  listeners beyond a trusted network without TLS.
- Call audio is sent to third-party STT/TTS providers. Document this in the README and
  let users choose providers; do not persist audio.
- Personal use only by default. Outbound calling to third parties is regulated:
  in India, TRAI's TCCCPR rules apply to commercial and unsolicited calls; in the US,
  the TCPA and FCC rules on AI-generated voices apply. Recording calls requires
  consent that varies by jurisdiction.
- The project must not be positioned as a tool for spam, impersonation, or evasion of
  caller-ID rules.

## 13. Repository conventions

- License: MIT.
- Language: English, neutral tone, no emojis, no marketing language.
- Markdown: GitHub-flavored. Mermaid for diagrams. Tables for parameters and BOMs.
- Rupee amounts are ranges, marked as indicative; instruct readers to verify current
  prices.
- Code: Python 3.11+. The core reference runtime uses only the standard library; the
  optional `server` extra adds `mcp`, `a2a-sdk`, and `uvicorn`. Development dependencies
  are ruff and pytest.
- Commits: Conventional Commits.
- Every guide starts with purpose and prerequisites, and ends with verification steps
  and known limitations where relevant.
- Claims about hardware behavior must be hedged as "verify on your device" unless they
  are enforced by the USB or Android specification.

## 14. Canonical repository map

```
README.md
LICENSE
CONTRIBUTING.md
CODE_OF_CONDUCT.md
SECURITY.md
CHANGELOG.md
Makefile
.env.example
config/config.example.toml
config/wireplumber/51-bt-headset.conf
docs/
  design-brief.md
  architecture.md
  hardware.md
  bom.csv
  power-and-thermal.md
  raspberry-pi-setup.md
  bluetooth-hfp.md
  android-setup.md
  dial-helper.md
  voice-agent.md
  interop.md
  latency-budget.md
  prompts.md
  operations.md
  runbook.md
  troubleshooting.md
  faq.md
  glossary.md
  decisions/0001-...md .. 0007-...md
scripts/
  uac-gadget.sh
  install.sh
  audio-loopback-test.sh
  bluetooth-setup.sh
  bluetooth-check.sh
  bluetooth-audio-test.sh
  dial.sh
  systemd/uac-gadget.service
  systemd/telephone-agent.service
  systemd/telephone-agent.timer
  systemd/telephone-agent-bluetooth.service
  systemd/telephone-agent-bluetooth.timer
agent/
  pyproject.toml
  README.md
  telephone_agent/*.py
  tests/*.py
android-helper/
  README.md
  DialService.kt
  AndroidManifest.xml
.github/
  ISSUE_TEMPLATE/*
  PULL_REQUEST_TEMPLATE.md
  workflows/docs.yml
  workflows/python.yml
```

## 15. Open questions

1. Language: Malayalam, English, or code-mixed Malayalam-English. Sarvam supports
   Malayalam; code-mixed speech quality varies by vendor.
2. Helper app: build the reference Kotlin app or rely on Tasker/MacroDroid for v0.
3. Local fallback models: worth the Pi 4/5 cost, or cloud-only.
4. Call content and persona: daily briefing, check-in, or reminder. Prompt design is
   out of scope for the first milestone.
5. Whether to support inbound PSTN calls as well as outbound.
