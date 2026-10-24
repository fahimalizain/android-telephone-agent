"""Tests for the long-lived call task service.

The project does not depend on pytest-asyncio, so every test is synchronous and
drives an inner async scenario with ``asyncio.run``. Sessions are fakes: no real
audio, dialer, or network is involved.
"""

from __future__ import annotations

import asyncio
import json
import math
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from telephone_agent.consult import ConsultationBroker
from telephone_agent.dialer import mask_number
from telephone_agent.pipeline import (
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
from telephone_agent.service import (
    TERMINAL_STATES,
    CallService,
    CallTask,
    TaskEvent,
    TaskState,
)

_NUMBER = "+15555550100"
_MAX_WAIT = 2.0

_TO_DICT_KEYS = {
    "task_id",
    "objective",
    "number",
    "state",
    "created_at",
    "max_call_seconds",
    "started_at",
    "ended_at",
    "turns",
    "result",
    "error",
    "pending_question",
    "transcript",
}


def _result(
    *,
    state: CallState = CallState.IDLE,
    reason: CallEndReason = CallEndReason.COMPLETED,
    turns: int = 1,
    duration_s: float = 0.25,
) -> CallResult:
    return CallResult(state=state, reason=reason, duration_s=duration_s, turns=turns)


class _Session:
    """Minimal SessionLike backed by a callable."""

    def __init__(self, run: Callable[[], CallResult]) -> None:
        self._run = run

    def run(self) -> CallResult:
        return self._run()


class _Recorder:
    """Session factory that records the keyword arguments it receives."""

    def __init__(self, builder: Callable[..., _Session]) -> None:
        self._builder = builder
        self.calls: list[dict[str, object]] = []

    def __call__(
        self,
        *,
        task: CallTask,
        broker: ConsultationBroker,
        stop_event: threading.Event,
        on_event: Callable[[SessionEvent], None],
    ) -> _Session:
        self.calls.append(
            {
                "task": task,
                "broker": broker,
                "stop_event": stop_event,
                "on_event": on_event,
            }
        )
        return self._builder(
            task=task,
            broker=broker,
            stop_event=stop_event,
            on_event=on_event,
        )


async def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout_s: float = _MAX_WAIT,
) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), timeout_s)


async def _collect(service: CallService, task_id: str) -> list[TaskEvent]:
    return [event async for event in service.subscribe(task_id)]


