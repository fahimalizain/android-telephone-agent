"""Tests for the MCP adapter over :class:`CallService`.

The project does not depend on pytest-asyncio, so every test drives an inner
async scenario with ``asyncio.run``. Sessions are fakes and all requests stay
inside the ASGI transport: no real network, audio, or dial helper is involved.
"""

from __future__ import annotations

import asyncio
import json
import math
import threading
from collections.abc import Callable

import pytest

pytest.importorskip("mcp")

import httpx  # noqa: E402
from telephone_agent import __version__  # noqa: E402
from telephone_agent.consult import ConsultationBroker  # noqa: E402
from telephone_agent.dialer import mask_number  # noqa: E402
from telephone_agent.pipeline import (  # noqa: E402
    CallEndReason,
    CallFinished,
    CallResult,
    CallState,
    ConsultationFinished,
    ConsultationStarted,
    SessionEvent,
    StateChanged,
)
from telephone_agent.servers.mcp import McpTools, create_mcp_app  # noqa: E402
from telephone_agent.service import CallService, TaskState  # noqa: E402

_TOKEN = "test-server-token"
_NUMBER = "+15555550100"
_MAX_WAIT = 2.0

_SUMMARY_KEYS = {"task_id", "state", "objective", "created_at", "pending_question"}


def _result(
    *,
    state: CallState = CallState.IDLE,
    reason: CallEndReason = CallEndReason.COMPLETED,
    turns: int = 1,
) -> CallResult:
    return CallResult(state=state, reason=reason, duration_s=0.25, turns=turns)


class _Session:
    """Minimal SessionLike backed by a callable."""

    def __init__(self, run: Callable[[], CallResult]) -> None:
        self._run = run

    def run(self) -> CallResult:
        return self._run()


async def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout_s: float = _MAX_WAIT,
) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), timeout_s)


def _event_payload(text: str) -> dict[str, object]:
    """Extract the JSON-RPC payload from one server-sent event."""

    for line in text.splitlines():
        if line.startswith("data: "):
            payload = json.loads(line[len("data: ") :])
            assert isinstance(payload, dict)
            return payload
    raise AssertionError("response did not contain an SSE data event")


def test_place_call_returns_immediately_and_get_call_masks_number() -> None:
    async def scenario() -> None:
        release = threading.Event()

        def factory(**_: object) -> _Session:
            def run() -> CallResult:
                release.wait(_MAX_WAIT)
                return _result(turns=2)

            return _Session(run)

        service = CallService(session_factory=factory, default_number=_NUMBER)
        await service.start()
        try:
            tools = McpTools(service)
            placed = await tools.place_call("Confirm the delivery.", max_minutes=1.5)

            assert set(placed) == {"task_id", "state"}
            assert placed["state"] == "queued"
            task_id = placed["task_id"]
            assert isinstance(task_id, str)

            task = await service.get(task_id)
            assert task is not None
            assert task.number == _NUMBER
            assert task.max_call_seconds == 90.0

            snapshot = await tools.get_call(task_id)
            assert snapshot["task_id"] == task_id
            assert snapshot["state"] == "queued"
            assert snapshot["number"] == mask_number(_NUMBER)
            assert _NUMBER not in json.dumps(snapshot)

            release.set()
            finished = await service.wait(task_id, timeout_s=_MAX_WAIT)
            assert finished is task
            assert (await tools.get_call(task_id))["state"] == "completed"

            with pytest.raises(ValueError, match="unknown task"):
                await tools.get_call("missing")
        finally:
            release.set()
            await service.stop()

    asyncio.run(scenario())


def test_place_call_uses_the_service_default_number() -> None:
    async def scenario() -> None:
        service = CallService(
            session_factory=lambda **_: _Session(lambda: _result()),
            default_number=_NUMBER,
        )
        await service.start()
        try:
            placed = await McpTools(service).place_call("Use the default destination.")
            task = await service.get(placed["task_id"])
            assert task is not None
            assert task.number == _NUMBER
        finally:
            await service.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("max_minutes", [0, -1.0, math.inf, math.nan])
def test_place_call_rejects_invalid_max_minutes(max_minutes: float) -> None:
    async def scenario() -> None:
        service = CallService(
            session_factory=lambda **_: _Session(lambda: _result()),
            default_number=_NUMBER,
        )
        await service.start()
        try:
            with pytest.raises(ValueError, match="max_minutes"):
                await McpTools(service).place_call(
                    "Invalid duration.",
                    number=_NUMBER,
                    max_minutes=max_minutes,
                )
        finally:
            await service.stop()

    asyncio.run(scenario())


