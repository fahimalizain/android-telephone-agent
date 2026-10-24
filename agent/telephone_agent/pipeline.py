"""Call session orchestration.

The v0 pipeline owns the call state machine, voice activity detection, and the
duration limits. Speech-to-text and text-to-speech are injected as
:class:`Transcriber` and :class:`Synthesizer` implementations. This repository
ships neither because provider choice is a deployment decision; the design brief
only requires a text-in, text-out :class:`~telephone_agent.brain.Brain` in v0.

When no adapters are injected, ``CallSession`` runs in scaffold mode: it detects
utterances, sends a clearly marked placeholder transcript to the brain, and logs
the reply instead of playing it. Use that mode to verify call control and VAD
wiring. It is not a conversation. Pass real adapters to hear the agent.

Known v0 limitations:

* No barge-in. Playback is synchronous, so the microphone is not drained while
  the agent speaks. See the TODO in :meth:`CallSession._speak`.
* No answer detection. The dial helper's ``POST /call`` is assumed to return
  once the call is placed; ringing time counts toward the silence timeout.
* No scheduler. The process places one call and exits.
"""

from __future__ import annotations

import enum
import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .audio import AudioDevice
from .brain import Brain
from .config import ensure_e164
from .dialer import Dialer, mask_number
from .vad import Endpointer

__all__ = [
    "CallEndReason",
    "CallResult",
    "CallSession",
    "CallState",
    "Synthesizer",
    "Transcriber",
]

logger = logging.getLogger(__name__)

_PLACEHOLDER_TRANSCRIPT = "[speech detected; no speech-to-text adapter is configured]"


class CallState(enum.StrEnum):
    """Lifecycle of one outbound call."""

    IDLE = "idle"
    DIALING = "dialing"
    IN_CALL = "in_call"
    ENDING = "ending"
    FAILED = "failed"


class CallEndReason(enum.StrEnum):
    """Why a call session stopped."""

    COMPLETED = "completed"
    SILENCE_TIMEOUT = "silence_timeout"
    MAX_DURATION = "max_duration"
    AUDIO_ENDED = "audio_ended"
    TURN_FAILED = "turn_failed"
    HANGUP_FAILED = "hangup_failed"
    INTERRUPTED = "interrupted"


class Transcriber(Protocol):
    """Speech-to-text adapter.

    ``pcm`` is 16-bit little-endian mono at ``sample_rate``. The adapter must
    return one final transcript for the utterance. Streaming partial transcripts
    are a v0.2 extension; a streaming adapter belongs beside this module and
    should return sentence-level text to the call loop.
    """

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        """Return the transcript of one utterance."""


class Synthesizer(Protocol):
    """Text-to-speech adapter.

    Return 16-bit little-endian mono PCM at ``sample_rate``. Return ``b""`` to
    skip playback. Streaming synthesis belongs beside this module; the pipeline
    would play sentence chunks as they arrive.
    """

    def synthesize(self, text: str, sample_rate: int) -> bytes:
        """Return PCM for *text*."""


@dataclass(frozen=True, slots=True)
class CallResult:
    """Outcome of :meth:`CallSession.run`."""

    state: CallState
    reason: CallEndReason
    duration_s: float
    turns: int


