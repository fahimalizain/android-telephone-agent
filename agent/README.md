# Telephone agent runtime

This directory contains the reference Python runtime for the telephone voice agent
described in the [design brief](../docs/design-brief.md). It runs on the Linux
host next to Phone A, captures and plays call audio through a Bluetooth
HFP SCO link managed by PipeWire, and asks a small LAN helper to place, answer,
and end the cellular call. Outbound calls are placed by the agent; inbound calls
are detected by polling the helper and are answered automatically.

The runtime is deliberately small. It uses only the Python standard library at
runtime; `pytest` and `ruff` are development dependencies. It is a scaffold for
the design, not a finished conversational agent: the default text pipeline does
not bundle speech-to-text (STT), text-to-speech (TTS), scheduling, barge-in, or
streaming. An independent Grok Voice realtime adapter is included for
full-duplex speech-to-speech; it bypasses the STT/brain/TTS seams. The sections
below explain where the pieces belong.

## Prerequisites

- A Linux host running BlueZ, PipeWire, and WirePlumber in the user session that
  runs the agent. A laptop, desktop, or mini PC works. The validated host was an
  Ubuntu laptop with onboard Bluetooth.
- Python 3.11 or later. Check with `python3 --version`.
- PipeWire, WirePlumber, and the PipeWire command-line tools `pw-record`,
  `pw-play`, and `pw-dump`, plus `bluetoothctl` for pairing and link checks. On
  Debian and Ubuntu the tools are in `pipewire-bin`; package names vary by
  distribution. Verify:

  ```bash
  command -v pw-record pw-play pw-dump wpctl
  ```

  The host must run WirePlumber with the HFP hands-free role enabled, and Phone A
  must be paired, trusted, and set to **Calls and audio**. See
  [Bluetooth HFP](../docs/bluetooth-hfp.md) for the WirePlumber fragment, pairing,
  and SCO verification.
- The agent must run as the same user as PipeWire and WirePlumber. A system
  service does not share the user's session, so it will not see the SCO nodes.
  For a headless install, run a systemd user service and enable lingering; see
  the systemd section below.
- The phone-side helper app reachable on the LAN with a bearer token. The
  reference HTTP contract is documented in `telephone_agent/dialer.py`:
  `GET /health`, `GET /state`, `POST /call`, `POST /answer`, and `POST /hangup`.
  Inbound answering also needs `ANSWER_PHONE_CALLS` on the phone.
- Phone A paired and trusted by the host, with **Calls and audio** enabled so
  call audio is routed to the Bluetooth HFP link.
- For `brain.kind = "http"`, network access to an OpenAI-compatible endpoint.
- For `realtime.kind = "grok"`, the optional `websockets` package and an xAI
  API key in `XAI_API_KEY`. Install the extra with
  `pip install -e ".[realtime]"` in `agent/`.

The Bluetooth SCO path is not inverted. The `api.bluez5.sco.source` node
(`bluez_input...`) is the downlink to capture, and the `api.bluez5.sco.sink` node
(`bluez_output...`) is the uplink to play. The runtime discovers those nodes by
the phone's Bluetooth address and factory name because the `.<id>` suffix can
change between connections.

## Install

For a production install on the host, use a virtual environment inside `agent/`:

```bash
cd "$HOME/android-telephone-agent/agent"
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -e .
```

The resulting console script is `agent/.venv/bin/telephone-agent`. The systemd
example below uses that path. Development tools are optional:

```bash
pip install -e ".[dev]"
```

For the Grok Voice realtime adapter, also install the optional WebSocket client:

```bash
pip install -e ".[realtime]"
```

For repository development, the root `Makefile` uses a root `.venv` instead:

```bash
make install   # creates .venv and installs ./agent with dev extras
make test
make lint
```

Do not commit either virtual environment; both are ignored by `.gitignore`.

## Configuration

Copy the example and replace every placeholder. From the repository root (one
level above `agent/`):

```bash
cp config/config.example.toml config/config.toml
chmod 600 config/config.toml
```

`config/config.toml` is ignored by `.gitignore`, but never add a populated file to
git by force. Keeping secrets in the environment is preferable; alternatively use
`config/local.toml` and pass `--config config/local.toml`.

The CLI's default `--config` path is relative to the current working directory.
From `agent/`, pass `--config ../config/config.toml` or an absolute path.

### Precedence

1. Dataclass defaults in `telephone_agent/config.py`.
2. Values in the TOML file.
3. `TELEPHONE_AGENT_*` environment variables.

Environment overrides use a double underscore between section and key:

