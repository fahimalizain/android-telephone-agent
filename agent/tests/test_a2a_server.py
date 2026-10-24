"""Tests for the A2A adapter over :class:`CallService`.

The project does not depend on pytest-asyncio, so every test drives an inner
async scenario with ``asyncio.run``. The route tests use an in-process ASGI
transport. The integration test uses the real service with a fake session, so
no audio, dialer, or network is involved.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import cast

import pytest

pytest.importorskip("a2a")
pytest.importorskip("starlette")

import httpx  # noqa: E402
from a2a.server.request_handlers.response_helpers import agent_card_to_dict  # noqa: E402
from a2a.types import TaskArtifactUpdateEvent, TaskState, TaskStatusUpdateEvent  # noqa: E402
from a2a.utils.constants import PROTOCOL_VERSION_CURRENT  # noqa: E402
from telephone_agent import __version__  # noqa: E402
from telephone_agent.consult import ConsultationBroker  # noqa: E402
from telephone_agent.pipeline import (  # noqa: E402
    AgentReply,
    CallEndReason,
    CallFinished,
    CallResult,
    CallState,
    SessionEvent,
    StateChanged,
    Utterance,
)
from telephone_agent.servers.a2a import (  # noqa: E402
    CallAgentExecutor,
    build_agent_card,
    create_a2a_app,
)
from telephone_agent.service import CallService, CallTask, TaskEvent  # noqa: E402
from telephone_agent.service import TaskState as CallTaskState

_TOKEN = "test-server-token"
_NUMBER = "+15555550100"
_A2A_TASK_ID = "a2a-task-1"
_A2A_CONTEXT_ID = "a2a-context-1"
_MAX_WAIT = 2.0


def _result(
    *,
    state: CallState = CallState.IDLE,
    reason: CallEndReason = CallEndReason.COMPLETED,
    turns: int = 1,
) -> CallResult:
    return CallResult(state=state, reason=reason, duration_s=0.25, turns=turns)


def _task(
    *,
    state: CallTaskState = CallTaskState.COMPLETED,
    transcript: list[dict[str, str]] | None = None,
    result: CallResult | None = None,
    error: str | None = None,
    turns: int = 0,
) -> CallTask:
    return CallTask(
        id="call-1",
        objective="Confirm the appointment.",
        number=_NUMBER,
        state=state,
        created_at=0.0,
        turns=turns,
        result=result,
        error=error,
        transcript=[] if transcript is None else transcript,
    )


def _event(kind: str, at: float, data: dict[str, object]) -> TaskEvent:
    return TaskEvent(task_id="call-1", kind=kind, at=at, data=data)


class _RecordingQueue:
    """Minimal EventQueue that records the protobuf objects the updater emits."""

    def __init__(self) -> None:
        self.events: list[object] = []

    async def enqueue_event(self, event: object) -> None:
        self.events.append(event)


@dataclass(slots=True)
class _Context:
    """The subset of RequestContext that the executor reads."""

    text: str
    task_id: str | None = _A2A_TASK_ID
    context_id: str | None = _A2A_CONTEXT_ID
    current_task: object | None = None

    def get_user_input(self) -> str:
        return self.text


class _FakeService:
    """Scripted CallService substitute that records adapter calls."""

    def __init__(
        self,
        *,
        events: list[TaskEvent],
        task: CallTask,
        answer_result: bool = True,
    ) -> None:
        self.events = events
        self.task = task
        self.answer_result = answer_result
        self.submitted: list[str] = []
        self.answers: list[tuple[str, str]] = []
        self.canceled: list[str] = []

    async def submit(self, *, objective: str) -> CallTask:
        self.submitted.append(objective)
        return self.task

    async def answer(self, task_id: str, text: str) -> bool:
        self.answers.append((task_id, text))
        return self.answer_result

    async def cancel(self, task_id: str) -> bool:
        self.canceled.append(task_id)
        return True

    async def get(self, task_id: str) -> CallTask | None:
        return self.task if task_id == self.task.id else None

    def subscribe(self, task_id: str) -> AsyncIterator[TaskEvent]:
        async def replay() -> AsyncIterator[TaskEvent]:
            for event in self.events:
                yield event

        return replay()


def _executor(service: _FakeService) -> CallAgentExecutor:
    return CallAgentExecutor(cast(CallService, service))


def _statuses(queue: _RecordingQueue) -> list[TaskStatusUpdateEvent]:
    return [event for event in queue.events if isinstance(event, TaskStatusUpdateEvent)]


def _artifacts(queue: _RecordingQueue) -> list[TaskArtifactUpdateEvent]:
    return [event for event in queue.events if isinstance(event, TaskArtifactUpdateEvent)]


def test_build_agent_card_describes_the_jsonrpc_skill() -> None:
    card = build_agent_card(url="https://agent.example.com/a2a")

    assert card.name == "telephone-agent"
    assert card.version == __version__
    assert len(card.supported_interfaces) == 1
    interface = card.supported_interfaces[0]
    assert interface.url == "https://agent.example.com/a2a"
    assert interface.protocol_binding == "JSONRPC"
    assert interface.protocol_version == PROTOCOL_VERSION_CURRENT
    assert card.capabilities.streaming is True
    assert card.capabilities.push_notifications is False
    assert list(card.default_input_modes) == ["text/plain"]
    assert list(card.default_output_modes) == ["text/plain"]

    assert len(card.skills) == 1
    skill = card.skills[0]
    assert skill.id == "place_phone_call"
    assert skill.name == "Place a phone call"
    assert list(skill.tags) == ["telephony", "voice", "outbound-call"]
    assert len(skill.examples) == 2
    assert list(skill.input_modes) == ["text/plain"]
    assert list(skill.output_modes) == ["text/plain"]

    document = json.dumps(agent_card_to_dict(card))
    assert _TOKEN not in document
    assert _NUMBER not in document
    assert "15555550100" not in document


def test_a2a_routes_health_and_card_are_public_but_rpc_requires_auth() -> None:
    async def scenario() -> None:
        service = CallService(session_factory=lambda **_: None)

        with pytest.raises(ValueError, match="token"):
            create_a2a_app(service, host="127.0.0.1", port=8766, token="")
        with pytest.raises(ValueError, match="token"):
            create_a2a_app(service, host="127.0.0.1", port=8766, token="   ")

        app = create_a2a_app(service, host="127.0.0.1", port=8766, token=_TOKEN)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8766",
        ) as client:
            health = await client.get("/health")
            assert health.status_code == 200
            assert health.json() == {
                "status": "ok",
                "service": "telephone-agent",
                "version": __version__,
            }

            card_response = await client.get("/.well-known/agent-card.json")
            assert card_response.status_code == 200
            card = card_response.json()
            assert card["name"] == "telephone-agent"
            assert card["skills"][0]["id"] == "place_phone_call"
            assert card["supportedInterfaces"][0]["url"] == "http://127.0.0.1:8766/a2a"

            missing = await client.post("/a2a", json={})
            assert missing.status_code == 401
            assert missing.json() == {"error": "unauthorized"}
            assert missing.headers["www-authenticate"] == "Bearer"

            malformed = await client.post(
                "/a2a",
                content=b"{not valid json",
                headers={
                    "Authorization": f"Bearer {_TOKEN}",
                    "Content-Type": "application/json",
                },
            )
            assert malformed.status_code != 401
            body = malformed.json()
            assert body.get("error") is not None or 400 <= malformed.status_code < 500

    asyncio.run(scenario())


def test_new_message_submits_a_call_and_asks_for_consultation_input() -> None:
    async def scenario() -> None:
        question = "What is the door code?"
        service = _FakeService(
            events=[
                _event("state", 1.0, {"state": "queued"}),
                _event("state", 2.0, {"state": "in_call"}),
                _event(
                    "consultation",
                    3.0,
                    {"consultation_id": "q-1", "question": question, "status": "pending"},
                ),
            ],
            task=_task(state=CallTaskState.INPUT_REQUIRED),
        )
        queue = _RecordingQueue()

        await _executor(service).execute(_Context("Get the door code."), queue)

        assert service.submitted == ["Get the door code."]
        assert [event.status.state for event in _statuses(queue)] == [
            TaskState.TASK_STATE_SUBMITTED,
            TaskState.TASK_STATE_WORKING,
            TaskState.TASK_STATE_INPUT_REQUIRED,
        ]
        pending = _statuses(queue)[-1].status.message
        assert pending.parts[0].text == question
        assert pending.task_id == _A2A_TASK_ID
        assert pending.context_id == _A2A_CONTEXT_ID
        assert not _artifacts(queue)

    asyncio.run(scenario())


def test_resume_answers_consultation_and_skips_replayed_history() -> None:
    async def scenario() -> None:
        question = "Which Friday should the delivery move to?"
        transcript = [{"role": "callee", "text": "Friday works."}]
        service = _FakeService(
            events=[
                _event("state", 1.0, {"state": "queued"}),
                _event("state", 2.0, {"state": "in_call"}),
                _event(
                    "consultation",
                    3.0,
                    {"consultation_id": "q-1", "question": question, "status": "pending"},
                ),
                _event("transcript", 4.0, {"role": "callee", "text": "Friday works."}),
                _event("state", 5.0, {"state": "completed"}),
                _event("finished", 6.0, {"state": "completed"}),
            ],
            task=_task(state=CallTaskState.COMPLETED, transcript=transcript, result=_result()),
        )
        executor = _executor(service)
        first_queue = _RecordingQueue()
        await executor.execute(_Context("Move the delivery."), first_queue)

        second_queue = _RecordingQueue()
        await executor.execute(_Context("Friday 30 October"), second_queue)

        assert service.answers == [("call-1", "Friday 30 October")]
        assert [event.status.state for event in _statuses(second_queue)] == [
            TaskState.TASK_STATE_WORKING,
            TaskState.TASK_STATE_COMPLETED,
        ]
        assert [event.status.state for event in _statuses(first_queue)].count(
            TaskState.TASK_STATE_INPUT_REQUIRED
        ) == 1
        assert all(
            event.status.state != TaskState.TASK_STATE_INPUT_REQUIRED
            for event in _statuses(second_queue)
        )

        artifacts = _artifacts(second_queue)
        assert len(artifacts) == 1
        assert artifacts[0].artifact.name == "call-summary"
        document = artifacts[0].artifact.parts[0].text
        payload = json.loads(document)
        assert payload == {
            "task_id": "call-1",
            "state": "completed",
            "objective": "Confirm the appointment.",
            "result": {
                "state": "idle",
                "reason": "completed",
                "duration_s": 0.25,
                "turns": 1,
            },
            "transcript": transcript,
        }
        assert _NUMBER not in document
        assert "15555550100" not in document

    asyncio.run(scenario())


def test_terminal_failure_reports_the_error() -> None:
    async def scenario() -> None:
        service = _FakeService(
            events=[
                _event("state", 1.0, {"state": "failed"}),
                _event("finished", 2.0, {"state": "failed"}),
            ],
            task=_task(state=CallTaskState.FAILED, error="dial failed"),
        )
        queue = _RecordingQueue()

        await _executor(service).execute(_Context("Try the call."), queue)

        statuses = _statuses(queue)
        assert statuses[-1].status.state == TaskState.TASK_STATE_FAILED
        assert statuses[-1].status.message.parts[0].text == "The call failed: dial failed"
        assert not _artifacts(queue)

    asyncio.run(scenario())


def test_cancel_is_mapped_and_idempotent() -> None:
    async def scenario() -> None:
        service = _FakeService(
            events=[
                _event(
                    "consultation",
                    1.0,
                    {"consultation_id": "q-1", "question": "Which one?", "status": "pending"},
                )
            ],
            task=_task(state=CallTaskState.INPUT_REQUIRED),
        )
        executor = _executor(service)

        unknown_queue = _RecordingQueue()
        await executor.cancel(
            _Context("", task_id="missing", context_id="ctx-missing"),
            unknown_queue,
        )
        assert service.canceled == []
        assert _statuses(unknown_queue)[-1].status.state == TaskState.TASK_STATE_CANCELED

        await executor.execute(_Context("Pick one."), _RecordingQueue())
        known_queue = _RecordingQueue()
        await executor.cancel(_Context(""), known_queue)

        assert service.canceled == ["call-1"]
        assert _statuses(known_queue)[-1].status.state == TaskState.TASK_STATE_CANCELED
        assert _statuses(known_queue)[-1].status.message.parts[0].text == "The call was canceled."

    asyncio.run(scenario())


def test_empty_objective_is_rejected_without_submitting() -> None:
    async def scenario() -> None:
        service = _FakeService(events=[], task=_task())
        queue = _RecordingQueue()
        context = _Context("   ", task_id="", context_id="ctx-from-message")

        await _executor(service).execute(context, queue)

        assert service.submitted == []
        status = _statuses(queue)[-1]
        assert status.status.state == TaskState.TASK_STATE_REJECTED
        assert status.status.message.parts[0].text == "An objective is required."
        assert status.task_id == "ctx-from-message"

    asyncio.run(scenario())


def test_existing_task_without_mapping_is_failed_after_a_restart() -> None:
    async def scenario() -> None:
        service = _FakeService(events=[], task=_task())
        queue = _RecordingQueue()

        await _executor(service).execute(
            _Context("Continue the call.", current_task=object()),
            queue,
        )

        assert service.submitted == []
        status = _statuses(queue)[-1]
        assert status.status.state == TaskState.TASK_STATE_FAILED
        assert status.status.message.parts[0].text == "This call is no longer active."

    asyncio.run(scenario())


class _Session:
    """Minimal SessionLike backed by a callable."""

    def __init__(self, run: Callable[[], CallResult]) -> None:
        self._run = run

    def run(self) -> CallResult:
        return self._run()


def _session_factory(
    *,
    task: CallTask,
    broker: ConsultationBroker,
    stop_event: threading.Event,
    on_event: Callable[[SessionEvent], None],
) -> _Session:
    """Emit one deterministic exchange and finish normally."""

    def run() -> CallResult:
        on_event(StateChanged(CallState.DIALING))
        on_event(StateChanged(CallState.IN_CALL))
        on_event(Utterance("The 3 pm slot is free."))
        on_event(AgentReply("Thank you. Goodbye."))
        result = _result()
        on_event(CallFinished(result))
        return result

    return _Session(run)


def test_executor_completes_a_real_service_call_with_an_artifact() -> None:
    async def scenario() -> None:
        service = CallService(session_factory=_session_factory, default_number=_NUMBER)
        await service.start()
        try:
            queue = _RecordingQueue()
            executor = CallAgentExecutor(service)
            await asyncio.wait_for(
                executor.execute(_Context("Ask about the 3 pm slot."), queue),
                timeout=_MAX_WAIT,
            )

            statuses = _statuses(queue)
            assert statuses[-1].status.state == TaskState.TASK_STATE_COMPLETED

            artifacts = _artifacts(queue)
            assert len(artifacts) == 1
            assert artifacts[0].artifact.name == "call-summary"
            document = artifacts[0].artifact.parts[0].text
            payload = json.loads(document)
            assert payload["state"] == "completed"
            assert payload["objective"] == "Ask about the 3 pm slot."
            assert payload["result"] == {
                "state": "idle",
                "reason": "completed",
                "duration_s": 0.25,
                "turns": 1,
            }
            assert payload["transcript"] == [
                {"role": "callee", "text": "The 3 pm slot is free."},
                {"role": "agent", "text": "Thank you. Goodbye."},
            ]
            assert _NUMBER not in document

            tasks = await service.list_tasks()
            assert len(tasks) == 1
            assert tasks[0].state is CallTaskState.COMPLETED
            assert payload["task_id"] == tasks[0].id
        finally:
            await service.stop()

    asyncio.run(scenario())
