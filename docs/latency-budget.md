# Latency Budget

Status: design complete, pre-implementation (alpha)
Last updated: 2026-10-24

Purpose: define the latency target for a spoken turn, show how to measure it,
and describe what to do when the measured system misses the target. Pipeline:
[voice agent](./voice-agent.md); context: [architecture](./architecture.md),
[Bluetooth HFP](./bluetooth-hfp.md), [agent/README](../agent/README.md); prompts:
[prompts](./prompts.md).

The optional Grok Voice realtime adapter (`realtime.kind = "grok"`) replaces the
STT, LLM, and TTS stages with one speech-to-speech WebSocket. Its turn gap is
dominated by provider processing and network RTT rather than the per-stage
budgets below; measure it separately with the same script and phone.

## Why latency matters

Conversation is turn-taking under tight timing. A natural gap between one
speaker finishing and the other starting is roughly 200 ms. A gap near one second
is noticeable; beyond that, callers repeat themselves, say "hello?", or assume
the line dropped. The agent does not need to be instantaneous, but it must be
predictable. A stable 900 ms response feels better than a response that is
sometimes 400 ms and sometimes 2.5 s.

Latency also changes behavior. If the agent is slow, callers interrupt more, which
triggers barge-in and discards a reply. A slow loop produces shorter, more
fragmented conversations; barge-in cannot make a consistently late agent natural.

## Definition

Measure from the end of the callee's speech to the first frame of agent audio
played to the SCO sink. Two clocks are useful:

- **Caller clock.** `t0` is the last frame classified as speech by VAD. This is
  the strict view: it includes the 300–500 ms endpointing window.
- **Pipeline clock.** `t_endpoint` is when VAD declares the turn over. This is the
  clock used by the brief's table, because STT and the LLM can begin here.

The table below reproduces the brief's budget and starts at `t_endpoint`. The
endpointing window is reported separately, not hidden inside STT.

Bluetooth SCO carries the audio between the phone and the host. The link is mSBC at
16 kHz or CVSD at 8 kHz, and the SCO scheduling and retransmission add delay, but this
document does not assign a measured value to that stage. The cellular codec and network
leg set a floor that the host cannot remove.

## Budget

| Stage | Low (ms) | High (ms) | Cumulative low (ms) | Cumulative high (ms) |
|---|---:|---:|---:|---:|
| Network to India-region provider | 30 | 80 | 30 | 80 |
| STT finalization | 150 | 250 | 180 | 330 |
| LLM time to first token | 300 | 600 | 480 | 930 |
| TTS time to first audio | 150 | 300 | 630 | 1,230 |
| Playback and buffering | 50 | 100 | 680 | 1,330 |
| **Target** | | | **< 1,200 (brief)** | **p95 < 1,800 (design)** |

The brief's target is a total under 1.2 s. The arithmetic shows why that is a
typical-case target: the high end reaches 1.33 s before endpointing. Treat 1.2 s
as the p50 target and 1.8 s as the p95 ceiling; if p95 exceeds 1.8 s, the agent
will feel unreliable even when its median is good.

| View | Start | Low (ms) | High (ms) |
|---|---|---:|---:|
| Endpointing | Last voiced frame | 300 | 500 |
| Response gap | Endpoint decision | 680 | 1,330 |
| Caller-perceived gap | Last voiced frame | 980 | 1,830 |

Do not compare a response gap measured at `t_endpoint` with a caller-perceived
number. State which clock was used.

## How to measure

Use one monotonic clock on the host for all local stages. Wall-clock time and NTP
adjustments must not enter the per-turn measurement. Mark these instants:

- `t_last_voiced`: last frame above the VAD threshold.
- `t_endpoint`: end-of-turn decision, after the endpointing window.
- `t_stt_final`: final transcript received.
- `t_llm_first`: first content token received.
- `t_tts_first_byte`: first audio byte or chunk received.
- `t_first_frame`: first PCM frame written to the SCO sink.

Also record `t_barge_in` and `t_playback_stop` for interruption tests. Each turn
gets a `call_id` and `turn_id`; each line is JSON. Do not log audio or full
transcripts in the timing log.

```python
import json
import time


class TurnTrace:
    def __init__(self, call_id: str, turn_id: int) -> None:
        self.call_id = call_id
        self.turn_id = turn_id
        self.t0 = time.monotonic()

    def mark(self, stage: str) -> None:
        now = time.monotonic()
        print(json.dumps({
            "call_id": self.call_id,
            "turn_id": self.turn_id,
            "stage": stage,
            "t_ms": round((now - self.t0) * 1000, 1),
        }), flush=True)
```

Create one trace at `t_last_voiced` and mark `endpoint`, `stt_final`,
`llm_first`, `tts_first_byte`, and `first_frame`. The first-frame delta is the
caller-perceived gap for that turn. Subtract the endpoint delta to get the
pipeline-clock gap.

Compute percentiles over turns, not over calls:

```python
import statistics

gaps = [...]  # first_frame_ms, one per turn
cut = statistics.quantiles(gaps, n=100)
p50, p95 = cut[49], cut[94]
```