```bash
export TELEPHONE_AGENT_DIALER__TOKEN='...'
export TELEPHONE_AGENT_AUDIO__BLUETOOTH_ADDRESS='AA:BB:CC:DD:EE:FF'
export TELEPHONE_AGENT_BRAIN__KIND='http'
```

The runtime does not read `.env` automatically. Export the variables in the
shell, or let systemd load them with `EnvironmentFile=`. See
[.env.example](../.env.example) for placeholders. A shell can load it with:

```bash
set -a
. ./.env
set +a
```

Unknown TOML keys and unknown environment variables are errors, not silently
ignored defaults. Configuration problems exit with status 2 and print an
actionable message.

### Settings

| Section | Key | Default | Description |
|---|---|---|---|
| top level | `phone_number` | empty | Destination in E.164 form. Optional for `doctor` and `selftest`; required for `dial`/`run` unless `--number` is passed. Also the default destination for MCP `place_call` and A2A `SendMessage` when no number is supplied. |
| `audio` | `bluetooth_address` | required | Phone A's Bluetooth address, six colon-separated hex octets, for example `AA:BB:CC:DD:EE:FF`. |
| `audio` | `node_timeout_s` | `30.0` | Maximum wait for the Bluetooth SCO nodes after a call is placed. The nodes exist only while a call or SCO link is active. |
| `audio` | `sample_rate` | `16000` | Processing rate in Hz, 8000-48000. PipeWire resamples the SCO link, which is 16 kHz with mSBC or 8 kHz with CVSD. |
| `audio` | `channels` | `1` | Must be 1 in v0; the pipeline is mono. |
| `audio` | `frame_ms` | `20` | Capture frame duration. Must describe a whole number of samples. |
| `dialer` | `url` | required | LAN base URL of the helper, without a trailing endpoint path. |
| `dialer` | `token` | required | Bearer token sent with every helper request. Prefer an environment override. |
| `dialer` | `timeout_s` | `10.0` | Per-request timeout. |
| `dialer` | `retries` | `2` | Retries for 5xx and connection failures only, 0-5. 4xx is never retried. |
| `inbound` | `poll_interval_s` | `1.0` | Seconds between helper `/state` polls for the `inbound` command, 0.2-30. |
| `inbound` | `answer_delay_s` | `0.0` | Grace period after a `ringing` state before asking the helper to answer, 0-30. The state is re-checked first. |
| `brain` | `kind` | `echo` | `echo` or `http`. |
| `brain` | `base_url` | empty | Required for `http`; API root up to `/v1`. |
| `brain` | `api_key` | empty | Optional for local OpenAI-compatible servers. |
| `brain` | `model` | empty | Required for `http`. |
| `brain` | `system_prompt` | built-in prompt | Used by `HttpBrain`; an empty value falls back to the built-in prompt. |
| `brain` | `max_tokens` | `200` | Maximum reply length requested from the provider. |
| `brain` | `timeout_s` | `30.0` | Per-request timeout. |
| `brain` | `action_mode` | `text` | `text` wraps the normal reply brain and never consults. `json` requires `kind = "http"` and asks the model for one JSON action per turn. |
| `realtime` | `kind` | `off` | `off` keeps the text pipeline. `grok` selects the full-duplex Grok Voice speech-to-speech adapter and bypasses `[brain]`, `Transcriber`, and `Synthesizer` at runtime. |
| `realtime` | `base_url` | `wss://api.x.ai/v1/realtime` | WebSocket endpoint without the model query parameter. |
| `realtime` | `api_key` | empty | Optional xAI key. Prefer `XAI_API_KEY` or a service `EnvironmentFile` outside the repository. |
| `realtime` | `model` | `grok-voice-latest` | Realtime model; pin a version such as `grok-voice-3.0` for reproducibility. |
| `realtime` | `voice` | `eve` | Built-in voice id or a custom xAI voice id. |
| `realtime` | `instructions` | built-in prompt | Optional system prompt. Empty uses the concise telephone prompt. |
| `realtime` | `greeting` | empty | Optional opening line requested once after the call audio backend is ready. Empty waits for the callee to speak first. |
| `realtime` | `language` | empty | Optional BCP-47 transcription hint. Malayalam is not in xAI's documented realtime list as of 2026-10. |
| `realtime` | `reasoning_effort` | `none` | `none` minimizes latency; `high` enables model reasoning. |
| `realtime` | `vad_threshold` | `0.5` | Server VAD speech probability, 0-1. This is not the dBFS `agent.vad_threshold`. |
| `realtime` | `prefix_padding_ms` | `300` | Server VAD audio retained before speech onset. |
| `realtime` | `silence_duration_ms` | `500` | Server VAD silence before end of turn. |
| `realtime` | `idle_timeout_ms` | `0` | Server re-engagement delay after a response; `0` disables it. |
| `agent` | `schedule` | empty | Reserved for an external scheduler. The runtime does not read it in v0. |
| `agent` | `max_call_seconds` | `300` | Hard call duration limit, counted from the start of dialing. |
| `agent` | `silence_timeout_s` | `20.0` | Hang up after this much silence with no active utterance. Ringing time counts. |
| `agent` | `vad_threshold` | `-40.0` | Speech threshold in dBFS, between -100 and 0. Tune on the device. |
| `agent` | `language` | `en` | BCP-47 tag reserved for speech adapters. |
| `agent` | `consult_timeout_s` | `8.0` | How long a mid-call consultation may pause the call for the requester's answer. |
| `agent` | `holding_phrase` | `One moment please.` | Spoken before the call pauses for the requester's answer. |
| `server` | `host` | `127.0.0.1` | Bind address for both listeners. A non-loopback value also requires `public_url`. |
| `server` | `port` | `8765` | MCP Streamable HTTP port. |
| `server` | `a2a_port` | `8766` | A2A JSON-RPC port. Must differ from `port`. |
| `server` | `token` | empty | Shared bearer token for the MCP and A2A RPC endpoints. Required by `serve`; at least 8 characters when set. Prefer the environment override. |
| `server` | `public_url` | empty | Externally reachable base URL used in the A2A Agent Card and in the MCP transport's allowed hosts. Required when `host` is not loopback. |
| `server` | `audit_log` | empty | Optional append-only JSONL path for `CallService` task events. |
| `transcript` | `enabled` | `false` | Write one local JSONL transcript per call for both `run` and `serve`. Off by default; when false, no directory or file is created. |
| `transcript` | `directory` | `runtime/transcripts` | Directory for transcript files. Created with mode 0700; each file is mode 0600. A relative path uses the process working directory. |

