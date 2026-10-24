# Design Brief — android-telephone-agent

Status: alpha; control plane implemented, voice adapters not bundled
Last updated: 2026-10-24
Owner: Fahim Ali Zain

> The earlier Pi-based and USB audio route was removed on 2026-10-24. Bluetooth HFP
> on a Linux host is now the only supported audio transport; [ADR
> 0009](./decisions/0009-bluetooth-only-linux-host.md) records the pivot.

This document is the source of truth for the project. Every other document must be
consistent with it. If a detail is not here, do not invent it; mark it as an open
question or a placeholder.

## 1. What this is

A programmable voice agent that places a scheduled outbound telephone call over the
public switched telephone network (PSTN) to its owner's mobile phone, holds a natural
spoken conversation, and hangs up.

The call originates from a real SIM card in a spare Android phone ("Phone A"). The agent
runs on a Linux host that pairs with that phone as a Bluetooth Hands-Free Profile (HFP)
headset. The host is the hands-free unit (HF) and Phone A is the audio gateway (AG); call
audio travels over a synchronous SCO link. The called party answers on an ordinary phone;
no app, no data connection, and no root are required on the receiving side.

A long-lived runtime can also expose the agent to other agents. MCP and A2A adapters
share one call queue, so an LLM host or a requester agent can delegate an outbound call,
observe it, answer a mid-call question, and collect a structured summary. The control
plane is asynchronous; the voice conversation remains real-time.

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
- Not a finished product. The repository is a design document and a reference software
  scaffold.
- Not an inbound PSTN call service in the first milestone. Inbound **control-plane
  tasking** over MCP and A2A is in scope: another agent may ask this agent to place an
  outbound call. A person dialing the agent's SIM is still not answered by the default
  configuration; an opt-in v0.3 inbound mode is implemented but not live-validated. See
  [Inbound calls](./inbound.md).
- Not a single-board-computer or USB audio project. That route was removed; see
  [ADR 0009](./decisions/0009-bluetooth-only-linux-host.md).

## 3. Why the obvious approaches do not work

These are established constraints, not opinions. They motivate the architecture.

1. **Android apps cannot write audio into a cellular call's uplink.** The modem's TX
   path is inside the modem/baseband and DSP firmware. There is no userspace PCM
   endpoint for the uplink.
2. **Root only removes permission checks.** `CAPTURE_AUDIO_OUTPUT`, `VOICE_CALL`,
   `VOICE_UPLINK`, and `VOICE_DOWNLINK` are signature/privileged permissions. Root can
   bypass the framework check, but if the vendor audio HAL does not expose the voice
   path, the result is silence.
3. **VoLTE does not help.** Speech is encoded by the modem and carried as RTP inside the
   modem's packet data network. It is not visible to the application processor's
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
Linux host becomes the phone's **Bluetooth headset**. Android already supports routing a
call's audio to an external headset. A headset supplies the microphone signal for the
uplink and receives the downlink. Owning the headset means owning both directions.

## 4. Topology

```text
                    PSTN / cellular network
  Phone A  ──────────────────────────────────────►  Phone B
  (SIM A,                                            (SIM B, owner)
   the "agent's phone")
     │
     │ Bluetooth HFP: SCO, mSBC 16 kHz or CVSD 8 kHz
     │
  Linux host
  (BlueZ + PipeWire/WirePlumber; agent runtime)
     │
     ├── LAN HTTP ──► Phone A helper: dial, answer, hangup
     │
     └── Wi-Fi ──► LAN ──► STT / LLM / TTS (India preferred)
                           or Grok Voice realtime

  Power: phone charger ──► Phone A
         mains ──► Linux host
```

Caller ID seen by Phone B is SIM A's number. Phone B needs no software.

Bluetooth HFP is the only audio path. The host advertises the HF role and Phone A is the
AG. PipeWire exposes the call as a SCO source (downlink) and SCO sink (uplink), and the
agent captures and plays those nodes with `pw-record` and `pw-play`. The phone charges
from a separate supply because its USB port is not the audio link.

