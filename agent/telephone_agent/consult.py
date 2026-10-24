"""Broker for one mid-call consultation at a time.

The call thread owns audio and blocks while it waits for an answer. A separate
async component, usually the asyncio loop that talks to a slow knowledge source,
resolves the consultation with :meth:`ConsultationBroker.answer` or
:meth:`ConsultationBroker.cancel`. All methods are thread-safe.

Only one consultation may be pending per broker. ``ask`` registers it under a
lock, invokes ``on_open`` outside the lock, and then waits on a
:class:`threading.Event` with a monotonic deadline. The lock is never held while
waiting, so the resolving thread can always make progress. A late answer after a
timeout or cancellation is rejected.
"""

from __future__ import annotations

import math
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

__all__ = ["Consultation", "ConsultationBroker", "ConsultationOutcome"]


@dataclass(frozen=True, slots=True)
class Consultation:
    """One question sent to the slow side of the call."""

    id: str
    question: str
    created_at: float


@dataclass(frozen=True, slots=True)
class ConsultationOutcome:
    """Resolution of a consultation.

    ``answer`` is ``None`` when the consultation timed out or was cancelled.
    ``timed_out`` distinguishes those two cases.
    """

    consultation: Consultation
    answer: str | None
    timed_out: bool


@dataclass(slots=True)
class _Pending:
    consultation: Consultation
    event: threading.Event
    deadline: float
    answer: str | None = None
    timed_out: bool = False
    resolved: bool = False


class ConsultationBroker:
    """Coordinate consultations between the call thread and the async side.

    Args:
        clock: Supplies ``Consultation.created_at``; injectable for tests.
        id_factory: Supplies consultation identifiers; injectable for tests.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._clock = clock
        self._id_factory = id_factory
        self._lock = threading.Lock()
        self._pending: _Pending | None = None

    @property
    def pending(self) -> Consultation | None:
        """The consultation currently waiting for an answer, if any."""

        with self._lock:
            pending = self._pending
            if pending is None or pending.resolved:
                return None
            return pending.consultation

    def ask(
        self,
        question: str,
        timeout_s: float,
        *,
        on_open: Callable[[Consultation], None] | None = None,
    ) -> ConsultationOutcome:
        """Register a question and block until it is answered or times out.

        Args:
            question: Text to send to the slow side. Must not be blank.
            timeout_s: Maximum wait in seconds; must be finite and positive.
            on_open: Optional callback invoked with the new consultation after it
                is registered and before waiting. It runs outside the lock, so it
                may call :meth:`answer` or :meth:`cancel`.

        Raises:
            ValueError: If *question* or *timeout_s* is invalid.
            RuntimeError: If another consultation is already pending.
        """

        text = question.strip()
        if not text:
            raise ValueError("question must not be empty or whitespace")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise ValueError(
                f"timeout_s must be a finite number greater than 0 (got {timeout_s!r})"
            )

        with self._lock:
            if self._pending is not None:
                raise RuntimeError("a consultation is already pending")
            consultation = Consultation(
                id=self._id_factory(),
                question=text,
                created_at=self._clock(),
            )
            pending = _Pending(
                consultation=consultation,
                event=threading.Event(),
                deadline=time.monotonic() + timeout_s,
            )
            self._pending = pending

        try:
            if on_open is not None:
                on_open(consultation)

            timed_out = False
            while True:
                remaining = pending.deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                if pending.event.wait(remaining):
                    break

            with self._lock:
                if pending.event.is_set():
                    answer = pending.answer
                    timed_out = pending.timed_out
                else:
                    pending.timed_out = True
                    pending.resolved = True
                    answer = None
                    timed_out = True
            return ConsultationOutcome(
                consultation=consultation,
                answer=answer,
                timed_out=timed_out,
            )
        finally:
            with self._lock:
                if self._pending is pending:
                    self._pending = None

    def answer(self, text: str, *, consultation_id: str | None = None) -> bool:
        """Resolve the pending consultation with *text*.

        Returns ``False`` when nothing is pending, when *consultation_id* does
        not match, or when the consultation has already been resolved or timed
        out.

        Raises:
            ValueError: If *text* is empty or whitespace.
        """

        answer = text.strip()
        if not answer:
            raise ValueError("answer must not be empty or whitespace")

        with self._lock:
            pending = self._pending
            if pending is None or pending.resolved:
                return False
            if consultation_id is not None and consultation_id != pending.consultation.id:
                return False
            if time.monotonic() >= pending.deadline:
                pending.timed_out = True
                pending.resolved = True
                pending.event.set()
                return False
            pending.answer = answer
            pending.resolved = True
            pending.event.set()
            return True

    def cancel(self) -> bool:
        """Cancel the pending consultation, if any.

        Returns ``True`` when a pending consultation was cancelled. The waiter
        receives an outcome with ``answer=None`` and ``timed_out=False``.
        """

        with self._lock:
            pending = self._pending
            if pending is None or pending.resolved:
                return False
            pending.resolved = True
            pending.event.set()
            return True
