"""Tests for the call session loop and the deliberative action path."""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Iterator

import pytest
from telephone_agent.actions import Action, Consult, DeliberativeBrain, HangUp, Speak
from telephone_agent.brain import Brain, EchoBrain
from telephone_agent.consult import ConsultationBroker
from telephone_agent.pipeline import (
    AgentReply,
    CallEndReason,
    CallFinished,
    CallResult,
    CallSession,
    CallState,
    ConsultationFinished,
    ConsultationStarted,
    SessionEvent,
    StateChanged,
    Utterance,
)

_NUMBER = "+15555550100"
_SAMPLE_RATE = 16_000
_FRAME_SAMPLES = 320
_SILENT_FRAME = bytes(_FRAME_SAMPLES * 2)
_SPEECH_FRAME = (10_000).to_bytes(2, "little", signed=True) * _FRAME_SAMPLES


def _utterance_frames() -> list[bytes]:
    """Return frames for one 300 ms utterance followed by trailing silence."""

    return [_SPEECH_FRAME] * 15 + [_SILENT_FRAME] * 25


class FakeAudio:
    sample_rate = _SAMPLE_RATE
    channels = 1
    frame_ms = 20
    frame_bytes = _FRAME_SAMPLES * 2
    description = "fake audio device"

    def __init__(self, frames: list[bytes]) -> None:
        self._frames = frames
        self.played: list[bytes] = []
        self.start_count = 0
        self.stop_count = 0

    def start(self) -> None:
        self.start_count += 1

    def stop(self) -> None:
        self.stop_count += 1

    def frames(self) -> Iterator[bytes]:
        yield from self._frames

    def play(self, pcm: bytes) -> None:
        self.played.append(pcm)


class FakeDialer:
    def __init__(self) -> None:
        self.placed: list[str] = []
        self.hangups = 0

    def place_call(self, number: str) -> dict[str, object]:
        self.placed.append(number)
        return {}

    def hangup(self) -> dict[str, object]:
        self.hangups += 1
        return {}


class FixedTranscriber:
    def __init__(self, text: str = "hello") -> None:
        self.text = text

    def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        return self.text


class FakeSynthesizer:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def synthesize(self, text: str, sample_rate: int) -> bytes:
        self.texts.append(text)
        return b"pcm"


class FakeDeliberative:
    def __init__(
        self,
        *,
        decide_action: Action,
        resume_action: Action | None = None,
        timeout_action: Action | None = None,
    ) -> None:
        self.decide_action = decide_action
        self.resume_action = resume_action if resume_action is not None else Speak("Thanks.")
        self.timeout_action = (
            timeout_action if timeout_action is not None else Speak("Sorry, I don't know.")
        )
        self.transcripts: list[str] = []
        self.answers: list[str] = []
        self.timeouts = 0
        self.resets = 0

    def decide(self, transcript: str) -> Action:
        self.transcripts.append(transcript)
        return self.decide_action

    def resume(self, answer: str) -> Action:
        self.answers.append(answer)
        return self.resume_action

    def on_consult_timeout(self) -> Action:
        self.timeouts += 1
        return self.timeout_action

    def reset(self) -> None:
        self.resets += 1


def _make_session(
    frames: list[bytes],
    *,
    brain: Brain | None = None,
    deliberative: DeliberativeBrain | None = None,
    consultations: ConsultationBroker | None = None,
    on_event: Callable[[SessionEvent], None] | None = None,
    stop_event: threading.Event | None = None,
    consult_timeout_s: float = 8.0,
    holding_phrase: str = "One moment please.",
) -> tuple[CallSession, FakeAudio, FakeDialer, FakeSynthesizer]:
    audio = FakeAudio(frames)
    dialer = FakeDialer()
    synthesizer = FakeSynthesizer()
    session = CallSession(
        audio=audio,
        dialer=dialer,
        brain=brain,
        number=_NUMBER,
        transcriber=FixedTranscriber(),
        synthesizer=synthesizer,
        deliberative=deliberative,
        consultations=consultations,
        consult_timeout_s=consult_timeout_s,
        holding_phrase=holding_phrase,
        on_event=on_event,
        stop_event=stop_event,
    )
    return session, audio, dialer, synthesizer


def _run_in_thread(
    session: CallSession,
) -> tuple[threading.Thread, list[CallResult], list[Exception]]:
    results: list[CallResult] = []
    errors: list[Exception] = []

    def target() -> None:
        try:
            results.append(session.run())
        except Exception as exc:  # reported by the test helper
            errors.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, results, errors