def test_submit_completes_and_masks_number() -> None:
    async def scenario() -> None:
        result = _result(turns=2)
        recorder = _Recorder(lambda **_: _Session(lambda: result))
        service = CallService(session_factory=recorder, default_number=_NUMBER)
        await service.start()
        try:
            task = await service.submit(
                objective="  Confirm the delivery.  ",
                max_call_seconds=60,
            )
            assert task.state is TaskState.QUEUED
            assert task.objective == "Confirm the delivery."
            assert task.number == _NUMBER
            assert task.max_call_seconds == 60

            events_task = asyncio.create_task(_collect(service, task.id))
            finished = await service.wait(task.id, timeout_s=_MAX_WAIT)
            assert finished is task
            assert finished.state is TaskState.COMPLETED
            assert finished.result is result
            assert finished.turns == 2
            assert finished.error is None
            assert finished.started_at is not None
            assert finished.ended_at is not None
            assert finished.ended_at >= finished.started_at
            assert finished.pending_consultation is None

            assert len(recorder.calls) == 1
            call = recorder.calls[0]
            assert call["task"] is task
            assert isinstance(call["broker"], ConsultationBroker)
            assert isinstance(call["stop_event"], threading.Event)
            assert callable(call["on_event"])

            payload = finished.to_dict()
            assert set(payload) == _TO_DICT_KEYS
            assert payload["task_id"] == task.id
            assert payload["state"] == "completed"
            assert payload["number"] == mask_number(_NUMBER)
            assert _NUMBER not in json.dumps(payload)
            assert payload["result"] == {
                "state": "idle",
                "reason": "completed",
                "duration_s": 0.25,
                "turns": 2,
            }
            assert payload["transcript"] == []
            assert payload["pending_question"] is None

            events = await asyncio.wait_for(events_task, _MAX_WAIT)
            assert [event.kind for event in events] == [
                "state",
                "state",
                "state",
                "finished",
            ]
            assert [event.data["state"] for event in events if event.kind == "state"] == [
                "queued",
                "dialing",
                "completed",
            ]
            assert events[-1].data == payload
            assert sum(event.kind == "finished" for event in events) == 1
            assert all(event.task_id == task.id for event in events)
            assert finished.state in TERMINAL_STATES
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_one_call_at_a_time() -> None:
    async def scenario() -> None:
        release = threading.Event()
        first_started = threading.Event()

        def build(*, task: CallTask, **_: object) -> _Session:
            def run() -> CallResult:
                if task.objective == "first":
                    first_started.set()
                    release.wait(_MAX_WAIT)
                return _result()

            return _Session(run)

        recorder = _Recorder(build)
        service = CallService(session_factory=recorder, default_number=_NUMBER)
        await service.start()
        try:
            first = await service.submit(objective="first")
            await _wait_until(first_started.is_set)
            second = await service.submit(objective="second")
            await asyncio.sleep(0)

            assert first.state is TaskState.DIALING
            assert second.state is TaskState.QUEUED
            assert [call["task"].objective for call in recorder.calls] == ["first"]

            release.set()
            assert await service.wait(first.id, timeout_s=_MAX_WAIT) is first
            assert await service.wait(second.id, timeout_s=_MAX_WAIT) is second
            assert first.state is TaskState.COMPLETED
            assert second.state is TaskState.COMPLETED
            assert [call["task"].objective for call in recorder.calls] == ["first", "second"]

            all_tasks = await service.list_tasks()
            assert [task.id for task in all_tasks] == [first.id, second.id]
            completed = await service.list_tasks(state=TaskState.COMPLETED)
            assert [task.id for task in completed] == [first.id, second.id]
            assert await service.list_tasks(state=TaskState.FAILED) == []
        finally:
            release.set()
            await service.stop()

    asyncio.run(scenario())


def test_cancel_queued_task_does_not_call_factory() -> None:
    async def scenario() -> None:
        release = threading.Event()
        first_started = threading.Event()

        def build(*, task: CallTask, **_: object) -> _Session:
            def run() -> CallResult:
                if task.objective == "first":
                    first_started.set()
                    release.wait(_MAX_WAIT)
                return _result()

            return _Session(run)

        recorder = _Recorder(build)
        service = CallService(session_factory=recorder, default_number=_NUMBER)
        await service.start()
        try:
            first = await service.submit(objective="first")
            await _wait_until(first_started.is_set)
            second = await service.submit(objective="second")
            assert second.state is TaskState.QUEUED

            assert await service.cancel(second.id) is True
            assert second.state is TaskState.CANCELED
            assert second.ended_at is not None
            assert second.cancel_requested is True
            assert [call["task"].objective for call in recorder.calls] == ["first"]
            assert await service.cancel(second.id) is False

            events = [event async for event in service.subscribe(second.id)]
            assert [event.data["state"] for event in events if event.kind == "state"] == [
                "queued",
                "canceled",
            ]
            assert events[-1].kind == "finished"
            assert events[-1].data["state"] == "canceled"

            release.set()
            assert await service.wait(first.id, timeout_s=_MAX_WAIT) is first
            assert first.state is TaskState.COMPLETED
            assert [call["task"].objective for call in recorder.calls] == ["first"]
        finally:
            release.set()
            await service.stop()

    asyncio.run(scenario())