For Bluetooth HFP, the audio section looks like this:

```toml
[audio]
bluetooth_address = "AA:BB:CC:DD:EE:FF"
node_timeout_s = 30.0
sample_rate = 16000
channels = 1
frame_ms = 20
```

Example TOML values are placeholders only. A real phone number, helper token, or
API key must not enter the repository.

## Commands

Run these from `agent/` with the virtual environment active, or use the absolute
console-script path from systemd. From the repository root, `make doctor` and
`make run` do the same with the root virtual environment.

### `doctor`

```bash
telephone-agent --config ../config/config.toml doctor
```

This loads the configuration, checks the Bluetooth HFP audio path, and calls the
helper's `/health` endpoint. It checks that `pw-record`, `pw-play`, and
`pw-dump` are installed, then looks for the SCO nodes; when the phone is idle,
it reports that the nodes are not present and does not treat that as a failure.
Run it during a call to see the downlink and uplink node names. It never prints
the bearer token or API key. Exit status is 0 when every check passes, 1 when a
hardware, audio, or helper check fails, and 2 for a configuration error.

### `selftest`

```bash
telephone-agent --config ../config/config.toml selftest
```

Records two seconds from the Bluetooth HFP call downlink and reports frame
count, duration, RMS, dBFS, and peak amplitude. It reads the SCO source, which
exists only during a call; the command waits up to `audio.node_timeout_s` for
the link, so place a call first. It does not place a call and does not play
audio. Digital silence suggests a muted microphone, a call that is not routed
to the Bluetooth host, or a SCO link that is not up. This only tests the host's
capture path; it does not prove Android is routing call audio to the headset.

For a manual Bluetooth check during a supervised call, use
`scripts/bluetooth-audio-test.sh list`, `record`, and `tone`; see
[Bluetooth HFP](../docs/bluetooth-hfp.md), section 8.

### `dial`

```bash
telephone-agent --config ../config/config.toml dial --number +15555550100
```

Asks the helper to place one call and exits. The helper owns the call after
that. This is useful for testing call control without the conversation loop.
`--number` overrides `phone_number`; both are validated as E.164.

### `run`

```bash
telephone-agent --config ../config/config.toml run --number +15555550100
```

Places one call, captures audio, runs the energy VAD, sends detected utterances
to the brain, and hangs up at the silence limit, the call limit, an audio
failure, or when interrupted with Ctrl-C. The process is one-shot; scheduling is
external.

In v0 this command runs in **scaffold mode** unless you add speech adapters or
set `realtime.kind = "grok"`. In scaffold mode it detects utterances, sends a
clearly marked placeholder transcript to the brain, and logs the reply instead
of speaking it. `EchoBrain` is the default, so a complete run demonstrates call
control, VAD, and brain plumbing without pretending to be a voice conversation.
See the next section to make it audible.

