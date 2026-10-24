# ADR 0004: Use a cloud voice stack first

- Status: Accepted
- Date: 2026-10-24
- Deciders: Project owner

## Context

The conversational loop needs streaming STT, an LLM, and streaming TTS within a
latency budget of roughly 1.2 seconds from the end of the callee's utterance to the
start of the agent's speech. The Zero 2 W has 512 MB of RAM and a modest quad-core
CPU. It can move PCM and run VAD, but it cannot comfortably run Whisper.cpp and Piper
alongside the audio bridge.

The project also needs Malayalam and Indian English, or code-mixed speech. Quality
varies by vendor, and code-mixed Malayalam-English is harder than either language
alone. The brief names Sarvam AI, Azure Speech in Central or South India, and Google
Cloud in Mumbai as candidates. Endpoints outside India add roughly 200-250 ms of
round-trip time, which consumes a large part of the budget before any model runs.

The alternatives are a cloud stack, a fully local stack, or a hybrid. Each trades
latency, quality, cost, privacy, and operational effort differently.

## Decision

The reference runtime uses cloud STT, LLM, and TTS, preferring providers in an India
region. Providers sit behind small interfaces so they can be swapped without changing
the call state machine or audio bridge. Audio is streamed, not written to disk by
default, and transcripts are not logged by default.

Local models are a stretch goal for a Pi 4 or Pi 5. They are not part of the Zero 2 W
baseline. The runtime should degrade predictably when a provider is slow or
unavailable: stop speaking, play a short closing prompt if possible, and end the call
rather than leaving the line open in silence.

## Consequences

### Positive

- The Zero 2 W only has to handle audio I/O, resampling, VAD, and network streams.
- Cloud models generally provide better ASR and TTS quality than local models that fit
  on the Pi.
- Provider-side scaling avoids long local inference queues and thermal throttling.
- India-region endpoints keep the network contribution within the latency budget.
- Swapping providers is a configuration and adapter change, not a rewrite.
- No model weights, GPU, or large storage volume is needed on the Pi.
- Streaming APIs support partial transcripts and sentence-chunked TTS, which are
  required for barge-in and turn-taking.

### Negative

- The agent depends on the home internet connection and on third-party availability.
- Call audio and transcripts leave the LAN, which is a privacy trade-off.
- Usage costs recur for every call and scale with duration.
- Provider quotas, authentication, and API changes become operational concerns.
- Code-mixed Malayalam-English quality is uncertain and must be evaluated per vendor.
- Region choice can be constrained by the provider's data-residency and billing setup.
- Latency is variable; a provider that is fast on average can still miss the budget.

### Risks

- **Provider outage mid-call.** The agent stops speaking and ends the call after a
  bounded wait; the heartbeat records the failure.
- **Latency spikes.** Health checks, per-stage timeouts, and a circuit breaker prevent
  one slow provider from holding the line open indefinitely.
- **Credential leakage.** Keys stay in environment variables or a secret file outside
  the repository; logs redact them.
- **Cost overrun.** Bound call duration, retries, and concurrency; alert on unusual
  usage.
- **Data residency or policy change.** Keep provider adapters separate and document
  which provider receives audio.
- **Vendor lock-in.** Avoid provider-specific features in the core state machine; keep
  prompts and audio formats portable.

## Alternatives considered

### Fully local on the Zero 2 W

Whisper.cpp and Piper can run on small boards, but not comfortably alongside the audio
bridge on a 512 MB Zero 2 W, and not with the latency and quality needed for a natural
call. It was rejected for the baseline.

### Fully local on a Pi 4 or Pi 5

A Pi 4 or Pi 5 can host local models as a stretch goal. It increases cost, power, heat,
and maintenance, and local model quality for Malayalam and code-mixed speech is not yet
established. It remains a useful fallback and a privacy-preserving option, not the
first implementation.

### Hybrid cloud and local

A hybrid could use local VAD and endpointing with cloud STT/LLM/TTS, which is already
the design, or fall back to local models when the network fails. A full local fallback
requires a larger board and a second model stack. It is deferred until the cloud path
is measured on real calls.

### On-device models on the phone

Running models on Phone A would add battery drain, heat, and Android background
restrictions, and would compete with the call audio path. It also spreads the runtime
across two devices. It was rejected for the first milestone.

## References

- [Design brief](../design-brief.md), section 10.
- [Architecture](../architecture.md), sections 6 and 12.
- [Voice agent](../voice-agent.md).
- [Latency budget](../latency-budget.md).
- [ADR 0005: Originate from a real SIM](./0005-originate-from-real-sim.md).