def test_cancel_in_call_finalizes_canceled() -> None:
    async def scenario() -> None:
        def build(
            *, on_event: Callable[[SessionEvent], None], stop_event: threading.Event, **_: object
        ) -> _Session:
            def run() -> CallResult:
                on_event(StateChanged(CallState.DIALING))
                on_event(StateChanged(CallState.IN_CALL))
                stop_event.wait(_MAX_WAIT)
                return _result(reason=CallEndReason.INTERRUPTED, turns=0)

            return _Session(run)

        recorder = _Recorder(build)
        service = CallService(session_factory=recorder, default_number=_NUMBER)
        await service.start()
        try:
            task = await service.submit(objective="Cancel me.")
            events_task = asyncio.create_task(_collect(service, task.id))
            await _wait_until(lambda: task.state is TaskState.IN_CALL)

            assert await service.cancel(task.id) is True
            assert task.state is TaskState.IN_CALL
            assert task.cancel_requested is True
            stop_event = recorder.calls[0]["stop_event"]
            assert isinstance(stop_event, threading.Event)
            assert stop_event.is_set()

            finished = await service.wait(task.id, timeout_s=_MAX_WAIT)
            assert finished is task
            assert task.state is TaskState.CANCELED
            assert task.result is not None
            assert task.result.reason is CallEndReason.INTERRUPTED
            assert task.error is None

            events = await asyncio.wait_for(events_task, _MAX_WAIT)
            finished_events = [event for event in events if event.kind == "finished"]
            assert len(finished_events) == 1
            assert finished_events[0].data["state"] == "canceled"
            assert [event.data["state"] for event in events if event.kind == "state"] == [
                "queued",
                "dialing",
                "in_call",
                "canceled",
            ]
            assert await service.cancel(task.id) is False
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_consultation_answer_resumes_call() -> None:
    async def scenario() -> None:
        def build(
            *,
            broker: ConsultationBroker,
            on_event: Callable[[SessionEvent], None],
            **_: object,
        ) -> _Session:
            def run() -> CallResult:
                on_event(StateChanged(CallState.DIALING))
                on_event(StateChanged(CallState.IN_CALL))
                on_event(Utterance("I need the door code."))
                on_event(AgentReply("Let me check."))
                outcome = broker.ask(
                    "What is the door code?",
                    _MAX_WAIT,
                    on_open=lambda consultation: on_event(ConsultationStarted(consultation)),
                )
                on_event(ConsultationFinished(outcome))
                on_event(AgentReply("Thanks, I have it."))
                result = _result(turns=2)
                on_event(CallFinished(result))
                return result

            return _Session(run)

        service = CallService(session_factory=build, default_number=_NUMBER)
        await service.start()
        try:
            task = await service.submit(objective="Get the door code.")
            events_task = asyncio.create_task(_collect(service, task.id))
            await _wait_until(lambda: task.state is TaskState.INPUT_REQUIRED)

            pending = task.pending_consultation
            assert pending is not None
            assert task.to_dict()["pending_question"] == "What is the door code?"
            assert await service.answer(task.id, "  42  ") is True
            assert await service.answer(task.id, "again") is False

            finished = await service.wait(task.id, timeout_s=_MAX_WAIT)
            assert finished is task
            assert task.state is TaskState.COMPLETED
            assert task.turns == 2
            assert task.pending_consultation is None
            assert task.result is not None

            events = await asyncio.wait_for(events_task, _MAX_WAIT)
            consultations = [event for event in events if event.kind == "consultation"]
            assert [event.data["status"] for event in consultations] == ["pending", "answered"]
            assert consultations[0].data["consultation_id"] == pending.id
            assert consultations[1].data["consultation_id"] == pending.id
            assert consultations[1].data["answer"] == "42"
            assert consultations[1].data["timed_out"] is False
            assert [event.data["state"] for event in events if event.kind == "state"] == [
                "queued",
                "dialing",
                "in_call",
                "input_required",
                "in_call",
                "completed",
            ]
            assert [event.data for event in events if event.kind == "transcript"] == [
                {"role": "callee", "text": "I need the door code."}
            ]
            assert [event.data["text"] for event in events if event.kind == "reply"] == [
                "Let me check.",
                "Thanks, I have it.",
            ]
            assert task.transcript == [
                {"role": "callee", "text": "I need the door code."},
                {"role": "agent", "text": "Let me check."},
                {"role": "agent", "text": "Thanks, I have it."},
            ]
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_answer_after_consultation_timeout_returns_false() -> None:
    async def scenario() -> None:
        gave_up = threading.Event()
        release = threading.Event()

        def build(
            *,
            broker: ConsultationBroker,
            on_event: Callable[[SessionEvent], None],
            **_: object,
        ) -> _Session:
            def run() -> CallResult:
                on_event(StateChanged(CallState.IN_CALL))
                outcome = broker.ask(
                    "Which Friday?",
                    0.05,
                    on_open=lambda consultation: on_event(ConsultationStarted(consultation)),
                )
                on_event(ConsultationFinished(outcome))
                gave_up.set()
                release.wait(_MAX_WAIT)
                return _result()

            return _Session(run)

        service = CallService(session_factory=build, default_number=_NUMBER)
        await service.start()
        try:
            task = await service.submit(objective="Pick a Friday.")
            await _wait_until(gave_up.is_set)
            await _wait_until(lambda: task.state is TaskState.IN_CALL)

            assert task.pending_consultation is None
            assert await service.answer(task.id, "Friday 30 October") is False

            release.set()
            finished = await service.wait(task.id, timeout_s=_MAX_WAIT)
            assert finished is task
            assert task.state is TaskState.COMPLETED

            events = [event async for event in service.subscribe(task.id)]
            consultations = [event for event in events if event.kind == "consultation"]
            assert [event.data["status"] for event in consultations] == ["pending", "timeout"]
            assert consultations[-1].data["answer"] is None
            assert consultations[-1].data["timed_out"] is True
        finally:
            release.set()
            await service.stop()

    asyncio.run(scenario())