def _finish(
    thread: threading.Thread,
    results: list[CallResult],
    errors: list[Exception],
) -> CallResult:
    thread.join(5.0)
    assert not thread.is_alive(), "call session did not finish"
    assert not errors, errors
    assert results
    return results[0]


def _events_of(events: list[SessionEvent], kind: type[SessionEvent]) -> list[SessionEvent]:
    return [event for event in events if isinstance(event, kind)]


def test_consult_speaks_holding_phrase_then_resumes_with_answer() -> None:
    broker = ConsultationBroker(id_factory=lambda: "c-1")
    deliberative = FakeDeliberative(
        decide_action=Consult("What is the door code?", timeout_s=0.2),
        resume_action=Speak("The door code is forty two."),
    )
    events: list[SessionEvent] = []
    started = threading.Event()

    def on_event(event: SessionEvent) -> None:
        events.append(event)
        if isinstance(event, ConsultationStarted):
            started.set()

    session, audio, dialer, synthesizer = _make_session(
        _utterance_frames(),
        deliberative=deliberative,
        consultations=broker,
        on_event=on_event,
    )
    thread, results, errors = _run_in_thread(session)

    assert started.wait(2.0)
    assert broker.answer("forty two") is True
    result = _finish(thread, results, errors)

    assert synthesizer.texts == ["One moment please.", "The door code is forty two."]
    assert audio.played == [b"pcm", b"pcm"]
    assert deliberative.transcripts == ["hello"]
    assert deliberative.answers == ["forty two"]
    assert deliberative.timeouts == 0
    assert deliberative.resets == 1

    assert [e.text for e in _events_of(events, Utterance)] == ["hello"]
    assert [e.text for e in _events_of(events, AgentReply)] == ["The door code is forty two."]
    started_events = _events_of(events, ConsultationStarted)
    assert [e.consultation.question for e in started_events] == ["What is the door code?"]
    finished_events = _events_of(events, ConsultationFinished)
    assert finished_events[0].outcome.answer == "forty two"
    assert finished_events[0].outcome.timed_out is False
    assert [e.state for e in _events_of(events, StateChanged)] == [
        CallState.DIALING,
        CallState.IN_CALL,
        CallState.ENDING,
        CallState.IDLE,
    ]
    assert isinstance(events[-1], CallFinished)
    assert events[-1].result is result

    assert result.state == CallState.IDLE
    assert result.reason == CallEndReason.AUDIO_ENDED
    assert result.turns == 1
    assert dialer.placed == [_NUMBER]
    assert dialer.hangups == 1
    assert audio.start_count == 1
    assert audio.stop_count == 1
    assert broker.pending is None


def test_consult_timeout_uses_the_timeout_action() -> None:
    broker = ConsultationBroker()
    deliberative = FakeDeliberative(
        decide_action=Consult("Are you still there?", timeout_s=0.05),
        timeout_action=Speak("I could not look that up."),
    )
    events: list[SessionEvent] = []
    session, _audio, _dialer, synthesizer = _make_session(
        _utterance_frames(),
        deliberative=deliberative,
        consultations=broker,
        on_event=events.append,
    )

    result = session.run()

    assert synthesizer.texts == ["One moment please.", "I could not look that up."]
    assert deliberative.answers == []
    assert deliberative.timeouts == 1
    finished_events = _events_of(events, ConsultationFinished)
    assert finished_events[0].outcome.answer is None
    assert finished_events[0].outcome.timed_out is True
    assert result.reason == CallEndReason.AUDIO_ENDED
    assert broker.pending is None


def test_hangup_action_ends_the_call_as_completed() -> None:
    deliberative = FakeDeliberative(decide_action=HangUp("done"))
    session, audio, dialer, synthesizer = _make_session(
        _utterance_frames(),
        deliberative=deliberative,
        consultations=ConsultationBroker(),
    )

    result = session.run()

    assert result.reason == CallEndReason.COMPLETED
    assert result.turns == 1
    assert synthesizer.texts == []
    assert audio.played == []
    assert dialer.hangups == 1


