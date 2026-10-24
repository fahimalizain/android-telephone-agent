# Telephone agent runtime

This directory contains the reference Python runtime for the outbound call agent
described in the [design brief](../docs/design-brief.md). It runs on the
Raspberry Pi next to Phone A, captures and plays audio through the USB Audio
Class gadget, and asks a small LAN helper to place and end the cellular call.

The runtime is deliberately small. It uses only the Python standard library at
runtime; `pytest` and `ruff` are development dependencies. It is a scaffold for
the design, not a finished conversational agent: speech-to-text (STT),
text-to-speech (TTS), scheduling, barge-in, and streaming are not bundled. The
sections below explain exactly where those pieces belong.

## Prerequisites

- Raspberry Pi Zero 2 W or Pi 4 Model B running Raspberry Pi OS Lite 64-bit
  (Bookworm or later), with the USB gadget configured and enumerating.
- Python 3.11 or later. Check with `python3 --version`.
- ALSA utilities, which provide `arecord` and `aplay`:

  ```bash
  sudo apt update
  sudo apt install -y python3-venv alsa-utils
  ```

- The phone-side helper app reachable on the LAN with a bearer token. The
  reference HTTP contract is documented in `telephone_agent/dialer.py`: `GET
  /health`, `POST /call`, and `POST /hangup`.
- Phone A configured to route call audio to the USB headset. On the phone,
  Developer options must not disable USB audio routing. Verify routing with the
  hardware loopback procedure before relying on the agent.
- For `brain.kind = "http"`, network access to an OpenAI-compatible endpoint.

The gadget's ALSA direction is inverted relative to a normal sound card.
Recording from the gadget card receives the remote party; playing to the gadget
card sends audio to Phone A's uplink. The runtime treats those operations as
"capture" and "play" and does not expose the inversion to the call loop.

## Install

For a production install on the Pi, use a virtual environment inside `agent/`:

```bash
cd /home/pi/android-telephone-agent/agent
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
export TELEPHONE_AGENT_AUDIO__CARD='UAC1Gadget'
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
| top level | `phone_number` | empty | Destination in E.164 form. Optional for `doctor` and `selftest`; required for `dial`/`run` unless `--number` is passed. |
| `audio` | `card` | required | ALSA card id from `arecord -l`, the token after `card N:` rather than the bracketed description. |
| `audio` | `device` | `0` | ALSA device index. |
| `audio` | `sample_rate` | `16000` | Processing rate in Hz, 8000-48000. `plughw` resamples the gadget's 48 kHz stream. |
| `audio` | `channels` | `1` | Must be 1 in v0; the pipeline is mono. |
| `audio` | `frame_ms` | `20` | Capture frame duration. Must describe a whole number of samples. |
| `dialer` | `url` | required | LAN base URL of the helper, without a trailing endpoint path. |
| `dialer` | `token` | required | Bearer token sent with every helper request. Prefer an environment override. |
| `dialer` | `timeout_s` | `10.0` | Per-request timeout. |
| `dialer` | `retries` | `2` | Retries for 5xx and connection failures only, 0-5. 4xx is never retried. |
| `brain` | `kind` | `echo` | `echo` or `http`. |
| `brain` | `base_url` | empty | Required for `http`; API root up to `/v1`. |
| `brain` | `api_key` | empty | Optional for local OpenAI-compatible servers. |
| `brain` | `model` | empty | Required for `http`. |
| `brain` | `system_prompt` | built-in prompt | Used by `HttpBrain`; an empty value falls back to the built-in prompt. |
| `brain` | `max_tokens` | `200` | Maximum reply length requested from the provider. |
| `brain` | `timeout_s` | `30.0` | Per-request timeout. |
| `agent` | `schedule` | empty | Reserved for an external scheduler. The runtime does not read it in v0. |
| `agent` | `max_call_seconds` | `300` | Hard call duration limit, counted from the start of dialing. |
| `agent` | `silence_timeout_s` | `20.0` | Hang up after this much silence with no active utterance. Ringing time counts. |
| `agent` | `vad_threshold` | `-40.0` | Speech threshold in dBFS, between -100 and 0. Tune on the device. |
| `agent` | `language` | `en` | BCP-47 tag reserved for speech adapters. |

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

This loads the configuration, lists capture devices from `arecord -l`, checks
that the configured card is present, and calls the helper's `/health` endpoint.
It never prints the bearer token or API key. Exit status is 0 when every check
passes, 1 when a hardware or helper check fails, and 2 for a configuration
error.

### `selftest`

```bash
telephone-agent --config ../config/config.toml selftest
```

Records two seconds from the gadget capture path and reports frame count,
duration, RMS, dBFS, and peak amplitude. It does not place a call and does not
play audio. Digital silence suggests a muted microphone, the wrong card, or a
gadget that has not enumerated. This only tests the Pi's capture path; it does
not prove Android is routing call audio to the headset.

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

In v0 this command runs in **scaffold mode** unless you add speech adapters. It
detects utterances, sends a clearly marked placeholder transcript to the brain,
and logs the reply instead of speaking it. `EchoBrain` is the default, so a
complete run demonstrates call control, VAD, and brain plumbing without
pretending to be a voice conversation. See the next section to make it audible.

The exit status is 0 for a completed call, 1 for a failed call or an interrupt,
and 2 for a configuration error.

### `--help`

```bash
cd agent
python -m telephone_agent --help
```

The same help is available from the installed console script.

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

Wire implementations in `telephone_agent/__main__.py::_build_session`, or call
the library directly:

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
  would also need a `stop_playback()` method around the `aplay` process.

Without those changes the pipeline is utterance-at-a-time and cannot stop
speaking when the caller interrupts.

## systemd

The runtime places one call and exits, so use a systemd timer rather than
`Restart=always`. Restarting a command that dials would redial after every exit.
The timer is the scheduler in v0; `agent.schedule` in the TOML file is reserved
for a future scheduler and is not read.

Place the units in `/etc/systemd/system/` and adjust `User`, paths, and the
schedule for your installation. `User=pi` is the common Raspberry Pi OS account;
verify it exists on your image.

```ini
[Unit]
Description=telephone-agent one outbound call
Wants=network-online.target sound.target
After=network-online.target sound.target