def test_factory_exception_fails_task() -> None:
    async def scenario() -> None:
        def factory(**_: object) -> _Session:
            raise RuntimeError("f" * 600)

        service = CallService(session_factory=factory, default_number=_NUMBER)
        await service.start()
        try:
            task = await service.submit(objective="Explode.")
            finished = await service.wait(task.id, timeout_s=_MAX_WAIT)
            assert finished is task
            assert task.state is TaskState.FAILED
            assert task.result is None
            assert task.error is not None
            assert len(task.error) == 500
            assert task.started_at is not None
            assert task.ended_at is not None

            events = [event async for event in service.subscribe(task.id)]
            assert [event.kind for event in events[-3:]] == ["error", "state", "finished"]
            assert events[-3].data == {"error": task.error}
            assert events[-2].data == {"state": "failed"}
            assert events[-1].data == task.to_dict()
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_session_exception_fails_task() -> None:
    async def scenario() -> None:
        def build(
            *,
            on_event: Callable[[SessionEvent], None],
            **_: object,
        ) -> _Session:
            def run() -> CallResult:
                on_event(StateChanged(CallState.IN_CALL))
                raise ValueError("session exploded")

            return _Session(run)

        service = CallService(session_factory=build, default_number=_NUMBER)
        await service.start()
        try:
            task = await service.submit(objective="Fail in the session.")
            finished = await service.wait(task.id, timeout_s=_MAX_WAIT)
            assert finished is task
            assert task.state is TaskState.FAILED
            assert task.error == "session exploded"
            assert task.result is None

            events = [event async for event in service.subscribe(task.id)]
            assert [event.data["state"] for event in events if event.kind == "state"] == [
                "queued",
                "dialing",
                "in_call",
                "failed",
            ]
            assert [event.kind for event in events[-3:]] == ["error", "state", "finished"]
            assert events[-3].data == {"error": "session exploded"}
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_wait_timeout_and_unknown_tasks() -> None:
    async def scenario() -> None:
        release = threading.Event()

        def factory(**_: object) -> _Session:
            def run() -> CallResult:
                release.wait(_MAX_WAIT)
                return _result()

            return _Session(run)

        service = CallService(session_factory=factory, default_number=_NUMBER)
        await service.start()
        await service.start()  # idempotent
        try:
            task = await service.submit(objective="Slow call.")
            assert await service.wait(task.id, timeout_s=0.05) is None

            assert await service.get("missing") is None
            assert await service.cancel("missing") is False
            assert await service.answer("missing", "hello") is False
            assert await service.wait("missing", timeout_s=0.1) is None
            with pytest.raises(KeyError):
                await anext(service.subscribe("missing"))
            with pytest.raises(ValueError, match="timeout_s"):
                await service.wait(task.id, timeout_s=0)
            with pytest.raises(ValueError, match="timeout_s"):
                await service.wait(task.id, timeout_s=-1.0)
            with pytest.raises(ValueError, match="timeout_s"):
                await service.wait(task.id, timeout_s=math.inf)

            release.set()
            finished = await service.wait(task.id, timeout_s=_MAX_WAIT)
            assert finished is task
            assert task.state is TaskState.COMPLETED
        finally:
            release.set()
            await service.stop()

    asyncio.run(scenario())


