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

The optional deliberative path replaces the single ``reply`` call with a
:class:`~telephone_agent.actions.DeliberativeBrain` that may speak, request one
mid-call consultation, or hang up. The call thread blocks on
:class:`~telephone_agent.consult.ConsultationBroker` until the async side answers.
A ``stop_event`` is checked only between audio frames, never during a turn.

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
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from .actions import Action, Consult, DeliberativeBrain, HangUp, Speak
from .audio import AudioDevice
from .brain import Brain
from .config import ensure_e164
from .consult import Consultation, ConsultationBroker, ConsultationOutcome
from .dialer import Dialer, mask_number
from .vad import Endpointer

__all__ = [
    "AgentReply",
    "CallEndReason",
    "CallFinished",
    "CallResult",
    "CallSession",
    "CallState",
    "ConsultationFinished",
    "ConsultationStarted",
    "SessionEvent",
    "StateChanged",
    "Synthesizer",
    "Transcriber",
    "Utterance",
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


@dataclass(frozen=True, slots=True)
class StateChanged:
    """Emitted after the call state changes."""

    state: CallState


@dataclass(frozen=True, slots=True)
class Utterance:
    """Emitted for each non-empty caller transcript."""

    text: str


@dataclass(frozen=True, slots=True)
class AgentReply:
    """Emitted when the agent has a non-empty reply to speak."""

    text: str


@dataclass(frozen=True, slots=True)
class ConsultationStarted:
    """Emitted when a mid-call consultation is registered."""

    consultation: Consultation


@dataclass(frozen=True, slots=True)
class ConsultationFinished:
    """Emitted when a mid-call consultation is resolved."""

    outcome: ConsultationOutcome


@dataclass(frozen=True, slots=True)
class CallFinished:
    """Emitted immediately before :meth:`CallSession.run` returns."""

    result: CallResult


SessionEvent = (
    StateChanged
    | Utterance
    | AgentReply
    | ConsultationStarted
    | ConsultationFinished
    | CallFinished
)


class CallSession:
    """Run one outbound call from dialing through hangup.

    Args:
        audio: Call audio backend (USB gadget or Bluetooth HFP).
        dialer: Helper client that places and ends the call.
        brain: Text-in, text-out conversation brain. Mutually exclusive with
            *deliberative*.
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
        deliberative: Optional action-returning brain. Requires *consultations*.
        consultations: Broker used by a deliberative brain for mid-call
            questions.
        consult_timeout_s: Default timeout for consultations that do not set
            their own.
        holding_phrase: Spoken before a consultation starts.
        on_event: Optional observer for session events. Observer exceptions are
            logged and ignored.
        stop_event: Optional event checked between audio frames. Setting it ends
            the call as interrupted.

    A session may be run only once.
    """

    def __init__(
        self,
        *,
        audio: AudioDevice,
        dialer: Dialer,
        brain: Brain | None = None,
        number: str,
        max_call_seconds: float = 300.0,
        silence_timeout_s: float = 20.0,
        vad_threshold: float = -40.0,
        transcriber: Transcriber | None = None,
        synthesizer: Synthesizer | None = None,
        clock: Callable[[], float] = time.monotonic,
        deliberative: DeliberativeBrain | None = None,
        consultations: ConsultationBroker | None = None,
        consult_timeout_s: float = 8.0,
        holding_phrase: str = "One moment please.",
        on_event: Callable[[SessionEvent], None] | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        if (brain is None) == (deliberative is None):
            raise ValueError("exactly one of brain or deliberative must be provided")
        if deliberative is not None and consultations is None:
            raise ValueError("deliberative requires a ConsultationBroker")
        if not math.isfinite(max_call_seconds) or max_call_seconds <= 0:
            raise ValueError(f"max_call_seconds must be a positive number (got {max_call_seconds})")
        if not math.isfinite(silence_timeout_s) or silence_timeout_s <= 0:
            raise ValueError(
                f"silence_timeout_s must be a positive number (got {silence_timeout_s})"
            )
        if not -100.0 <= vad_threshold <= 0.0:
            raise ValueError(f"vad_threshold must be between -100 and 0 dBFS (got {vad_threshold})")
        if not math.isfinite(consult_timeout_s) or consult_timeout_s <= 0:
            raise ValueError(
                f"consult_timeout_s must be a positive number (got {consult_timeout_s})"
            )
        if not holding_phrase.strip():
            raise ValueError("holding_phrase must not be empty or whitespace")

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
        self._deliberative = deliberative
        self._consultations = consultations
        self._consult_timeout_s = consult_timeout_s
        self._holding_phrase = holding_phrase
        self._on_event = on_event
        self._stop_event = stop_event
        self._state = CallState.IDLE
        self._ran = False
        self._dial_attempted = False
        self._hangup_requested = False
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
                    if self._stop_event is not None and self._stop_event.is_set():
                        reason = CallEndReason.INTERRUPTED
                        break
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
                        if self._hangup_requested:
                            reason = CallEndReason.COMPLETED
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
        self._emit(CallFinished(result))
        return result

    def _handle_utterance(self, pcm: bytes) -> None:
        transcript = self._transcribe(pcm)
        text = transcript.strip()
        if not text:
            logger.info("ignoring utterance: transcriber returned no text")
            return
        logger.debug(
            "utterance: %d bytes, transcript %d characters",
            len(pcm),
            len(transcript),
        )
        self._emit(Utterance(text))

        if self._deliberative is not None:
            action = self._deliberative.decide(text)
            self._handle_action(action, depth=0)
            return

        if self.brain is None:  # pragma: no cover - constructor validates this
            raise RuntimeError("no brain configured")
        reply = self.brain.reply(text)
        if not isinstance(reply, str):
            raise TypeError("Brain.reply() must return str")
        reply_text = reply.strip()
        if not reply_text:
            logger.warning("brain returned an empty reply")
            return
        self._emit(AgentReply(reply_text))
        self._speak(reply_text)

    def _handle_action(self, action: Action, *, depth: int) -> None:
        if isinstance(action, Speak):
            text = action.text.strip()
            if not text:
                logger.warning("deliberative brain returned an empty reply")
                return
            self._emit(AgentReply(text))
            self._speak(text)
            return

        if isinstance(action, HangUp):
            self._hangup_requested = True
            logger.info("deliberative brain requested hangup: %s", action.reason)
            return

        if isinstance(action, Consult):
            if depth > 0:
                raise RuntimeError("a consultation cannot request another consultation")
            question = action.question.strip()
            if not question:
                logger.warning("deliberative brain requested an empty consultation")
                return
            timeout_s = (
                action.timeout_s if action.timeout_s is not None else self._consult_timeout_s
            )
            self._speak(self._holding_phrase)
            if self._consultations is None:  # pragma: no cover - constructor validates this
                raise RuntimeError("no ConsultationBroker configured")
            outcome = self._consultations.ask(
                question,
                timeout_s,
                on_open=lambda consultation: self._emit(ConsultationStarted(consultation)),
            )
            self._emit(ConsultationFinished(outcome))
            if outcome.answer is None:
                next_action = self._deliberative.on_consult_timeout()
            else:
                next_action = self._deliberative.resume(outcome.answer)
            self._handle_action(next_action, depth=depth + 1)
            return

        raise TypeError("DeliberativeBrain must return Speak, Consult, or HangUp")

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
        if self._deliberative is not None:
            try:
                self._deliberative.reset()
            except Exception:
                logger.warning("DeliberativeBrain.reset() failed; continuing", exc_info=True)
            return

        if self.brain is None:  # pragma: no cover - constructor validates this
            return
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
        self._emit(StateChanged(state))

    def _emit(self, event: SessionEvent) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception:
            logger.warning("session event observer failed; continuing", exc_info=True)