The exit status is 0 for a completed call, 1 for a failed call or an interrupt,
and 2 for a configuration error.

### `inbound`

```bash
telephone-agent --config ../config/config.toml inbound
telephone-agent --config ../config/config.toml inbound --once
```

Polls the helper's `GET /state` endpoint every `inbound.poll_interval_s` and
answers every `ringing` call with `POST /answer`. After a successful answer it
opens one Grok Voice realtime session on the call, then waits for the helper to
report `idle` before re-arming. A call that is already `offhook` is never
answered, because the user may have answered it manually. `--once` handles at
most one call and exits.

This command requires `realtime.kind = "grok"`; the text pipeline has no inbound
audio path, and the command exits with status 2 otherwise. SIGTERM and SIGINT
set a stop event that is passed to the active session, which ends the call and
asks the helper to hang up. Helper failures are logged and retried with backoff;
the watcher does not exit.

There is **no caller ID** and no allowlist: the helper does not request
`READ_CALL_LOG` and never returns a caller number. Every ringing call on the
agent SIM is answered. Use a SIM dedicated to the agent and read
[Inbound calls](../docs/inbound.md) before enabling it. Inbound and outbound
calls are mutually exclusive on one phone.

### `--help`

```bash
cd agent
python -m telephone_agent --help
```

The same help is available from the installed console script.

## Running the interop server

The agent can be driven by other agents over MCP and A2A from one process, with
both listeners sharing a single in-process `CallService` queue. Install the
optional protocol dependencies and run the `serve` command:

```bash
cd agent
pip install -e ".[server]"
cd ..
telephone-agent --config config/config.toml serve
```

`serve` requires `[server].token` (or `TELEPHONE_AGENT_SERVER__TOKEN`); it
refuses to start without one and never prints the token. The process blocks
until Ctrl-C and exposes:

| Endpoint | URL |
|---|---|
| MCP Streamable HTTP | `http://<server.host>:<server.port>/mcp` |
| A2A JSON-RPC | `http://<server.host>:<server.a2a_port>/a2a` |
| A2A Agent Card | `http://<server.host>:<server.a2a_port>/.well-known/agent-card.json` |
| Health, both listeners | `/health` |

Every route except `/health` and the A2A Agent Card requires
`Authorization: Bearer <server.token>`. Calls run one at a time, so a second
request waits in the queue. Task state is held in memory and is lost on
restart. `server.host` defaults to `127.0.0.1`, which only accepts connections
from the same host. If you bind a LAN address, also set `server.public_url` so
the A2A Agent Card advertises a URL that a requester can reach; the command
refuses to start otherwise.

The token is admin-level. There is no destination allowlist, per-requester
budget, rate limit, or queue-depth limit, and no requester identity: any token
holder can place a call to any valid E.164 number and can list, read, answer, or
cancel every task. Keep the listener on loopback or a trusted network until
those controls exist, and do not port-forward it. See
[docs/interop.md](../docs/interop.md) for the protocol contract and security
model.

Without STT/TTS adapters, a delegated call uses placeholder transcripts and logs
replies instead of speaking. Set `realtime.kind = "grok"` to delegate the voice
path to the realtime adapter instead. The control plane still places, tracks,
and cancels the call.

MCP clients call the five tools `place_call`, `get_call`, `answer_call`,
`cancel_call`, and `list_calls`. A2A clients speak protocol 1.0, send
`A2A-Version: 1.0`, and use `SendMessage`/`GetTask`/`CancelTask`. A new call can
carry a text part with the objective, or a data part such as
`{"objective": "...", "number": "+15555550100"}`; `to` is accepted as an alias
for `number`.

The optional `[server].audit_log` appends one JSON object per task event with
`ts` (UTC ISO-8601), `at` (monotonic), `kind`, `task_id`, and `data`. The final
`finished` record drops the objective and full transcript, and the destination
is masked, but per-turn `transcript` and `reply` events do contain text. Apply a
retention rule and restrict access.

## Local call transcripts

Set `[transcript].enabled = true` to persist one JSONL file per call from
`telephone-agent run`, `telephone-agent inbound`, and every delegated call
handled by `telephone-agent serve`. The default is off, and when off the runtime
creates neither the directory nor a file. `directory` defaults to
`runtime/transcripts`, relative to the process working directory; `runtime/` is
already gitignored.

