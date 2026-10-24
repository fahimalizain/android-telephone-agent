"""Tests for the thread-safe mid-call consultation broker."""

from __future__ import annotations

import math
import threading

import pytest
from telephone_agent.consult import (
    Consultation,
    ConsultationBroker,
    ConsultationOutcome,
)


def _start_ask(
    broker: ConsultationBroker,
    question: str,
    timeout_s: float,
    *,
    opened: threading.Event,
    opened_consultations: list[Consultation],
) -> tuple[threading.Thread, list[ConsultationOutcome], list[Exception]]:
    outcomes: list[ConsultationOutcome] = []
    errors: list[Exception] = []

    def on_open(consultation: Consultation) -> None:
        opened_consultations.append(consultation)
        opened.set()

    def target() -> None:
        try:
            outcomes.append(broker.ask(question, timeout_s, on_open=on_open))
        except Exception as exc:  # reported by the test helper
            errors.append(exc)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, outcomes, errors


def _join(thread: threading.Thread, errors: list[Exception]) -> None:
    thread.join(2.0)
    assert not thread.is_alive(), "ask() did not return"
    assert not errors, errors


def test_pending_is_none_initially() -> None:
    assert ConsultationBroker().pending is None


def test_answer_from_another_thread_resolves_the_consultation() -> None:
    broker = ConsultationBroker(clock=lambda: 100.0, id_factory=lambda: "c-1")
    opened = threading.Event()
    opened_consultations: list[Consultation] = []

    thread, outcomes, errors = _start_ask(
        broker,
        "  what is the door code?  ",
        0.2,
        opened=opened,
        opened_consultations=opened_consultations,
    )
    assert opened.wait(2.0)
    consultation = opened_consultations[0]

    assert consultation == Consultation(
        id="c-1",
        question="what is the door code?",
        created_at=100.0,
    )
    assert broker.pending == consultation

    assert broker.answer("  42  ") is True

    _join(thread, errors)
    outcome = outcomes[0]
    assert outcome.consultation is consultation
    assert outcome.answer == "42"
    assert outcome.timed_out is False
    assert broker.pending is None


def test_ask_times_out_and_rejects_a_late_answer() -> None:
    broker = ConsultationBroker()

    outcome = broker.ask("is anyone there?", 0.05)

    assert outcome.consultation.question == "is anyone there?"
    assert outcome.answer is None
    assert outcome.timed_out is True
    assert broker.pending is None
    assert broker.answer("too late") is False


def test_second_ask_raises_while_one_is_pending() -> None:
    broker = ConsultationBroker(id_factory=lambda: "c-1")
    opened = threading.Event()
    opened_consultations: list[Consultation] = []

    thread, outcomes, errors = _start_ask(
        broker,
        "first",
        0.2,
        opened=opened,
        opened_consultations=opened_consultations,
    )
    assert opened.wait(2.0)

    with pytest.raises(RuntimeError, match="already pending"):
        broker.ask("second", 0.2)

    assert broker.answer("first answer") is True
    _join(thread, errors)
    assert outcomes[0].answer == "first answer"


def test_cancel_resolves_without_an_answer() -> None:
    broker = ConsultationBroker()
    opened = threading.Event()
    opened_consultations: list[Consultation] = []

    thread, outcomes, errors = _start_ask(
        broker,
        "cancel me",
        0.2,
        opened=opened,
        opened_consultations=opened_consultations,
    )
    assert opened.wait(2.0)
    assert broker.pending == opened_consultations[0]

    assert broker.cancel() is True
    assert broker.cancel() is False

    _join(thread, errors)
    outcome = outcomes[0]
    assert outcome.answer is None
    assert outcome.timed_out is False
    assert broker.pending is None


def test_answer_with_mismatched_id_is_rejected() -> None:
    broker = ConsultationBroker(id_factory=lambda: "c-1")
    opened = threading.Event()
    opened_consultations: list[Consultation] = []

    thread, outcomes, errors = _start_ask(
        broker,
        "question",
        0.2,
        opened=opened,
        opened_consultations=opened_consultations,
    )
    assert opened.wait(2.0)

    assert broker.answer("wrong", consultation_id="c-2") is False
    assert broker.pending == opened_consultations[0]
    assert broker.answer("right", consultation_id="c-1") is True

    _join(thread, errors)
    assert outcomes[0].answer == "right"


def test_on_open_runs_before_waiting_and_may_answer() -> None:
    broker = ConsultationBroker(id_factory=lambda: "c-1")
    opened: list[Consultation] = []
    pending_after_open: list[Consultation | None] = []
    pending_after_answer: list[Consultation | None] = []

    def on_open(consultation: Consultation) -> None:
        opened.append(consultation)
        pending_after_open.append(broker.pending)
        assert broker.answer("early answer") is True
        pending_after_answer.append(broker.pending)

    outcome = broker.ask("question", 0.2, on_open=on_open)

    assert opened[0].id == "c-1"
    assert opened[0].question == "question"
    assert pending_after_open == opened
    assert pending_after_answer == [None]
    assert outcome.answer == "early answer"
    assert outcome.timed_out is False


def test_answer_without_a_pending_consultation_returns_false() -> None:
    broker = ConsultationBroker()

    assert broker.answer("hello") is False
    assert broker.cancel() is False


@pytest.mark.parametrize("question", ["", "   ", "\n\t"])
def test_ask_rejects_an_empty_question(question: str) -> None:
    with pytest.raises(ValueError, match="question"):
        ConsultationBroker().ask(question, 0.2)


@pytest.mark.parametrize("timeout_s", [0.0, -1.0, math.inf, -math.inf, math.nan])
def test_ask_rejects_an_invalid_timeout(timeout_s: float) -> None:
    with pytest.raises(ValueError, match="timeout_s"):
        ConsultationBroker().ask("question", timeout_s)


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
def test_answer_rejects_empty_text(text: str) -> None:
    with pytest.raises(ValueError, match="answer"):
        ConsultationBroker().answer(text)