def test_answer_cancel_and_list_calls() -> None:
    async def scenario() -> None:
        release = threading.Event()

        def factory(
            *,
            broker: ConsultationBroker,
            on_event: Callable[[SessionEvent], None],
            **_: object,
        ) -> _Session:
            def run() -> CallResult:
                on_event(StateChanged(CallState.IN_CALL))
                outcome = broker.ask(
                    "Which Friday?",
                    _MAX_WAIT,
                    on_open=lambda consultation: on_event(ConsultationStarted(consultation)),
                )
                on_event(ConsultationFinished(outcome))
                release.wait(_MAX_WAIT)
                result = _result()
                on_event(CallFinished(result))
                return result

            return _Session(run)

        service = CallService(session_factory=factory, default_number=_NUMBER)
        await service.start()
        try:
            tools = McpTools(service)
            first = await tools.place_call("Move the delivery.")
            first_id = first["task_id"]
            assert isinstance(first_id, str)
            task = await service.get(first_id)
            assert task is not None
            await _wait_until(lambda: task.state is TaskState.INPUT_REQUIRED)

            snapshot = await tools.get_call(first_id)
            assert snapshot["pending_question"] == "Which Friday?"

            answered = await tools.answer_call(first_id, "Friday 30 October")
            assert set(answered) == {"task_id", "accepted", "state"}
            assert answered["task_id"] == first_id
            assert answered["accepted"] is True
            assert answered["state"] in {"input_required", "in_call"}

            second = await tools.place_call("Another call.", number=_NUMBER)
            second_id = second["task_id"]
            assert second["state"] == "queued"

            canceled = await tools.cancel_call(second_id)
            assert canceled == {
                "task_id": second_id,
                "canceled": True,
                "state": "canceled",
            }
            canceled_again = await tools.cancel_call(second_id)
            assert canceled_again["canceled"] is False
            assert canceled_again["state"] == "canceled"

            listed = await tools.list_calls()
            assert [item["task_id"] for item in listed] == [second_id, first_id]
            assert all(set(item) == _SUMMARY_KEYS for item in listed)
            assert _NUMBER not in json.dumps(listed)
            assert listed[0]["state"] == "canceled"

            with pytest.raises(ValueError, match="unknown task"):
                await tools.answer_call("missing", "answer")
            with pytest.raises(ValueError, match="unknown task"):
                await tools.cancel_call("missing")

            release.set()
            assert await service.wait(first_id, timeout_s=_MAX_WAIT) is task
            late = await tools.answer_call(first_id, "too late")
            assert late["accepted"] is False
            assert late["state"] == "completed"
        finally:
            release.set()
            await service.stop()

    asyncio.run(scenario())


def test_create_mcp_app_health_is_public_and_mcp_requires_auth() -> None:
    async def scenario() -> None:
        service = CallService(
            session_factory=lambda **_: _Session(lambda: _result()),
            default_number=_NUMBER,
        )

        with pytest.raises(ValueError, match="token"):
            create_mcp_app(service, host="127.0.0.1", token="")
        with pytest.raises(ValueError, match="token"):
            create_mcp_app(service, host="127.0.0.1", token="   ")

        app = create_mcp_app(service, host="127.0.0.1", token=_TOKEN)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8765",
        ) as client:
            health = await client.get("/health")
            assert health.status_code == 200
            assert health.json() == {
                "status": "ok",
                "service": "telephone-agent",
                "version": __version__,
            }

            missing = await client.post("/mcp", json={})
            assert missing.status_code == 401
            assert missing.json() == {"error": "unauthorized"}
            assert missing.headers["www-authenticate"] == "Bearer"

            wrong = await client.post(
                "/mcp",
                json={},
                headers={"Authorization": "Bearer wrong-token"},
            )
            assert wrong.status_code == 401
            assert wrong.json() == {"error": "unauthorized"}

    asyncio.run(scenario())


def test_mcp_initialize_and_tools_list_over_asgi() -> None:
    async def scenario() -> None:
        service = CallService(
            session_factory=lambda **_: _Session(lambda: _result()),
            default_number=_NUMBER,
        )
        app = create_mcp_app(service, host="127.0.0.1", token=_TOKEN)
        transport = httpx.ASGITransport(app=app)
        async with (
            app.router.lifespan_context(app),
            httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1:8765",
            ) as client,
        ):
            headers = {
                "Authorization": f"Bearer {_TOKEN}",
                "Accept": "application/json, text/event-stream",
            }
            initialized = await client.post(
                "/mcp",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "test", "version": "1.0"},
                    },
                },
                headers=headers,
            )
            assert initialized.status_code == 200
            session_headers = {
                **headers,
                "mcp-session-id": initialized.headers["mcp-session-id"],
                "mcp-protocol-version": "2025-06-18",
            }
            await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=session_headers,
            )

            listed = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                headers=session_headers,
            )
            assert listed.status_code == 200
            payload = _event_payload(listed.text)
            result = payload["result"]
            assert isinstance(result, dict)
            tools = result["tools"]
            assert isinstance(tools, list)
            assert {tool["name"] for tool in tools} == {
                "place_call",
                "get_call",
                "answer_call",
                "cancel_call",
                "list_calls",
            }

    asyncio.run(scenario())


def test_public_url_is_allowed_through_a_reverse_proxy_host_header() -> None:
    async def scenario() -> None:
        service = CallService(
            session_factory=lambda **_: _Session(lambda: _result()),
            default_number=_NUMBER,
        )
        app = create_mcp_app(
            service,
            host="127.0.0.1",
            token=_TOKEN,
            public_url="https://agent.example.com",
        )
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1.0"},
            },
        }
        headers = {
            "Authorization": f"Bearer {_TOKEN}",
            "Accept": "application/json, text/event-stream",
        }

        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://agent.example.com",
            ) as client:
                allowed = await client.post("/mcp", json=body, headers=headers)
                assert allowed.status_code == 200

            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://unexpected.example",
            ) as client:
                rejected = await client.post("/mcp", json=body, headers=headers)
                assert rejected.status_code == 421

    asyncio.run(scenario())
