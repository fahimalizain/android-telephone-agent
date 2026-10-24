"""Actions requested by a deliberative conversation brain.

A :class:`~telephone_agent.brain.Brain` responds to every transcript with a
single string. A :class:`DeliberativeBrain` has more control: it may speak,
consult a slower knowledge source mid-call, or end the call. The pipeline owns
the effects; these values only describe the requested next step.

:class:`BrainActionAdapter` lets an existing text-in, text-out brain participate
without changes. It always replies immediately and uses a fixed fallback line
when a consultation times out.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

__all__ = [
    "Action",
    "BrainActionAdapter",
    "Consult",
    "DeliberativeBrain",
    "HangUp",
    "Speak",
]


@dataclass(frozen=True, slots=True)
class Speak:
    """Speak *text* to the caller."""

    text: str

    def __post_init__(self) -> None:
        if not self.text.strip():
            raise ValueError("Speak.text must not be empty or whitespace")


@dataclass(frozen=True, slots=True)
class Consult:
    """Pause the call and ask *question* through a consultation broker.

    ``timeout_s`` overrides the call session's default consultation timeout when
    set.
    """

    question: str
    timeout_s: float | None = None

    def __post_init__(self) -> None:
        if not self.question.strip():
            raise ValueError("Consult.question must not be empty or whitespace")
        if self.timeout_s is not None and (
            not math.isfinite(self.timeout_s) or self.timeout_s <= 0
        ):
            raise ValueError(
                f"Consult.timeout_s must be a finite number greater than 0 (got {self.timeout_s!r})"
            )


@dataclass(frozen=True, slots=True)
class HangUp:
    """End the call after the current turn."""

    reason: str = "completed"

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("HangUp.reason must not be empty or whitespace")


Action = Speak | Consult | HangUp


class DeliberativeBrain(Protocol):
    """A brain that chooses the next call action.

    ``decide`` handles a caller transcript. If it returns :class:`Consult`, the
    pipeline calls :meth:`resume` with the answer or
    :meth:`on_consult_timeout` when no answer arrives. The action returned from
    either method is processed in the same turn but may not itself consult.
    """

    def decide(self, transcript: str) -> Action:
        """Return the next action for one caller transcript."""

    def resume(self, answer: str) -> Action:
        """Return the next action after a consultation is answered."""

    def on_consult_timeout(self) -> Action:
        """Return the next action when a consultation times out."""

    def reset(self) -> None:
        """Discard conversation history, if any."""


class _ReplyBrain(Protocol):
    def reply(self, transcript: str) -> str:
        """Return a spoken-style reply to one caller transcript."""


@dataclass(frozen=True, slots=True)
class BrainActionAdapter:
    """Adapt a plain ``reply(str) -> str`` brain to :class:`DeliberativeBrain`."""

    brain: _ReplyBrain

    def decide(self, transcript: str) -> Action:
        """Return the wrapped brain's reply as a :class:`Speak` action."""

        return Speak(self.brain.reply(transcript))

    def resume(self, answer: str) -> Action:
        """Speak the consultation answer verbatim."""

        return Speak(answer)

    def on_consult_timeout(self) -> Action:
        """Say that the requested information is unavailable."""

        return Speak("I'm sorry, I don't have that information right now.")

    def reset(self) -> None:
        """Reset the wrapped brain if it exposes a callable ``reset``."""

        reset = getattr(self.brain, "reset", None)
        if callable(reset):
            reset()