def test_audit_log_is_jsonl_and_never_exposes_raw_number(tmp_path: Path) -> None:
    async def scenario() -> None:
        audit_path = tmp_path / "nested" / "audit.jsonl"
        service = CallService(
            session_factory=lambda **_: _Session(lambda: _result()),
            default_number=_NUMBER,
            audit_path=audit_path,
        )
        await service.start()
        try:
            task = await service.submit(objective="Audit this call.")
            finished = await service.wait(task.id, timeout_s=_MAX_WAIT)
            assert finished is task
        finally:
            await service.stop()

        assert audit_path.exists()
        text = audit_path.read_text(encoding="utf-8")
        records = [json.loads(line) for line in text.splitlines()]
        assert records
        for record in records:
            assert set(record) == {"ts", "at", "kind", "task_id", "data"}
            assert isinstance(record["at"], (int, float))
            assert isinstance(record["ts"], str) and "T" in record["ts"]
            assert record["task_id"] == task.id

        finished_records = [record for record in records if record["kind"] == "finished"]
        assert len(finished_records) == 1
        assert finished_records[0]["data"]["number"] == mask_number(_NUMBER)
        assert finished_records[0]["data"]["state"] == "completed"
        assert "transcript" not in finished_records[0]["data"]
        assert "objective" not in finished_records[0]["data"]
        assert _NUMBER not in text
        assert _NUMBER.removeprefix("+") not in text
        assert "Audit this call." not in text

    asyncio.run(scenario())


def test_submit_validation() -> None:
    async def scenario() -> None:
        def factory(**_: object) -> _Session:
            raise AssertionError("factory must not be called")

        service = CallService(session_factory=factory)
        await service.stop()  # no-op before start
        with pytest.raises(RuntimeError, match="start"):
            await service.submit(objective="before start", number=_NUMBER)

        await service.start()
        try:
            with pytest.raises(ValueError, match="objective"):
                await service.submit(objective="   ", number=_NUMBER)
            with pytest.raises(ValueError, match="objective"):
                await service.submit(objective="x" * 4001, number=_NUMBER)
            with pytest.raises(ValueError, match="E.164"):
                await service.submit(objective="bad number", number="555")
            with pytest.raises(ValueError, match="E.164"):
                await service.submit(objective="no number")
            with pytest.raises(ValueError, match="max_call_seconds"):
                await service.submit(objective="zero", number=_NUMBER, max_call_seconds=0)
            with pytest.raises(ValueError, match="max_call_seconds"):
                await service.submit(
                    objective="negative",
                    number=_NUMBER,
                    max_call_seconds=-1.0,
                )
            with pytest.raises(ValueError, match="max_call_seconds"):
                await service.submit(
                    objective="nan",
                    number=_NUMBER,
                    max_call_seconds=math.nan,
                )
            with pytest.raises(ValueError, match="max_call_seconds"):
                await service.submit(
                    objective="inf",
                    number=_NUMBER,
                    max_call_seconds=math.inf,
                )
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_stop_cancels_active_and_queued_tasks() -> None:
    async def scenario() -> None:
        active_started = threading.Event()

        def build(
            *,
            on_event: Callable[[SessionEvent], None],
            stop_event: threading.Event,
            **_: object,
        ) -> _Session:
            def run() -> CallResult:
                on_event(StateChanged(CallState.IN_CALL))
                active_started.set()
                stop_event.wait(_MAX_WAIT)
                return _result(reason=CallEndReason.INTERRUPTED, turns=0)

            return _Session(run)

        service = CallService(session_factory=_Recorder(build), default_number=_NUMBER)
        await service.start()
        try:
            active = await service.submit(objective="active")
            await _wait_until(active_started.is_set)
            queued = await service.submit(objective="queued")
            assert queued.state is TaskState.QUEUED

            await service.stop()
            assert active.state is TaskState.CANCELED
            assert queued.state is TaskState.CANCELED
            assert active.ended_at is not None
            assert queued.ended_at is not None
            assert await service.wait(active.id, timeout_s=0.1) is active
            assert await service.wait(queued.id, timeout_s=0.1) is queued
            with pytest.raises(RuntimeError, match="start"):
                await service.submit(objective="too late", number=_NUMBER)
        finally:
            await service.stop()

    asyncio.run(scenario())
