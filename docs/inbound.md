# Inbound calls

The inbound path turns Phone A into an answering agent: the helper reports a
ringing call over the LAN, the Pi asks it to answer, and the Grok Voice realtime
adapter handles the conversation. This document covers the signaling topology,
the helper contract, configuration, systemd deployment, and the privacy limits
you must accept before enabling auto-answer.

The outbound path is documented in [Voice agent](voice-agent.md) and
[Architecture](architecture.md). The helper's full API is in
[Dial helper](dial-helper.md).

## Topology

```text
                 LAN, bearer token
  ┌────────────┐  GET /state      ┌──────────────────────────┐
  │   Phone A  │◀────────────────▶│  Pi: telephone-agent      │
  │  SIM +     │  POST /answer    │  inbound watcher          │
  │  helper    │  POST /hangup    │  Grok Voice session       │
  └─────┬──────┘                  └────────────┬─────────────┘
        │                                      │
        │  Bluetooth HFP SCO or USB audio      │
        └──────────────────────────────────────┘
                 call audio only
```

The helper is the **signaling path**. It owns Android call control and exposes
the call phase. Call audio is separate: it travels over Bluetooth HFP (PipeWire
SCO nodes) or the USB Audio Class gadget. Bluetooth HFP does not carry a ringing
event or caller ID to user space; the helper is how the agent learns that a call
is ringing.

## Helper contract

Every request carries `Authorization: Bearer <dialer.token>`.

| Request | Success | Meaning |
|---|---|---|
| `GET /state` | `200 {"state":"idle"\|"ringing"\|"offhook"\|"unknown"}` | Side-effect-free call phase. |
| `POST /answer` | `202 {"state":"answering"}` | Telecom accepted the answer request for the ringing call. |
| `POST /hangup` | `202` | Ends the answered call because `/answer` set the helper's call window. |

`/answer` failures:

| Status | Body | Meaning |
|---|---|---|
| `409` | `{"error":"no ringing call"}` | The call stopped ringing, was answered manually, or the state is unreadable. No session starts. |
| `403` | `{"error":"answer permission denied"}` | `ANSWER_PHONE_CALLS` is missing or Android denied `acceptRingingCall()`. |
| `500` | `{"error":"answer failed"}` | Android threw while trying to answer. |

`unknown` is a read failure, not a call phase. The watcher treats it as
not-actionable and keeps polling. `202 answering` means Telecom accepted the
request; it does not prove that the call is connected. The subsequent `/state`
normally reports `offhook`.

A ringing or active call blocks `POST /call`, so inbound and outbound calls are
mutually exclusive on the same phone. Do not run the outbound timer or a
scheduled `run` while the inbound watcher is enabled.

## Prerequisites

- `realtime.kind = "grok"`. The inbound command refuses to start with the text
  pipeline, which has no inbound audio path.
- The helper provisioned with `ANSWER_PHONE_CALLS` and a valid bearer token. See
  [Dial helper](dial-helper.md) and [Android setup](android-setup.md).
- An audio path that exists only while the call is active:
  - Bluetooth HFP: PipeWire and WirePlumber in the **same user session** as the
    agent, with the host in the Hands-Free role and Phone A set to **Calls and
    audio**. See [Bluetooth HFP](bluetooth-hfp.md).
  - USB gadget: the gadget is enumerated and Phone A routes call audio to it.
- An `XAI_API_KEY` in the service environment or `[realtime].api_key`.

## Configuration

```toml
[inbound]
# Seconds between GET /state polls. 0.2-30; 1 second is a good default.
poll_interval_s = 1.0
# Seconds to wait after seeing "ringing" before answering. 0-30. The watcher
# re-checks the state after the delay and does not answer a call that stopped
# ringing or was answered manually. 0 answers immediately.
answer_delay_s = 0.0

[realtime]
kind = "grok"
model = "grok-voice-latest"
voice = "eve"
greeting = "Hello, this is the telephone agent. How can I help?"
```

The same values can be overridden with
`TELEPHONE_AGENT_INBOUND__POLL_INTERVAL_S` and
`TELEPHONE_AGENT_INBOUND__ANSWER_DELAY_S`.

`realtime.greeting` is requested only after the session opens the audio device,
so with Bluetooth it fires after the SCO nodes appear. See
[Voice agent](voice-agent.md) for the realtime adapter settings.

## Running the watcher

```bash
telephone-agent --config config/config.toml inbound
telephone-agent --config config/config.toml inbound --once
```

The watcher polls `/state`, answers a `ringing` call, and runs exactly one
session per answered call. After the session, it waits for the helper to report
`idle` before re-arming, so a call that ends remotely cannot cause a second
session. A call that is already `offhook` is never answered: the user may have
answered it manually.

`--once` handles at most one call and exits after that call's session. SIGTERM
and SIGINT set a stop event; the event is passed to the active session, which
ends the call and asks the helper to hang up. Sleeps between polls are
interruptible, so shutdown does not wait for the next poll interval.