The trade-offs are pairing and reconnection, call-scoped SCO nodes, a shared 2.4 GHz
radio, and the requirement that the agent run in the same user session as PipeWire and
WirePlumber. Dialing and hangup still go through the LAN helper; HFP AT call control is
not implemented. The path was validated end to end on a Linux laptop with a Galaxy A23 4G
over outbound PSTN calls, including a Grok Voice realtime conversation. Live inbound
calls and latency measurements remain unvalidated. See [Bluetooth HFP](./bluetooth-hfp.md)
and [ADR 0006](./decisions/0006-use-bluetooth-hfp-audio.md).

## 5. Host requirements

| Role | Requirement | Notes |
|---|---|---|
| Host | Linux with BlueZ, PipeWire, and WirePlumber in the same user session | The agent must run as the user that owns the PipeWire session. |
| Bluetooth | A working adapter | Onboard Bluetooth was sufficient for the validated laptop. |
| Phone A | Samsung Galaxy A23 4G, SM-A235F | Validated device; 5000 mAh, 25 W wired charging, no wireless charging. Other phones with HFP call audio may work; verify on your device. |
| SIM | Voice-capable SIM with credit and a known number | Use a dedicated SIM for unattended and inbound use. |
| Charger | Samsung 25 W USB-C PD charger | Already owned; any suitable phone charger works. |
| Network | Private LAN shared by the phone and host, plus internet access | The helper is LAN-only; the host reaches cloud providers. |
| Power | Mains power for the host | The phone has its own battery; a long outage can still stop the host or router. |
| Test aid | A second phone you can call and answer | Used for supervised audio tests. |

The validated host was a Linux laptop (MSI GL738SE) running Ubuntu with BlueZ 5.85,
PipeWire 1.6.2, and WirePlumber 0.5.13. A desktop, mini PC, or other Linux machine with a
Bluetooth adapter is expected to work; verify the adapter roles and SCO routing on your
host. No single-board computer, USB audio device, USB hub, UPS, microSD card, or fixed bill of materials
is part of the design.

## 6. Power topology

- The phone charges from its own USB-C charger. The USB port is free because it is not
  the audio link, so any suitable charger works.
- The host runs from mains power. A laptop uses its own battery and charger; a desktop or
  mini PC uses its normal supply.
- During a power cut, Phone A survives on its battery, but the host and router do not
  unless they have their own backup. A call in progress fails if the host loses power.
- Phone A runs Samsung's Protect battery (80–85% depending on One UI build) to reduce
  swelling. The phone stands upright, without a case, in ventilated space.

## 7. Audio transport: Bluetooth HFP

- The host advertises the Hands-Free Profile **hands-free unit (HF)** role; Phone A is
  the **audio gateway (AG)**. Android already routes call audio to a Bluetooth headset,
  so no root or vendor API is involved.
- WirePlumber enables the role with the fragment in
  [`config/wireplumber/51-bt-headset.conf`](../config/wireplumber/51-bt-headset.conf).
- The link uses mSBC at 16 kHz when both sides negotiate it, with CVSD at 8 kHz as the
  narrowband fallback. The host cannot force mSBC if the phone does not offer it.
- While a call is active, PipeWire creates two nodes for the phone's Bluetooth address:

  | Direction | Node | Factory | Media class | Agent action |
  |---|---|---|---|---|
  | Callee to agent (downlink) | `bluez_input.<address>.<id>` | `api.bluez5.sco.source` | `Stream/Output/Audio` | Capture with `pw-record` |
  | Agent to callee (uplink) | `bluez_output.<address>.<id>` | `api.bluez5.sco.sink` | `Stream/Input/Audio` | Play with `pw-play` |

- The `.<id>` suffix is a SPA object id and can change per connection. Discover nodes by
  `api.bluez5.address` and `factory.name`, never by a hard-coded `.0` or `.1`.
- SCO nodes exist only during a call or SCO link. The backend waits up to
  `node_timeout_s` for them; if they never appear, the call fails before audio starts.
- Direction is direct: capture from the SCO source is the remote party, and playback to
  the SCO sink is the agent's voice. There is no USB-style inversion.
- The agent must run in the same user session as PipeWire and WirePlumber, normally a
  systemd user service with lingering. A system service running as another user does not
  see the SCO nodes.
- HFP call control (ATD, ATA, CHUP) is not implemented. Dialing, answering, and hangup
  stay with the LAN helper.

## 8. Dialing and call control

Call control is separate from the audio transport:

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
   - Exposes `GET /state` and `POST /answer` for the opt-in inbound watcher.