class CallSession:
    """Run one outbound call from dialing through hangup.

    Args:
        audio: Call audio backend (USB gadget or Bluetooth HFP).
        dialer: Helper client that places and ends the call.
        brain: Text-in, text-out conversation brain.
        number: E.164 destination number.
        max_call_seconds: Hard limit on call duration.
        silence_timeout_s: Hang up after this much silence with no active
            utterance. Ringing time counts.
        vad_threshold: Speech threshold in dBFS.
        transcriber: Optional speech-to-text adapter. Without one the session
            uses a placeholder transcript and logs a warning.
        synthesizer: Optional text-to-speech adapter. Without one replies are
            logged, not played.
        clock: Monotonic clock, injectable for tests.

    A session may be run only once.
    """

    def __init__(
        self,
        *,
        audio: AudioDevice,
        dialer: Dialer,
        brain: Brain,
        number: str,
        max_call_seconds: float = 300.0,
        silence_timeout_s: float = 20.0,
        vad_threshold: float = -40.0,
        transcriber: Transcriber | None = None,
        synthesizer: Synthesizer | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not math.isfinite(max_call_seconds) or max_call_seconds <= 0:
            raise ValueError(f"max_call_seconds must be a positive number (got {max_call_seconds})")
        if not math.isfinite(silence_timeout_s) or silence_timeout_s <= 0:
            raise ValueError(
                f"silence_timeout_s must be a positive number (got {silence_timeout_s})"
            )
        if not -100.0 <= vad_threshold <= 0.0:
            raise ValueError(f"vad_threshold must be between -100 and 0 dBFS (got {vad_threshold})")

        self.audio = audio
        self.dialer = dialer
        self.brain = brain
        self.number = ensure_e164(number)
        self.max_call_seconds = max_call_seconds
        self.silence_timeout_s = silence_timeout_s
        self.vad_threshold = vad_threshold
        self._transcriber = transcriber
        self._synthesizer = synthesizer
        self._clock = clock
        self._state = CallState.IDLE
        self._ran = False
        self._dial_attempted = False
        self._warned_no_transcriber = False
        self._warned_no_synthesizer = False

    @property
    def state(self) -> CallState:
        """Current lifecycle state."""

        return self._state

    def run(self) -> CallResult:
        """Place the call, run the conversation loop, and hang up.

        Keyboard interrupts end the call without a traceback and still attempt
        the hangup request. Adapter or brain failures end the call and return a
        ``FAILED`` result; dialing and audio-device failures propagate as
        exceptions after cleanup.
        """

        if self._ran:
            raise RuntimeError("CallSession.run() may only be called once")
        self._ran = True

        started = self._clock()
        reason = CallEndReason.COMPLETED
        final_state = CallState.IDLE
        turns = 0

        try:
            self._reset_brain()
            self._set_state(CallState.DIALING)
            logger.info("placing call to %s", mask_number(self.number))
            self._dial_attempted = True
            self.dialer.place_call(self.number)

            self._set_state(CallState.IN_CALL)
            logger.info("call placed; starting conversation loop")
            self.audio.start()
            endpointer = Endpointer(
                sample_rate=self.audio.sample_rate,
                threshold_db=self.vad_threshold,
            )
            frames = self.audio.frames()
            last_activity = self._clock()
            deadline = started + self.max_call_seconds
            try:
                for frame in frames:
                    now = self._clock()
                    if now >= deadline:
                        reason = CallEndReason.MAX_DURATION
                        break
                    if endpointer.in_utterance:
                        last_activity = now

                    utterance = endpointer.push(frame)
                    if utterance is not None:
                        turns += 1
                        last_activity = self._clock()
                        try:
                            self._handle_utterance(utterance)
                        except Exception:
                            logger.exception("conversation turn failed")
                            reason = CallEndReason.TURN_FAILED
                            final_state = CallState.FAILED
                            break
                        last_activity = self._clock()
                        if last_activity >= deadline:
                            reason = CallEndReason.MAX_DURATION
                            break
                        continue

                    if (
                        not endpointer.in_utterance
                        and now - last_activity >= self.silence_timeout_s
                    ):
                        logger.info(
                            "no speech for %.1f s; ending call",
                            self.silence_timeout_s,
                        )
                        reason = CallEndReason.SILENCE_TIMEOUT
                        break
                else:
                    reason = CallEndReason.AUDIO_ENDED
            finally:
                frames.close()
        except KeyboardInterrupt:
            logger.info("interrupted; ending call")
            reason = CallEndReason.INTERRUPTED
        except Exception:
            final_state = CallState.FAILED
            raise
        finally:
            self._set_state(CallState.ENDING)
            hangup_ok = self._teardown()
            if not hangup_ok:
                if final_state == CallState.IDLE:
                    final_state = CallState.FAILED
                if reason == CallEndReason.COMPLETED:
                    reason = CallEndReason.HANGUP_FAILED
            self._set_state(final_state)

        duration_s = self._clock() - started
        result = CallResult(
            state=final_state,
            reason=reason,
            duration_s=duration_s,
            turns=turns,
        )
        logger.info(
            "call finished: state=%s reason=%s turns=%d duration=%.1f s",
            result.state,
            result.reason,
            result.turns,
            result.duration_s,
        )
        return result

    def _handle_utterance(self, pcm: bytes) -> None:
        transcript = self._transcribe(pcm)
        if not transcript.strip():
            logger.info("ignoring utterance: transcriber returned no text")
            return
        logger.debug(
            "utterance: %d bytes, transcript %d characters",
            len(pcm),
            len(transcript),
        )

        reply = self.brain.reply(transcript.strip())
        if not isinstance(reply, str):
            raise TypeError("Brain.reply() must return str")
        text = reply.strip()
        if not text:
            logger.warning("brain returned an empty reply")
            return
        self._speak(text)

    def _transcribe(self, pcm: bytes) -> str:
        if self._transcriber is None:
            if not self._warned_no_transcriber:
                logger.warning(
                    "no Transcriber configured; using placeholder transcripts. "
                    "See agent/README.md for the adapter seam."
                )
                self._warned_no_transcriber = True
            return _PLACEHOLDER_TRANSCRIPT
        text = self._transcriber.transcribe(pcm, self.audio.sample_rate)
        if not isinstance(text, str):
            raise TypeError("Transcriber.transcribe() must return str")
        return text

    def _speak(self, text: str) -> None:
        if self._synthesizer is None:
            if not self._warned_no_synthesizer:
                logger.warning(
                    "no Synthesizer configured; replies will be logged instead "
                    "of played. See agent/README.md for the adapter seam."
                )
                self._warned_no_synthesizer = True
            logger.info("agent reply (not spoken): %s", text)
            return

        pcm = self._synthesizer.synthesize(text, self.audio.sample_rate)
        if not isinstance(pcm, (bytes, bytearray)):
            raise TypeError("Synthesizer.synthesize() must return bytes")
        if not pcm:
            logger.warning("synthesizer returned empty PCM; reply was not played")
            return

        # TODO(v0.2): barge-in. `play()` blocks until aplay exits, so capture is
        # not drained while the agent talks. The extension point is here:
        #  1. start playback in a worker thread or an incremental sink;
        #  2. keep consuming `frames()` and feed the endpointer;
        #  3. if the caller starts speaking, call a new
        #     `audio.stop_playback()` that terminates the player process.
        # Playback must remain at the audio device's rate and must be drained
        # before the next reply is queued.
        self.audio.play(bytes(pcm))

    def _reset_brain(self) -> None:
        reset = getattr(self.brain, "reset", None)
        if not callable(reset):
            return
        try:
            reset()
        except Exception:
            logger.warning("Brain.reset() failed; continuing", exc_info=True)

    def _teardown(self) -> bool:
        """Stop audio and request hangup. Returns ``False`` if hangup failed."""

        try:
            self.audio.stop()
        except Exception:
            logger.exception("failed to stop the audio device")

        hangup_ok = True
        if self._dial_attempted:
            try:
                self.dialer.hangup()
                logger.info("hangup requested")
            except Exception as exc:
                logger.warning("hangup request failed: %s", exc)
                hangup_ok = False
        return hangup_ok

    def _set_state(self, state: CallState) -> None:
        if state == self._state:
            return
        logger.debug("state %s -> %s", self._state, state)
        self._state = state