[Service]
Type=oneshot
User=pi
Group=pi
SupplementaryGroups=audio
WorkingDirectory=/home/pi/android-telephone-agent/agent
EnvironmentFile=-/home/pi/android-telephone-agent/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/pi/android-telephone-agent/agent/.venv/bin/telephone-agent --config /home/pi/android-telephone-agent/config/config.toml run
TimeoutStartSec=900
```

```ini
[Unit]
Description=telephone-agent daily call

[Timer]
OnCalendar=*-*-* 09:00:00
Persistent=true
RandomizedDelaySec=30

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now telephone-agent.timer
systemctl list-timers telephone-agent.timer
journalctl -u telephone-agent.service -n 100 --no-pager
```

The unit runs as `pi`; that user must be able to open the ALSA device (membership
in the `audio` group) and must have read access to the configuration and
environment files. `EnvironmentFile` is optional (`-` prefix), so the service
still starts if you use only the TOML file.

The design brief targets 24/7 operation with a hardware watchdog and volatile
journald storage. Those are host-level settings; the one-shot unit above does
not enable them. Do not add `Restart=always` until a scheduler guarantees one
call per slot and idempotent retries.

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

- **No STT or TTS adapters are bundled.** `run` uses placeholder transcripts and
  logs replies unless adapters are injected.
- **No barge-in.** Playback blocks capture. The caller cannot interrupt the
  agent, and the agent may talk over the caller if VAD finds an utterance during
  playback.
- **Energy VAD, not a speech model.** Fan noise, road noise, or a loud room can
  hold a turn open. Tune `agent.vad_threshold` and the endpointing constants in
  `vad.py` for the deployment.
- **No answer detection.** The helper is assumed to return when the call is
  placed, not when it is answered. Ringing time counts against
  `agent.silence_timeout_s` and `agent.max_call_seconds`. If the helper can
  report answer state, wire that in before production use.
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
- **The cellular codec caps fidelity.** AMR-NB, AMR-WB, or EVS quality is the
  limit regardless of the gadget's 48 kHz rate. Optimise for turn-taking and
  latency, not hi-fi.
- **Pi Zero 2 W is CPU constrained.** Local speech models are not comfortable on
  this board; the design brief treats Whisper/Piper-class local models as a
  Pi 4/5 stretch goal.

## Troubleshooting

Start with the least invasive checks:

```bash
arecord -l
aplay -l
telephone-agent --config ../config/config.toml doctor
telephone-agent --config ../config/config.toml selftest
journalctl -u telephone-agent.service -n 100 --no-pager
```

| Symptom | Check |
|---|---|
| Exit status 2 | The message names the bad key or environment variable. Compare with `config/config.example.toml`; unknown keys are rejected on purpose. |
| `arecord: command not found` | Install `alsa-utils`. |
| `arecord -l` shows no gadget | Check the USB gadget service, `dtoverlay=dwc2,dr_mode=peripheral`, `modules-load=dwc2,libcomposite`, the cable, and the hub. Re-enumeration after a phone reboot is a known failure mode. |
| Configured card not found | Use the ALSA card id (for example `UAC1Gadget` or `Headset`), not the bracketed description. Card ids are case-sensitive. |
| `Device or resource busy` | Another process has the card open. Stop it and rerun `selftest`. On a desktop image, suspend or stop the desktop audio stack. |
| `selftest` reports digital silence | The gadget is capturing but Phone A is not routing call audio, or the phone microphone is muted. Test the routing with USB-C earphones before changing the agent. |
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
the Pi can only testify about its own ALSA path.

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
    __main__.py           doctor, dial, run, selftest
    config.py             TOML loading, environment overrides, validation
    audio.py              arecord/aplay lifecycle through plughw
    vad.py                RMS, dBFS, energy endpointer
    dialer.py             helper HTTP client with retries and idempotency
    brain.py              Brain protocol, EchoBrain, HttpBrain
    pipeline.py           CallSession state machine and adapter seams
  tests/                  stdlib-only tests; no hardware or network required
```