2. **Development path: wireless debugging.**
   `adb connect <phone-ip>:<port>` then
   `adb shell am start -a android.intent.action.CALL -d tel:+91XXXXXXXXXX`.
   Not suitable for unattended production because the pairing port and authorization
   do not survive reliably across reboots.

Tasker or MacroDroid with a webhook trigger is an acceptable no-code fallback.

## 9. Phone A configuration

- Protect battery ON.
- Developer options → Stay awake while charging ON.
- Bluetooth → pair the host, trust it, and enable **Calls and audio** for it.
- Auto restart OFF (Settings → General management → Reset).
- Play Store auto-update OFF; software update auto-download OFF.
- SIM manager → default for calls = agent SIM; VoLTE ON.
- Helper app battery setting → Unrestricted.
- Screen lock may remain enabled; calls are placed by the helper service.
- Case removed; phone upright; charging cable strain-relieved.

## 10. Agent pipeline

Reference pipeline:

```text
SCO capture (16 kHz mSBC or 8 kHz CVSD) → resample to 16 kHz mono
  → voice activity detection → streaming STT (partial transcripts)
  → LLM (with call context and tools)
  → streaming TTS (sentence chunks)
  → resample to the SCO rate → SCO sink playback
```

The pipeline is transport-neutral. Bluetooth HFP SCO is 16 kHz with mSBC or 8 kHz with
CVSD, and the boundary resamples to the same 16 kHz mono processing format. PipeWire
handles the stream-to-link conversion; the agent does not address ALSA cards.

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
- Local fallback: Whisper.cpp + Piper can run on a Linux host with more CPU and RAM.
  Treat local models as a stretch goal.

The network codec (AMR-NB, AMR-WB, or EVS) caps audio fidelity regardless of the SCO
rate. Do not optimize for hi-fi; optimize for latency and turn-taking.

## 11. Operations

- 24/7 operation on the Linux host. A systemd **user** timer triggers a one-shot user
  unit for each call slot; the unit must not restart automatically, or it would redial.
  A host watchdog may reboot a hung machine where available.
- journald may use volatile storage to reduce disk writes on an SSD or SD-backed host;
  this is optional on a desktop.
- Daily heartbeat to Telegram/ntfy: call placed, duration, outcome. Alert if the call
  did not connect.
- Failure modes to handle: no cellular signal, phone rebooted, Bluetooth disconnected or
  SCO nodes missing, helper app killed, provider outage, power cut, Wi-Fi down, SIM out
  of credit, call answered by voicemail.
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
- Markdown: GitHub-flavored. Mermaid for diagrams. Tables for parameters and
  requirements.
- Code: Python 3.11+. The core reference runtime uses only the standard library; the
  optional `server` extra adds `mcp`, `a2a-sdk`, and `uvicorn`. Development dependencies
  are ruff and pytest.
- Commits: Conventional Commits.
- Every guide starts with purpose and prerequisites, and ends with verification steps
  and known limitations where relevant.
- Claims about device behavior must be hedged as "verify on your device" unless they
  are enforced by the Android or Bluetooth specification.

## 14. Canonical repository map

```text
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
  bluetooth-hfp.md
  inbound.md
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
  decisions/0001-...md .. 0009-...md
scripts/
  bluetooth-setup.sh
  bluetooth-check.sh
  bluetooth-audio-test.sh
  dial.sh
  build-helper.sh
  install-helper.sh
  helper-smoke-test.sh
  systemd/telephone-agent-bluetooth.service
  systemd/telephone-agent-bluetooth.timer
  systemd/telephone-agent-inbound.service
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
3. Local fallback models: worth the extra CPU and RAM on the host, or cloud-only.
4. Call content and persona: daily briefing, check-in, or reminder. Prompt design is
   out of scope for the first milestone.
5. Whether to support inbound PSTN calls as well as outbound.

   **Resolved (2026-10-24).** Implemented as an opt-in `inbound` daemon: the helper
   exposes `GET /state` and `POST /answer`, and the daemon polls, answers, and runs one
   Grok Voice session per call. Bluetooth carries audio only, so HFP has no ringing signal
   and the SCO nodes appear only after the call is answered. It answers every call with no
   caller-ID filter. The daemon is unit-tested, but no live inbound PSTN call has been
   validated. See [Inbound calls](./inbound.md).