Use at least 20 calls and 60 turns after warm-up. Report p50 and p95 separately
for the first turn of a call and for later turns; the first turn often pays for
TLS, DNS, model warm-up, and TTS connection setup. A single average hides the
tail that callers actually notice.

## Common causes of delay

| Cause | Symptom | Mitigation |
|---|---|---|
| Non-streaming TTS | First audio waits for the whole reply | Use a streaming endpoint and sentence chunks |
| Waiting for LLM completion | First token arrives early but audio starts late | Stream tokens; do not await the full message |
| STT finalization | Long wait after the callee stops | Explicit finalize, 250 ms cap, partial fallback |
| TCP and TLS setup | First turn much slower than later turns | Pre-warm at boot, keep-alive, TLS session reuse |
| Wi-Fi power save | Periodic 100–300 ms spikes | Disable power save; accept higher idle power |
| Large audio buffers | Smooth audio but sluggish replies | 20 ms frames; 40–80 ms playback buffer |
| Bluetooth coexistence | Jitter or dropouts on the 2.4 GHz link | Keep the host and phone close, use a clean channel |
| Distant provider region | Constant 200–250 ms penalty | Use an India endpoint |
| Slow DNS | Occasional multi-second first connect | Cache, pre-resolve, avoid blocking the audio loop |

## Tuning checklist

- Pre-warm provider connections at boot and after each call. Warm TLS and HTTP/2,
  but do not open a billable STT stream until the call is answered. Keep
  connections alive with the provider's ping interval.
- Chunk TTS at sentence boundaries, with a short first chunk. Do not split names,
  numbers, times, or currency.
- Cap LLM output at 80–120 tokens and stop after one question. A shorter reply
  reaches TTS sooner and is easier to interrupt.
- Prefer India regions for STT and TTS. Test from the actual Wi-Fi network, not an
  office connection.
- Use 20 ms audio frames. Larger frames reduce syscall overhead but add latency.
- Disable Wi-Fi power save: `sudo iw dev wlan0 set power_save off`. Verify with
  `iw dev wlan0 get power_save`. The trade-off is higher idle power; the host is
  mains-powered, so latency wins for this application. Make the setting persistent
  and re-check it after a reboot or driver update.
- Set `TCP_NODELAY` on provider sockets. Avoid tiny writes that wait for Nagle.
- Measure p95, not only p50, and segment by first turn versus later turns.

## Degradation policy

When the measured gap exceeds the target, do not fill the line with silence.
Change the response strategy:

| Condition | Response |
|---|---|
| p95 above 1.2 s but below 1.8 s | Shorten replies; skip optional tool calls; keep one question per turn |
| First token over 800 ms | Play a short acknowledgement from a pre-synthesized cache, then answer |
| TTS first audio over 1.5 s | Use a backup voice or a cached line; do not wait indefinitely |
| STT final over 500 ms | Use the latest partial; confirm names, times, and numbers |
| Provider failure | Use the fallback phrase and end the call gracefully |

Canned acknowledgements ("Mm-hmm", "Right", "One moment") buy time without
pretending to answer. Use them once per turn, not as a loop; pre-synthesize them
with the same voice. If the agent cannot produce audio, hang up instead of
leaving an open line.

## Verification

Run the same script 20 times on the target host, phone, Wi-Fi network, and provider
configuration. Fill in the measured column; do not copy expected values.

| Metric | Expected | Measured | Notes |
|---|---:|---:|---|
| Endpointing window | 300–500 ms | | Per turn |
| STT finalization | 150–250 ms | | After endpoint |
| LLM first token | 300–600 ms | | p50 |
| TTS first audio | 150–300 ms | | p50 |
| First playback frame | 50–100 ms | | After TTS byte |
| Response gap, p50 | < 1,200 ms | | From endpoint |
| Response gap, p95 | < 1,800 ms | | From endpoint |
| Caller-perceived gap, p50 | < 1,500 ms | | Includes endpointing |
| Barge-in stop | < 100 ms | | Detection to silence |
| First-turn penalty | < 300 ms | | Versus later turns |

Record the date, host, adapter, phone, codec, network, provider, and model with the
results; a latency number without that context is not reproducible.

## Measurement limitations

- No end-to-end latency measurement exists for this project yet, including the validated
  Bluetooth run. The table above is a target, not a result.
- Twenty calls is a small sample. It is enough to catch gross regressions, not to
  estimate a rare tail. Increase the sample for a release decision.
- Cellular codec and network conditions vary by carrier and location. Measure on
  the SIM and network the agent will actually use.
- Bluetooth codec negotiation varies. State whether a run used mSBC or CVSD; a CVSD run
  is not comparable to an mSBC run.
- Provider latency can change without notice. Re-run the check after a provider
  or model change, and keep a dated baseline.
- These numbers measure the agent's response, not the callee's perception. A
  short, well-placed acknowledgement can make a 1.2 s gap feel shorter than a
  silent 800 ms gap.