Files are named `YYYYmmddTHHMMSS-<short-id>.jsonl`, using local wall-clock time
at the start of the call and the first eight safe characters of the call or task
id. The directory is created with mode `0700` and each file is opened
append-only with mode `0600`. The logger is best-effort: if it cannot create or
write the file, it logs a warning and disables transcript writes for that call
without interrupting the call.

Every line is one UTF-8 JSON object with `ts` (ISO 8601 wall-clock time in the
local time zone, millisecond precision), `call_id`, and `event`. The current
events are:

| `event` | Additional fields |
|---|---|
| `call_start` | `direction` (`outbound` or `inbound`), `number` (full E.164 outbound; empty inbound because caller ID is unavailable), `audio_backend`, `realtime_kind`, `objective` when non-empty, and `realtime_model`/`realtime_voice` for a Grok realtime call |
| `transcript` | `role` (`callee`) and verbatim `text` |
| `reply` | `role` (`agent`) and verbatim `text` |
| `call_end` | `state`, `reason`, `turns`, and `duration_s` |

Example:

```json
{"ts":"2026-10-24T09:15:02.123+05:30","call_id":"8f3c...","event":"call_start","direction":"outbound","number":"+15555550100","audio_backend":"pipewire","realtime_kind":"grok","objective":"Confirm the appointment.","realtime_model":"grok-voice-3.0","realtime_voice":"ara"}
{"ts":"2026-10-24T09:15:08.406+05:30","call_id":"8f3c...","event":"transcript","role":"callee","text":"Hello?"}
{"ts":"2026-10-24T09:15:09.115+05:30","call_id":"8f3c...","event":"reply","role":"agent","text":"Hi, this is the telephone agent."}
{"ts":"2026-10-24T09:15:31.900+05:30","call_id":"8f3c...","event":"call_end","state":"idle","reason":"completed","turns":1,"duration_s":29.7}
```

The schema is extensible: consumers should ignore unknown events and fields.
Audio is never written. The local transcript is intentionally complete and is
**not redacted**: it contains the full phone number, full conversation text, and
call metadata, while operational stdout/journald logging remains masked.
Transcript text and the objective are personal data. Keep `directory` on the
local device, restrict access to the service user, and apply a retention or
deletion policy appropriate to the deployment. Do not include transcript files
in backups that leave the device.

## Plugging in STT, TTS, and an LLM

The v0 runtime separates three seams:

| Seam | Location | Contract |
|---|---|---|
| Brain | `telephone_agent/brain.py` | `reply(transcript: str) -> str`, optional `reset()`. |
| Transcriber | `telephone_agent/pipeline.py` | `transcribe(pcm: bytes, sample_rate: int) -> str`. |
| Synthesizer | `telephone_agent/pipeline.py` | `synthesize(text: str, sample_rate: int) -> bytes`. |

PCM is signed 16-bit little-endian, mono, at `audio.sample_rate` (16 kHz by
default). The transcriber receives one endpointed utterance; the synthesizer
returns the complete reply audio. Neither adapter may write audio to disk.

Wire implementations in `telephone_agent/__main__.py::_build_session` for the
one-shot `run` command and in `_build_session_factory` for `serve`, or call the
library directly:

```python
from telephone_agent.pipeline import CallSession


class MyTranscriber:
    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        ...  # call the provider with raw PCM


class MySynthesizer:
    def synthesize(self, text: str, sample_rate: int) -> bytes:
        ...  # return raw PCM, not WAV


session = CallSession(
    audio=audio,
    dialer=dialer,
    brain=brain,
    number="+15555550100",
    transcriber=MyTranscriber(),
    synthesizer=MySynthesizer(),
)
result = session.run()
```

Keep provider HTTP calls in the standard library if you want to preserve the
zero-runtime-dependency property. A provider SDK can be added behind an optional
extra, but it should not become an import of the core package. New adapter
modules beside `pipeline.py` are the right place; do not put provider logic in
`config.py` or the CLI.

`HttpBrain` is OpenAI-compatible and sends `stream: false`. That request shape
does not meet the latency budget in the design brief. For production:

- Use a streaming brain that exposes token or sentence chunks. Sentence chunks
  are the natural unit for TTS.
- Start synthesis on the first sentence instead of waiting for the full reply.
- Add streaming STT partials only after the pipeline can cancel a pending turn.
- Barge-in needs non-blocking playback. The extension point and the required
  steps are marked with a `TODO(v0.2)` in `CallSession._speak`; the audio layer
  would also need a `stop_playback()` method around the `pw-play` process.

Without those changes the pipeline is utterance-at-a-time and cannot stop
speaking when the caller interrupts.

## Grok Voice realtime adapter