def test_stop_event_ends_the_call_between_frames() -> None:
    stop_event = threading.Event()
    stop_event.set()
    deliberative = FakeDeliberative(decide_action=Speak("never spoken"))
    session, _audio, dialer, synthesizer = _make_session(
        [_SILENT_FRAME],
        deliberative=deliberative,
        consultations=ConsultationBroker(),
        stop_event=stop_event,
    )

    result = session.run()

    assert result.reason == CallEndReason.INTERRUPTED
    assert result.turns == 0
    assert synthesizer.texts == []
    assert deliberative.transcripts == []
    assert dialer.hangups == 1


def test_nested_consultation_fails_the_turn() -> None:
    broker = ConsultationBroker()
    deliberative = FakeDeliberative(
        decide_action=Consult("First question?", timeout_s=0.2),
        resume_action=Consult("Second question?"),
    )
    events: list[SessionEvent] = []
    started = threading.Event()

    def on_event(event: SessionEvent) -> None:
        events.append(event)
        if isinstance(event, ConsultationStarted):
            started.set()

    session, _audio, _dialer, synthesizer = _make_session(
        _utterance_frames(),
        deliberative=deliberative,
        consultations=broker,
        on_event=on_event,
    )
    thread, results, errors = _run_in_thread(session)

    assert started.wait(2.0)
    assert broker.answer("answer") is True
    result = _finish(thread, results, errors)

    assert result.state == CallState.FAILED
    assert result.reason == CallEndReason.TURN_FAILED
    assert synthesizer.texts == ["One moment please."]
    assert deliberative.answers == ["answer"]
    assert len(_events_of(events, ConsultationStarted)) == 1
    assert len(_events_of(events, ConsultationFinished)) == 1
    assert isinstance(events[-1], CallFinished)
    assert broker.pending is None


def test_plain_brain_path_still_speaks_and_emits_events() -> None:
    events: list[SessionEvent] = []
    session, audio, _dialer, synthesizer = _make_session(
        _utterance_frames(),
        brain=EchoBrain(),
        on_event=events.append,
    )

    result = session.run()

    assert synthesizer.texts == ["I heard: hello"]
    assert audio.played == [b"pcm"]
    assert [e.text for e in _events_of(events, Utterance)] == ["hello"]
    assert [e.text for e in _events_of(events, AgentReply)] == ["I heard: hello"]
    assert result.reason == CallEndReason.AUDIO_ENDED
    assert result.turns == 1


def test_observer_exceptions_do_not_break_the_call() -> None:
    def on_event(event: SessionEvent) -> None:
        raise RuntimeError("observer failed")

    session, _audio, _dialer, synthesizer = _make_session(
        _utterance_frames(),
        brain=EchoBrain(),
        on_event=on_event,
    )

    result = session.run()

    assert result.reason == CallEndReason.AUDIO_ENDED
    assert synthesizer.texts == ["I heard: hello"]


def test_session_requires_exactly_one_brain() -> None:
    audio = FakeAudio([])
    dialer = FakeDialer()

    with pytest.raises(ValueError, match="exactly one"):
        CallSession(audio=audio, dialer=dialer, number=_NUMBER)

    with pytest.raises(ValueError, match="exactly one"):
        CallSession(
            audio=audio,
            dialer=dialer,
            number=_NUMBER,
            brain=EchoBrain(),
            deliberative=FakeDeliberative(decide_action=Speak("hi")),
            consultations=ConsultationBroker(),
        )


def test_deliberative_requires_a_consultation_broker() -> None:
    with pytest.raises(ValueError, match="ConsultationBroker"):
        CallSession(
            audio=FakeAudio([]),
            dialer=FakeDialer(),
            number=_NUMBER,
            deliberative=FakeDeliberative(decide_action=Speak("hi")),
        )


@pytest.mark.parametrize("timeout_s", [0.0, -1.0, math.inf, -math.inf, math.nan])
def test_invalid_consult_timeout_is_rejected(timeout_s: float) -> None:
    with pytest.raises(ValueError, match="consult_timeout_s"):
        CallSession(
            audio=FakeAudio([]),
            dialer=FakeDialer(),
            number=_NUMBER,
            brain=EchoBrain(),
            consult_timeout_s=timeout_s,
        )


@pytest.mark.parametrize("holding_phrase", ["", "   ", "\n\t"])
def test_blank_holding_phrase_is_rejected(holding_phrase: str) -> None:
    with pytest.raises(ValueError, match="holding_phrase"):
        CallSession(
            audio=FakeAudio([]),
            dialer=FakeDialer(),
            number=_NUMBER,
            brain=EchoBrain(),
            holding_phrase=holding_phrase,
        )
