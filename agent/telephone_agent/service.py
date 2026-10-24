"""Long-lived, protocol-agnostic call task service.

:class:`CallService` owns a FIFO queue of outbound-call tasks and runs at most
one :class:`~telephone_agent.pipeline.CallSession` at a time. The future MCP and
A2A adapters translate protocol objects to and from this API; neither adapter
owns call state, queueing, cancellation, or consultations.

Concurrency model:

* Task state and event publication happen on the asyncio event loop that
  called :meth:`CallService.start`. The service is not safe to drive from more
  than one event loop.
* The session itself runs in a worker thread because the audio loop blocks.
  Its ``on_event`` callback marshals back to the loop with
  ``loop.call_soon_threadsafe``. Events that arrive after the task is terminal
  or after the service stopped are dropped.
* Only the per-task ``threading.Event`` and
  :class:`~telephone_agent.consult.ConsultationBroker` cross the thread
  boundary. Both are thread-safe.

Cancellation is cooperative. ``CallSession`` checks its ``stop_event`` only
between audio frames, so cancelling an active call may take until the end of the
current turn or frame. A pending consultation is cancelled immediately through
the broker, which wakes the call thread.

Tasks and their event history live in memory; a restart loses them. The
optional audit log is append-only JSONL and records every emitted
:class:`TaskEvent` with a wall-clock timestamp. The objective and transcript
are personal data: they are omitted from the final ``finished`` audit record
even though the in-memory event carries them.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import math
import os
import threading
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from .config import ensure_e164
from .consult import Consultation, ConsultationBroker
from .dialer import mask_number
from .pipeline import (
    AgentReply,
    CallEndReason,
    CallFinished,
    CallResult,
    CallState,
    ConsultationFinished,
    ConsultationStarted,
    SessionEvent,
    StateChanged,
    Utterance,
)

__all__ = [
    "CallService",
    "CallTask",
    "SessionFactory",
    "SessionLike",
    "TERMINAL_STATES",
    "TaskEvent",
    "TaskState",
]

logger = logging.getLogger(__name__)

_MAX_OBJECTIVE_CHARS = 4000
_MAX_ERROR_CHARS = 500
_STOP_TIMEOUT_S = 2.0
_FAILED_REASONS = frozenset({CallEndReason.TURN_FAILED, CallEndReason.HANGUP_FAILED})


class TaskState(enum.StrEnum):
    """Lifecycle of one task owned by :class:`CallService`."""

    QUEUED = "queued"
    DIALING = "dialing"
    IN_CALL = "in_call"
    INPUT_REQUIRED = "input_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"


TERMINAL_STATES = frozenset(
    {
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CANCELED,
    }
)


@dataclass(frozen=True, slots=True)
class TaskEvent:
    """One ordered event in a task's history.

    ``kind`` is one of ``state``, ``transcript``, ``reply``, ``consultation``,
    ``error``, or ``finished``. ``at`` is a monotonic clock reading, not a
    wall-clock timestamp; the audit record carries the same value.
    """

    task_id: str
    kind: str
    at: float
    data: Mapping[str, object]


@dataclass(slots=True)
class CallTask:
    """One outbound call and its accumulated state.

    The service owns mutation of these fields. Adapters may read them, but
    should serialise through :meth:`to_dict` and treat the values as snapshots.
    """

    id: str
    objective: str
    number: str
    state: TaskState
    created_at: float
    max_call_seconds: float | None = None
    started_at: float | None = None
    ended_at: float | None = None
    turns: int = 0
    result: CallResult | None = None
    error: str | None = None
    pending_consultation: Consultation | None = None
    transcript: list[dict[str, str]] = field(default_factory=list)
    cancel_requested: bool = False

    def to_dict(self) -> dict[str, object]:
        """Return a serialisable snapshot, with the destination masked.

        The number is always masked with
        :func:`~telephone_agent.dialer.mask_number`; raw E.164 digits never
        appear in the returned mapping.
        """

        result: dict[str, object] | None = None
        if self.result is not None:
            result = {
                "state": self.result.state.value,
                "reason": self.result.reason.value,
                "duration_s": self.result.duration_s,
                "turns": self.result.turns,
            }

        return {
            "task_id": self.id,
            "objective": self.objective,
            "number": mask_number(self.number),
            "state": self.state.value,
            "created_at": self.created_at,
            "max_call_seconds": self.max_call_seconds,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "turns": self.turns,
            "result": result,
            "error": self.error,
            "pending_question": (
                self.pending_consultation.question
                if self.pending_consultation is not None
                else None
            ),
            "transcript": list(self.transcript),
        }


class SessionLike(Protocol):
    """The one method :class:`CallService` needs from a call session."""

    def run(self) -> CallResult:
        """Run the call to completion and return its result."""


SessionFactory = Callable[..., SessionLike]


@dataclass(slots=True)
class _TaskRuntime:
    """Thread-crossing state for one active task.

    Session events are appended from the call thread and drained on the event
    loop. Draining at finalization as well as from the loop callback means a
    burst of events emitted just before ``run()`` returns cannot be lost to a
    wakeup race.
    """

    stop_event: threading.Event
    broker: ConsultationBroker
    closed: bool = False
    pending_events: deque[SessionEvent] = field(default_factory=deque)
    events_lock: threading.Lock = field(default_factory=threading.Lock)


class CallService:
    """Queue and run outbound call tasks one at a time.

    Args:
        session_factory: Builds a session. Called with the keyword arguments
            ``task``, ``broker``, ``stop_event``, and ``on_event`` from the
            worker thread. It may block; it must not touch task state.
        default_number: E.164 destination used when ``submit`` omits one.
        audit_path: Optional JSONL path. Every emitted event is appended
            synchronously under a lock. Audit failures are logged and ignored.
        clock: Monotonic clock for task and event timestamps. Injectable for
            tests.
        id_factory: Supplies task identifiers. Injectable for tests.

    Use :meth:`start` before :meth:`submit`, and :meth:`stop` on shutdown. The
    service is intended for one event loop for its whole lifetime.
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        default_number: str = "",
        audit_path: str | os.PathLike[str] | None = None,
        clock: Callable[[], float] = time.monotonic,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._session_factory = session_factory
        self._default_number = default_number
        self._audit_path = Path(audit_path) if audit_path is not None else None
        self._clock = clock
        self._id_factory = id_factory

        self._tasks: dict[str, CallTask] = {}
        self._events: dict[str, list[TaskEvent]] = {}
        self._subscribers: dict[str, set[asyncio.Queue[TaskEvent]]] = {}
        self._runtimes: dict[str, _TaskRuntime] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._audit_lock = threading.Lock()

        self._worker: asyncio.Task[None] | None = None
        self._current: CallTask | None = None
        self._started = False
        self._stopping = False

    async def start(self) -> None:
        """Create the single worker task. Idempotent while already running."""

        if self._worker is not None and not self._worker.done():
            return
        self._started = True
        self._stopping = False
        self._worker = asyncio.create_task(self._run_worker(), name="call-service-worker")

    async def stop(self) -> None:
        """Stop the worker and terminate queued and active work.

        Active sessions are asked to stop through their ``stop_event`` and any
        pending consultation is cancelled. The worker is given a short grace
        period to finish, then it is cancelled. Cancelling the worker cannot
        interrupt a thread already inside the audio loop, so the session thread
        may outlive the service by one turn; it is then ignored. Idempotent, and
        a no-op before :meth:`start`.
        """

        worker = self._worker
        if worker is None:
            self._started = False
            return

        self._stopping = True
        for task in list(self._tasks.values()):
            if task.state in TERMINAL_STATES:
                continue
            task.cancel_requested = True
            if task.state is TaskState.QUEUED:
                self._mark_terminal(task, TaskState.CANCELED)
                continue
            runtime = self._runtimes.get(task.id)
            if runtime is not None:
                runtime.stop_event.set()
                runtime.broker.cancel()

        try:
            if self._current is None:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)
            else:
                try:
                    await asyncio.wait_for(asyncio.shield(worker), timeout=_STOP_TIMEOUT_S)
                except TimeoutError:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
        except asyncio.CancelledError:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
            raise
        except Exception:
            logger.warning("call service worker ended with an error", exc_info=True)
        finally:
            self._worker = None
            self._started = False
            self._stopping = False

    async def submit(
        self,
        *,
        objective: str,
        number: str | None = None,
        max_call_seconds: float | None = None,
    ) -> CallTask:
        """Validate, record, and enqueue one call task.

        Args:
            objective: What the agent should accomplish. Trimmed; must be
                non-empty and at most 4000 characters.
            number: E.164 destination. Falls back to the service default when
                omitted or empty.
            max_call_seconds: Optional hard call limit. Must be finite and
                greater than zero.

        Raises:
            RuntimeError: If the service is not running.
            ValueError: If the objective, number, or duration is invalid.

        Returns immediately; the task stays ``QUEUED`` until the active call
        ends.
        """

        if not self._started or self._stopping or self._worker is None or self._worker.done():
            raise RuntimeError("CallService.start() must be called before submit()")

        text = objective.strip()
        if not text:
            raise ValueError("objective must not be empty or whitespace")
        if len(text) > _MAX_OBJECTIVE_CHARS:
            raise ValueError(
                f"objective must be at most {_MAX_OBJECTIVE_CHARS} characters (got {len(text)})"
            )
        resolved_number = ensure_e164(number or self._default_number or "")
        if max_call_seconds is not None and (
            not math.isfinite(max_call_seconds) or max_call_seconds <= 0
        ):
            raise ValueError(
                "max_call_seconds must be a finite number greater than 0 "
                f"(got {max_call_seconds!r})"
            )

        task = CallTask(
            id=self._id_factory(),
            objective=text,
            number=resolved_number,
            state=TaskState.QUEUED,
            created_at=self._clock(),
            max_call_seconds=max_call_seconds,
        )
        self._tasks[task.id] = task
        self._events.setdefault(task.id, [])
        self._emit(task, "state", {"state": TaskState.QUEUED.value})
        self._queue.put_nowait(task.id)
        return task

    async def get(self, task_id: str) -> CallTask | None:
        """Return the live task, or ``None`` if the identifier is unknown."""

        return self._tasks.get(task_id)

    async def list_tasks(self, *, state: TaskState | None = None) -> list[CallTask]:
        """Return tasks sorted by creation time then id, optionally filtered.

        The returned objects are the live tasks, not copies. Adapters should
        call :meth:`CallTask.to_dict` before serialising and must not mutate
        them.
        """

        tasks = list(self._tasks.values())
        if state is not None:
            tasks = [task for task in tasks if task.state == state]
        tasks.sort(key=lambda task: (task.created_at, task.id))
        return tasks

    async def cancel(self, task_id: str) -> bool:
        """Request cancellation of a queued or active task.

        Returns ``False`` for an unknown or terminal task. A queued task is
        finalized as ``CANCELED`` immediately and never dials. For an active
        task the request is recorded, the session's ``stop_event`` is set, and a
        pending consultation is cancelled; the worker finalizes the task as
        ``CANCELED`` after the session returns. Because ``CallSession`` checks
        its stop flag only between audio frames, that may take until the end of
        the current frame or turn.
        """

        task = self._tasks.get(task_id)
        if task is None or task.state in TERMINAL_STATES:
            return False

        task.cancel_requested = True
        if task.state is TaskState.QUEUED:
            self._mark_terminal(task, TaskState.CANCELED)
            return True

        runtime = self._runtimes.get(task.id)
        if runtime is not None:
            runtime.stop_event.set()
            runtime.broker.cancel()
        return True

    async def answer(self, task_id: str, text: str) -> bool:
        """Answer the pending consultation for *task_id*.

        Returns ``False`` when the task is unknown, is not waiting for input,
        has no pending consultation, or lost the race with its timeout. A blank
        answer is also rejected with ``False`` rather than raising, so adapters
        can map every refusal to a protocol-level conflict.
        """

        task = self._tasks.get(task_id)
        if task is None or task.state is not TaskState.INPUT_REQUIRED:
            return False
        consultation = task.pending_consultation
        if consultation is None or not isinstance(text, str) or not text.strip():
            return False
        runtime = self._runtimes.get(task.id)
        if runtime is None:
            return False
        try:
            return runtime.broker.answer(text, consultation_id=consultation.id)
        except ValueError:
            return False

    async def wait(self, task_id: str, *, timeout_s: float | None = None) -> CallTask | None:
        """Wait for a task to reach a terminal state.

        Returns the live task when it finishes, ``None`` if it is unknown or the
        timeout expires. A timeout must be finite and greater than zero when
        supplied.
        """

        if timeout_s is not None and (not math.isfinite(timeout_s) or timeout_s <= 0):
            raise ValueError(
                f"timeout_s must be a finite number greater than 0 (got {timeout_s!r})"
            )
        if task_id not in self._tasks:
            return None

        async def consume() -> CallTask | None:
            async for event in self.subscribe(task_id):
                if event.kind == "finished":
                    break
            return self._tasks.get(task_id)

        if timeout_s is None:
            return await consume()
        try:
            return await asyncio.wait_for(consume(), timeout_s)
        except TimeoutError:
            return None

    def subscribe(self, task_id: str) -> AsyncIterator[TaskEvent]:
        """Stream a task's events, replaying history first.

        Raises:
            KeyError: Immediately if *task_id* is unknown.

        The returned iterator yields events in order until it has yielded the
        single ``finished`` event, then returns. Subscribers are independent and
        cannot slow the call thread: publication uses unbounded queues and
        ``put_nowait``.
        """

        if task_id not in self._tasks:
            raise KeyError(task_id)
        return self._subscribe(task_id)

    async def _subscribe(self, task_id: str) -> AsyncIterator[TaskEvent]:
        queue: asyncio.Queue[TaskEvent] = asyncio.Queue()
        # No await between registering the subscriber and snapshotting history,
        # so no event can fall into the gap.
        self._subscribers.setdefault(task_id, set()).add(queue)
        history = list(self._events.get(task_id, ()))
        try:
            for event in history:
                yield event
                if event.kind == "finished":
                    return
            while True:
                event = await queue.get()
                yield event
                if event.kind == "finished":
                    return
        finally:
            subscribers = self._subscribers.get(task_id)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    self._subscribers.pop(task_id, None)

    async def _run_worker(self) -> None:
        try:
            while not self._stopping:
                task_id = await self._queue.get()
                task = self._tasks.get(task_id)
                if task is None or task.state is not TaskState.QUEUED:
                    continue
                try:
                    await self._run_task(task)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("call task %s failed unexpectedly", task.id)
                    self._fail(task, exc)
        except asyncio.CancelledError:
            task = self._current
            if task is not None and task.state not in TERMINAL_STATES:
                task.cancel_requested = True
                runtime = self._runtimes.get(task.id)
                if runtime is not None:
                    runtime.stop_event.set()
                    runtime.broker.cancel()
                self._mark_terminal(task, TaskState.CANCELED)
            raise
        except Exception:
            logger.exception("call service worker stopped unexpectedly")
        finally:
            self._current = None

    async def _run_task(self, task: CallTask) -> None:
        runtime = _TaskRuntime(
            stop_event=threading.Event(),
            broker=ConsultationBroker(clock=self._clock),
        )
        self._runtimes[task.id] = runtime
        self._current = task
        task.started_at = self._clock()
        self._set_state(task, TaskState.DIALING)
        loop = asyncio.get_running_loop()

        def on_event(event: SessionEvent) -> None:
            with runtime.events_lock:
                runtime.pending_events.append(event)
            try:
                loop.call_soon_threadsafe(self._drain_session_events, task, runtime)
            except RuntimeError:
                logger.debug("event loop is closed; dropping event for task %s", task.id)

        def run_session() -> CallResult:
            session = self._session_factory(
                task=task,
                broker=runtime.broker,
                stop_event=runtime.stop_event,
                on_event=on_event,
            )
            return session.run()

        try:
            result = await asyncio.to_thread(run_session)
        except asyncio.CancelledError:
            self._drain_session_events(task, runtime)
            task.cancel_requested = True
            runtime.stop_event.set()
            runtime.broker.cancel()
            self._mark_terminal(task, TaskState.CANCELED)
            raise
        except Exception as exc:
            self._drain_session_events(task, runtime)
            logger.warning("call task %s failed: %s", task.id, _short_error(exc))
            self._fail(task, exc)
        else:
            self._drain_session_events(task, runtime)
            self._complete(task, result)
        finally:
            runtime.closed = True
            runtime.broker.cancel()
            self._runtimes.pop(task.id, None)
            self._current = None

    def _drain_session_events(self, task: CallTask, runtime: _TaskRuntime) -> None:
        """Apply all queued session events in order on the event loop."""

        while True:
            with runtime.events_lock:
                if not runtime.pending_events:
                    return
                event = runtime.pending_events.popleft()
            self._handle_session_event(task, runtime, event)

    def _handle_session_event(
        self,
        task: CallTask,
        runtime: _TaskRuntime,
        event: SessionEvent,
    ) -> None:
        if runtime.closed or task.state in TERMINAL_STATES:
            return
        try:
            if isinstance(event, StateChanged):
                self._handle_state_changed(task, event)
            elif isinstance(event, Utterance):
                task.transcript.append({"role": "callee", "text": event.text})
                self._emit(task, "transcript", {"role": "callee", "text": event.text})
            elif isinstance(event, AgentReply):
                task.transcript.append({"role": "agent", "text": event.text})
                self._emit(task, "reply", {"role": "agent", "text": event.text})
            elif isinstance(event, ConsultationStarted):
                self._handle_consultation_started(task, runtime, event.consultation)
            elif isinstance(event, ConsultationFinished):
                self._handle_consultation_finished(task, event)
            elif isinstance(event, CallFinished):
                task.result = event.result
        except Exception:
            logger.exception("failed to handle session event for task %s", task.id)

    def _handle_state_changed(self, task: CallTask, event: StateChanged) -> None:
        if task.state is TaskState.INPUT_REQUIRED:
            return
        if event.state == CallState.DIALING:
            if task.state is TaskState.QUEUED:
                self._set_state(task, TaskState.DIALING)
            return
        if event.state != CallState.IN_CALL:
            return
        if task.state is TaskState.DIALING:
            self._set_state(task, TaskState.IN_CALL)

    def _handle_consultation_started(
        self,
        task: CallTask,
        runtime: _TaskRuntime,
        consultation: Consultation,
    ) -> None:
        task.pending_consultation = consultation
        self._set_state(task, TaskState.INPUT_REQUIRED)
        self._emit(
            task,
            "consultation",
            {
                "consultation_id": consultation.id,
                "question": consultation.question,
                "status": "pending",
            },
        )
        if task.cancel_requested:
            # The call was cancelled before the session opened the question.
            # Resolve it now so a cancelled task cannot wait for the timeout.
            runtime.broker.cancel()

    def _handle_consultation_finished(
        self,
        task: CallTask,
        event: ConsultationFinished,
    ) -> None:
        outcome = event.outcome
        task.pending_consultation = None
        if task.state is TaskState.INPUT_REQUIRED:
            self._set_state(task, TaskState.IN_CALL)
        if outcome.answer is not None:
            status = "answered"
        elif outcome.timed_out:
            status = "timeout"
        else:
            status = "canceled"
        self._emit(
            task,
            "consultation",
            {
                "consultation_id": outcome.consultation.id,
                "answer": outcome.answer,
                "timed_out": outcome.timed_out,
                "status": status,
            },
        )

    def _set_state(self, task: CallTask, state: TaskState) -> None:
        if task.state is state:
            return
        task.state = state
        self._emit(task, "state", {"state": state.value})

    def _mark_terminal(self, task: CallTask, state: TaskState) -> bool:
        if task.state in TERMINAL_STATES:
            return False
        runtime = self._runtimes.get(task.id)
        if runtime is not None:
            runtime.closed = True
            runtime.broker.cancel()
        task.pending_consultation = None
        task.ended_at = self._clock()
        self._set_state(task, state)
        self._emit(task, "finished", task.to_dict())
        return True

    def _complete(self, task: CallTask, result: CallResult) -> None:
        if task.state in TERMINAL_STATES:
            return
        task.result = result
        task.turns = result.turns
        if task.cancel_requested or result.reason == CallEndReason.INTERRUPTED:
            final_state = TaskState.CANCELED
        elif result.state == CallState.FAILED or result.reason in _FAILED_REASONS:
            final_state = TaskState.FAILED
        else:
            final_state = TaskState.COMPLETED
        self._mark_terminal(task, final_state)

    def _fail(self, task: CallTask, exc: BaseException) -> None:
        if task.state in TERMINAL_STATES:
            return
        message = _short_error(exc)
        task.error = message
        self._emit(task, "error", {"error": message})
        self._mark_terminal(task, TaskState.FAILED)

    def _emit(
        self,
        task: CallTask,
        kind: str,
        data: Mapping[str, object],
    ) -> None:
        event = TaskEvent(
            task_id=task.id,
            kind=kind,
            at=self._clock(),
            data=dict(data),
        )
        self._events.setdefault(task.id, []).append(event)
        for queue in tuple(self._subscribers.get(task.id, ())):
            queue.put_nowait(event)
        self._write_audit(event)

    def _write_audit(self, event: TaskEvent) -> None:
        path = self._audit_path
        if path is None:
            return

        # The final snapshot carries the objective and transcript, which are
        # personal data. Keep them out of the audit log; callers that need the
        # transcript read it from the task before it leaves memory.
        data = dict(event.data)
        if event.kind == "finished":
            data.pop("objective", None)
            data.pop("transcript", None)

        record = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "at": event.at,
            "kind": event.kind,
            "task_id": event.task_id,
            "data": data,
        }
        try:
            with self._audit_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        except Exception:
            logger.warning("failed to append audit event; continuing", exc_info=True)


def _short_error(exc: BaseException) -> str:
    message = str(exc).strip() or type(exc).__name__
    return message[:_MAX_ERROR_CHARS]
