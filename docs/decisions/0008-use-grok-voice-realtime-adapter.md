# ADR 0008: Use an independent Grok Voice realtime adapter

- Status: Accepted
- Date: 2026-10-24
- Deciders: Project owner

## Context

ADR 0004 selected cloud STT, LLM, and TTS behind small interfaces. That stack is
three network stages, each with its own connection, timeout, and failure mode,
and it cannot interrupt itself cleanly unless the TTS layer is cancellable and
capture keeps running during playback.

xAI's Grok Voice realtime API exposes one full-duplex WebSocket for speech
recognition, turn detection, the conversation model, and speech synthesis. It
supports server-side VAD, binary PCM16 in both directions, and function tools.
A browser-based sample demonstrated the protocol, and the API documentation
confirms that a server-side client can authenticate with the API key directly.

The alternatives are:

1. Keep only the cascaded STT/LLM/TTS pipeline.
2. Replace the pipeline with a realtime session.
3. Add the realtime session beside the pipeline behind a configuration switch.

## Decision

Add the realtime session beside the existing pipeline as an independent adapter,
`telephone_agent/grok_realtime.py`. `realtime.kind = "grok"` selects it;
`realtime.kind = "off"` remains the default. It is a drop-in replacement for
`CallSession` at the `run` and `serve` boundaries but does not use `Brain`,
`Transcriber`, or `Synthesizer`.

The adapter streams the call downlink as binary PCM16 at `audio.sample_rate`,
plays the returned PCM through a streaming ALSA or PipeWire sink, stops playback
on `input_audio_buffer.speech_started`, and exposes `end_call` and, on delegated
calls, `consult` as function tools. The API key comes from `XAI_API_KEY` or a
`TELEPHONE_AGENT_REALTIME__API_KEY` override; the optional TOML field exists for
compatibility but should stay empty in a committed file.

## Consequences

### Positive

- One network session and one provider for the whole voice path.
- Server VAD and automatic interruption make barge-in part of the protocol.
- Binary PCM avoids base64 overhead at 50 frames per second.
- The text pipeline, its tests, and its adapter seams stay intact.
- The realtime adapter can be selected per deployment without a rewrite.

### Negative

- xAI's documented realtime languages do not include Malayalam as of 2026-10.
- The adapter adds an optional `websockets` dependency and a second code path to
  test and operate.
- Call audio leaves the LAN for xAI, and speech-to-speech is priced per minute of
  audio rather than per STT minute plus TTS character.
- There is no local speech fallback if the WebSocket cannot be established.

### Risks

- **Language regression.** Malayalam quality may be worse than a dedicated
  provider. Keep the text pipeline available and evaluate both.
- **Provider outage.** A failed WebSocket ends the call after the helper is asked
  to hang up; it does not leave the line open.
- **Latency outside India.** xAI has no India region at the time of writing.
- **Protocol drift.** Event names and session fields follow the October 2026 API;
  pin a model version and keep the protocol translation in one module.

## Alternatives considered

### Keep only the cascaded pipeline

This preserves provider flexibility and the existing tests, but keeps three
stages, three timeout budgets, and an utterance-at-a-time playback path that
cannot meet the barge-in requirement without further work.

### Replace the pipeline with realtime

This removes the STT/LLM/TTS seams and the provider-independent text path. It
would make the realtime provider mandatory and lose the ability to run the
scaffold without an xAI key.

### Add realtime as another Brain

A `Brain` receives one transcript and returns one string. It has no audio
stream, no turn event, and no cancellation path, so a realtime speech-to-speech
session cannot be represented by that interface without hiding the parts that
matter.