`telephone_agent/grok_realtime.py` is an independent adapter for xAI's
[Grok Voice speech-to-speech API](https://docs.x.ai/developers/model-capabilities/audio/voice-agent).
It is not built on `Brain`, `Transcriber`, or `Synthesizer`. One WebSocket
session performs speech recognition, turn detection, the conversation model,
and speech synthesis, and the adapter streams the call audio in both directions.

Enable it in the TOML file:

```toml
[realtime]
kind = "grok"
model = "grok-voice-latest"
voice = "eve"
# Optional: spoken once after the call audio is ready. Empty waits for the
# callee to speak first.
greeting = "Hello, this is the telephone agent calling about your appointment."
# Optional: prefer XAI_API_KEY in the service environment instead.
# api_key = ""
```

Install the optional client and export the key outside the repository:

```bash
cd agent
pip install -e ".[realtime]"
export XAI_API_KEY='...'   # or use systemd EnvironmentFile=
```

The key is never logged. The example configuration leaves `api_key` empty; prefer
`XAI_API_KEY` or a systemd `EnvironmentFile` outside the repository. `doctor`
reports whether a key is configured without printing it:

```bash
telephone-agent --config ../config/config.toml doctor
```

The adapter speaks 16-bit PCM over the WebSocket's binary transport at
`audio.sample_rate`. Server-side VAD supplies end-of-turn detection, and
`input_audio_buffer.speech_started` stops local playback immediately, so barge-in
does not depend on the local energy detector. The model can call two tools:
`end_call` (after saying goodbye) and, on a delegated `serve` call, `consult`
(the same `ConsultationBroker` used by the deliberative text brain). The call
objective from MCP/A2A is included in the system instructions.

When `realtime.greeting` is set, the adapter requests one opening turn from the
model immediately after `audio.start()` succeeds, before it enters the audio
frame loop. That ordering matters: for Bluetooth the SCO nodes exist only once
the call is active, and speaking earlier would synthesize audio into a dead
uplink. The request adds a system-role conversation item asking the model to say
the configured line, followed by `response.create`; it is sent once per session.
This lets scheduled outbound calls open the conversation instead of waiting for
the callee. The inbound path in `__main__.py` builds the same adapter with
`place_call=False`; it reuses the greeting ordering after its own audio setup
and still asks the helper to hang up when the session ends. A blank `greeting`
sends no events and preserves the listen-first behavior.

Caveats:

- **Language coverage.** xAI's documented realtime languages do not include
  Malayalam as of 2026-10. Hindi, Bengali, and English are documented; test
  Malayalam-English code-mixing before relying on it.
- **Audio rate.** The configured `audio.sample_rate` must be one of 8000, 16000,
  22050, 24000, 32000, 44100, or 48000 Hz. The 16 kHz default is accepted and
  avoids resampling.
- **Cost and privacy.** Speech-to-speech is billed per minute of audio and call
  audio leaves the LAN for xAI. The adapter does not write audio to disk.
- **No fallback.** If the WebSocket cannot be established, the call ends as a
  failed turn after the helper is asked to hang up. There is no local speech
  fallback in this adapter.

## systemd

The one-shot `run` command places one call and exits, so use a systemd **user** timer
rather than `Restart=always`. Restarting a command that dials would redial after every
exit. The timer is the scheduler in v0; `agent.schedule` in the TOML file is reserved for
a future scheduler and is not read. The `serve` command is the long-lived alternative and
should run as a normal user service if you use the control plane instead of the timer.

PipeWire and WirePlumber run in the user session. A system service, even one that runs as
the same account, has a different session bus and `XDG_RUNTIME_DIR` and cannot see the
SCO nodes. Run the agent as a systemd **user** service in the account that owns the
PipeWire session.

The repository ships user units in `scripts/systemd/`:
`telephone-agent-bluetooth.service` and `telephone-agent-bluetooth.timer`. They assume the
repository is at `~/android-telephone-agent` and use the `agent/.venv` console script;
edit the `%h`-based paths (`WorkingDirectory`, `EnvironmentFile`, and `ExecStart`) if yours
are elsewhere. Install them under the user unit directory and enable the timer:

```bash
mkdir -p ~/.config/systemd/user
install -m644 scripts/systemd/telephone-agent-bluetooth.service \
  ~/.config/systemd/user/
install -m644 scripts/systemd/telephone-agent-bluetooth.timer \
  ~/.config/systemd/user/
sudo loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now telephone-agent-bluetooth.timer
journalctl --user -u telephone-agent-bluetooth.service -n 100 --no-pager
```

Enable lingering on a headless host so the user manager and PipeWire keep
running when no one is logged in. Do not enable two outbound timers for the
same call slot. The full deployment steps are in
[Bluetooth HFP](../docs/bluetooth-hfp.md), section 10.

The inbound watcher has its own long-lived user unit,
`scripts/systemd/telephone-agent-inbound.service`. It uses the root
`.venv/bin/telephone-agent`, shares the user's PipeWire/WirePlumber session, and
restarts on failure because it polls for calls instead of dialing at startup.
Inbound and outbound calls are mutually exclusive: do not enable the inbound
unit together with an outbound timer on the same phone. Installation and
privacy limits are in [Inbound calls](../docs/inbound.md).

The design brief targets 24/7 operation with a host watchdog and optional volatile
journald storage. Those are host-level settings; the one-shot user unit does not enable
them. Do not add `Restart=always` until a scheduler guarantees one call per slot and
idempotent retries.

## Testing

From the repository root:

```bash
python3 -m pytest agent/tests -q
```

The tests do not need a sound card, a phone, or network access. `urllib.request`
and subprocesses are mocked in the dialer and brain tests. With development
dependencies installed:

```bash
make test
make lint
```

`make lint` runs `ruff check` and `ruff format --check` over `agent/`. The CI
workflow runs `ruff check` and `pytest` on Python 3.11, 3.12, and 3.13. Python
3.13 does not provide the removed `audioop` module; the runtime computes RMS with
`array` instead.

## Limitations

- **The text pipeline has no STT or TTS adapters.** `run` and `serve` use
  placeholder transcripts and log replies on the default path. The independent
  `realtime.kind = "grok"` adapter provides full-duplex speech instead.
- **No control-plane access controls.** `serve` has one shared admin token and
  no destination allowlist, per-requester budget, requester identity, rate
  limit, or queue-depth limit. Keep it on a trusted network.
- **A2A consultation timeouts are stale.** If a consultation times out while
  the A2A task is `TASK_STATE_INPUT_REQUIRED`, the task stays in that state until
  the requester sends another message.
- **No barge-in on the text pipeline.** Playback blocks capture there. The
  Grok Voice adapter is full-duplex and stops playback on server VAD speech
  onset.
- **Energy VAD, not a speech model, on the text pipeline.** Fan noise, road
  noise, or a loud room can hold a turn open. Tune `agent.vad_threshold` and the
  endpointing constants in `vad.py` for the deployment. The realtime adapter
  uses Grok's server VAD instead.
- **No outbound answer detection.** For outbound calls the helper is assumed to
  return when the call is placed, not when it is answered. Ringing time counts
  against `agent.silence_timeout_s` and `agent.max_call_seconds`. The inbound
  watcher does use the helper's `/state` endpoint and answers ringing calls, but
  outbound calls still need a separate answer signal for production use.
- **No scheduler or durable idempotency.** One `run` is one call. The dialer
  retries 5xx and connection failures with an idempotency key, but the helper
  must honour that header to avoid a duplicate call.
- **No streaming.** `HttpBrain` waits for a complete completion, so the latency
  budget in the design brief is not met.
- **No recording.** Audio is streamed through memory and is not persisted. Add
  recording only with consent and a retention policy; it is out of scope here.
- **Hangup may be unavailable.** Android restricts `TelecomManager.endCall()` on
  modern releases. If the helper cannot end the call, the remote party must hang
  up and the service cannot guarantee prompt teardown.
- **Inbound auto-answer has no caller ID or screening.** The helper does not
  request `READ_CALL_LOG`, so `/state` and `/answer` expose no number. Every
  ringing call on the agent SIM is answered; use a dedicated SIM. See
  [Inbound calls](../docs/inbound.md).
- **Bluetooth HFP requires PipeWire.** The agent must run in the same user
  session as PipeWire and WirePlumber, and the SCO nodes exist only during a
  call. A system service without access to that session cannot use the audio
  path.
- **Bluetooth validation is limited.** The path was validated on a Linux laptop
  with a Galaxy A23 over outbound PSTN calls and one live inbound PSTN call.
  Latency has not been measured, and HFP call control is not implemented. The link
  was weak (RSSI about -87 dBm); the first call dropped during setup and needed a
  manual reconnect, and later calls were stable.
- **No HFP call control.** The agent does not send `ATD`, `ATA`, or `CHUP`.
  Dialing and hangup still require the LAN helper and a reachable phone.
- **The cellular codec caps fidelity.** AMR-NB, AMR-WB, or EVS quality is the
  limit regardless of the SCO link's 16 kHz mSBC rate. Optimise for turn-taking
  and latency, not hi-fi.
- **Local speech models need a larger host.** Local speech models are not
  comfortable on a low-power machine; the design brief treats Whisper/Piper-class
  local models as a stretch goal for a desktop or mini PC.

## Troubleshooting

Start with the least invasive checks:

```bash
telephone-agent --config ../config/config.toml doctor
telephone-agent --config ../config/config.toml selftest
journalctl --user -u telephone-agent-bluetooth.service -n 100 --no-pager
```

| Symptom | Check |
|---|---|
| Exit status 2 | The message names the bad key or environment variable. Compare with `config/config.example.toml`; unknown keys are rejected on purpose. |
| `pw-record` or `pw-play` not found | Install PipeWire's command-line tools (the `pipewire-bin` package on Debian and Ubuntu), then rerun `doctor`. |
| `pw-dump` cannot connect to PipeWire | The agent is not in the PipeWire user session. Run it as the same user, use a user service, and enable lingering; check `XDG_RUNTIME_DIR`. |
| `doctor` says Bluetooth SCO nodes are not present | This is normal while the phone is idle. Place a call, confirm the phone routes call audio to Bluetooth, and rerun during the call. |
| Bluetooth node target is missing after reconnect | The `.<id>` suffix changed. Do not hard-code it; the runtime discovers nodes by address and factory name. Use `pw-dump` or `scripts/bluetooth-check.sh` to see the current names. |
| `selftest` reports digital silence | No call is active, the phone is not routing call audio to the Bluetooth host, or the SCO link is not up. Test the route during a call before changing the agent. |
| Helper returns 401/403 | The bearer token does not match. Set `TELEPHONE_AGENT_DIALER__TOKEN` and restart the service. |
| Helper returns 404 | The helper does not implement the reference endpoint path. Check `/health`, `/call`, and `/hangup` in `telephone_agent/dialer.py`. |
| Helper returns 5xx | The helper app or phone is unhealthy. The dialer retries a bounded number of times, then fails. Check the helper's logs and the phone's battery optimisation setting. |
| Brain returns 401/404 | Check `brain.base_url` (include the API version, usually `/v1`), `brain.model`, and the API key. |
| Call ends immediately | Ringing or unanswered time can trip `silence_timeout_s`; there is no answer detection in v0. Check the helper's call log and the phone's signal. |
| `run` logs replies but nothing is heard | Expected when no synthesizer is configured. Wire a TTS adapter; see the adapter section. |
| `run` detects speech but the echo is a placeholder | Expected when no transcriber is configured. Wire an STT adapter. |
| Agent talks over the caller | Barge-in is not implemented in v0. The TODO in `CallSession._speak` is the extension point. |
| High latency | Non-streaming brain and TTS wait for complete responses. See the streaming notes and the design brief's latency budget. |

If `doctor` passes but audio still fails, verify the Android-side routing first;
the host can only testify about its own PipeWire path.

## Security and privacy

Call audio is sent to third-party STT/TTS providers when adapters are wired, and
transcripts are sent to the LLM. Choose providers and regions accordingly, do
not persist audio, and disclose the processing to call participants where
required. The helper endpoint stays on the LAN, binds to a private interface, and
requires a bearer token; do not port-forward it. Phone numbers are masked in logs
and scrubbed from helper error bodies, but provider responses and provider-side
logs are outside this runtime's control.

Outbound calling is regulated. In India, TRAI's TCCCPR rules apply to commercial
and unsolicited calls; in the US, the TCPA and FCC rules on AI-generated voices
apply. Use this project for personal calls to people who consented. Do not use
it for spam, impersonation, or caller-ID evasion. See [SECURITY.md](../SECURITY.md).

## Repository layout

```text
agent/
  pyproject.toml          packaging, console script, ruff and pytest settings
  README.md               this file
  telephone_agent/
    __main__.py           doctor, dial, run, inbound, serve, selftest
    config.py             TOML loading, environment overrides, validation
    audio.py              pw-record/pw-play backend and Bluetooth SCO discovery
    vad.py                RMS, dBFS, energy endpointer
    dialer.py             helper HTTP client with retries and idempotency
    inbound.py            inbound call watcher (poll, answer, one session per call)
    actions.py            Speak/Consult/HangUp actions and BrainActionAdapter
    brain.py              Brain protocol, EchoBrain, HttpBrain, HttpDeliberativeBrain
    grok_realtime.py      independent xAI Grok Voice speech-to-speech adapter
    consult.py            consultation broker for mid-call questions
    pipeline.py           CallSession state machine, consultation, adapter seams
    service.py            task queue, task state, and audit log
    servers/              MCP and A2A adapters (optional server extra)
  tests/                  stdlib-only tests; no hardware or network required
```
