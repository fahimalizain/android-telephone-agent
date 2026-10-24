"""Tests for deliberative actions and the plain-brain adapter."""

from __future__ import annotations

import math

import pytest
from telephone_agent.actions import (
    BrainActionAdapter,
    Consult,
    HangUp,
    Speak,
)


class FakeBrain:
    def __init__(self, reply: str = "hello") -> None:
        self.reply_text = reply
        self.transcripts: list[str] = []
        self.resets = 0

    def reply(self, transcript: str) -> str:
        self.transcripts.append(transcript)
        return self.reply_text

    def reset(self) -> None:
        self.resets += 1


class BrainWithoutReset:
    def reply(self, transcript: str) -> str:
        return f"reply to {transcript}"


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_speak_rejects_empty_text(text: str) -> None:
    with pytest.raises(ValueError, match="Speak.text"):
        Speak(text)


def test_speak_keeps_original_text() -> None:
    action = Speak("  hello  ")
    assert action.text == "  hello  "


@pytest.mark.parametrize("question", ["", "   ", "\n\t"])
def test_consult_rejects_empty_question(question: str) -> None:
    with pytest.raises(ValueError, match="Consult.question"):
        Consult(question)


@pytest.mark.parametrize("timeout_s", [0.0, -1.0, math.inf, -math.inf, math.nan])
def test_consult_rejects_invalid_timeout(timeout_s: float) -> None:
    with pytest.raises(ValueError, match="Consult.timeout_s"):
        Consult("question", timeout_s=timeout_s)


def test_consult_accepts_none_and_positive_timeout() -> None:
    assert Consult("question").timeout_s is None
    assert Consult("question", timeout_s=0.2).timeout_s == 0.2


@pytest.mark.parametrize("reason", ["", "   ", "\n\t"])
def test_hangup_rejects_empty_reason(reason: str) -> None:
    with pytest.raises(ValueError, match="HangUp.reason"):
        HangUp(reason)


def test_hangup_defaults_to_completed() -> None:
    assert HangUp().reason == "completed"


def test_adapter_decide_wraps_brain_reply() -> None:
    brain = FakeBrain("a reply")
    adapter = BrainActionAdapter(brain)

    action = adapter.decide("hello")

    assert action == Speak("a reply")
    assert brain.transcripts == ["hello"]


def test_adapter_resume_speaks_answer_verbatim() -> None:
    adapter = BrainActionAdapter(FakeBrain())

    assert adapter.resume("the answer") == Speak("the answer")


def test_adapter_timeout_uses_fixed_fallback() -> None:
    adapter = BrainActionAdapter(FakeBrain())

    assert adapter.on_consult_timeout() == Speak(
        "I'm sorry, I don't have that information right now."
    )


def test_adapter_reset_delegates_when_callable() -> None:
    brain = FakeBrain()
    adapter = BrainActionAdapter(brain)

    adapter.reset()

    assert brain.resets == 1


def test_adapter_reset_tolerates_brain_without_reset() -> None:
    adapter = BrainActionAdapter(BrainWithoutReset())

    adapter.reset()

    assert adapter.decide("hi") == Speak("reply to hi")