Helper errors (unreachable, 401/403, `unknown` state, `409` from `/answer`) are
logged and retried with bounded backoff. They do not terminate the daemon.
With Bluetooth, audio setup fails if the SCO nodes do not appear within
`audio.node_timeout_s`; that failure ends the session and the helper is asked to
hang up.

`make` does not wrap this command; use the console script or
`python -m telephone_agent ... inbound`.

## systemd user unit

`scripts/systemd/telephone-agent-inbound.service` is a **user** unit. It must
run as the same account that owns the PipeWire and WirePlumber session;
otherwise it cannot see the Bluetooth SCO nodes. It uses the root `.venv`:

```ini
[Service]
Type=simple
Restart=always
RestartSec=5
WorkingDirectory=%h/android-telephone-agent
EnvironmentFile=-%h/android-telephone-agent/.env
ExecStart=%h/android-telephone-agent/.venv/bin/telephone-agent \
  --config %h/android-telephone-agent/config/config.toml inbound
```

Install it and keep the user manager running on a headless host:

```bash
mkdir -p ~/.config/systemd/user
install -m644 scripts/systemd/telephone-agent-inbound.service \
  ~/.config/systemd/user/
sudo loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now telephone-agent-inbound.service
journalctl --user -u telephone-agent-inbound.service -n 100 --no-pager
```

The unit has `Restart=always` because the watcher is long-lived and does not
place a call on startup. A crashed process re-arms safely: it polls rather than
redialing. `TimeoutStopSec=30` gives the active session time to close; SIGTERM
sets the stop event and the session asks the helper to hang up.

Do not enable this unit together with an outbound dialing unit/timer for the
same phone. Inbound and outbound calls are mutually exclusive, and the helper
rejects `/call` while a call is ringing or active.

## Audio: Bluetooth carries audio only

Bluetooth HFP has no ringing notification or caller ID in user space. PipeWire
creates the SCO source/sink only **after** the call is answered. The watcher
therefore answers through the helper first, then builds the audio device and
Grok session. The greeting is requested after `audio.start()` succeeds, when
the uplink can actually carry speech.

The unit must share the user's PipeWire/WirePlumber session. A system unit
running as another user has a different session bus and `XDG_RUNTIME_DIR` and
will not find the SCO nodes. Enable lingering if nobody is logged in.

The USB gadget path is also supported: the ALSA device is independent of the
call, but Phone A must route call audio to the gadget.

## Auto-answer and privacy

> **Warning: this answers every ringing call on the agent SIM.**
>
> There is no caller-ID filter, allowlist, blocklist, or screening. The helper
> does not request `READ_CALL_LOG` and `/state` never returns a caller's number.
> Any call that reaches the SIM while the watcher is running is answered and
> connected to the voice agent, including unknown numbers, spam, and wrong
> numbers. Use a SIM dedicated to the agent, not a personal number. Review the
> call-recording and disclosure rules in your jurisdiction before enabling it.

`acceptRingingCall()` is best effort for non-dialer apps. Some Android versions,
vendors, and locked-screen states can deny or ignore it even with
`ANSWER_PHONE_CALLS` granted. The helper reports `403` or `500` in that case and
the watcher logs the failure and keeps polling. A `202` response means Telecom
accepted the request, not that the caller is connected.

The agent may speak a greeting before the caller says anything. Make the
greeting identify the automation when required by local law. Do not use this
path for impersonation, spam, or caller-ID evasion.

## Transcripts

When `[transcript].enabled = true`, every inbound call writes one JSONL file
using the same logger as outbound calls. `call_start` has:

- `"direction": "inbound"`;
- `"number": ""` because caller ID is unavailable;
- no `objective` unless one is supplied;
- the same `audio_backend`, `realtime_kind`, `realtime_model`, and
  `realtime_voice` metadata as outbound calls.

Speech turns and the final result are recorded as usual. Transcripts are
**not redacted**: they contain the full conversation text and are personal
data. They are created with mode 0600 in a directory with mode 0700. Keep them
on-device and apply a retention policy.

## Limits and troubleshooting

| Symptom | Check |
|---|---|
| `inbound requires realtime.kind = 'grok'` | Set `[realtime].kind = "grok"`; the text pipeline has no inbound path. |
| `/state` stays `unknown` | Android denied the call-state query. Check the helper's log and phone permission/role state. |
| `/answer` returns 409 | The call stopped ringing or was answered manually. This is not retried; the watcher keeps polling. |
| `/answer` returns 403 | Grant `ANSWER_PHONE_CALLS`; some vendors restrict non-dialer answering. |
| `/answer` returns 500 | Android rejected the answer attempt; check the helper's log and locked-screen state. |
| Call is answered but the agent is silent | With Bluetooth, confirm the SCO nodes appeared and the phone routes call audio to the host. Run `doctor` during the call. |
| Second call is not answered | The watcher waits for `idle` after a session. If `/hangup` failed and the phone remains `offhook`, end the call on the phone. |
| Unit cannot find PipeWire | Run it as a user unit in the PipeWire user's session and enable lingering. |
| Outbound call returns 409 | A call is ringing or active. Inbound and outbound are mutually exclusive. |
