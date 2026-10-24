"""Tests for the shared HTTP authentication and response helpers."""

from __future__ import annotations

import asyncio
import json

import pytest

pytest.importorskip("mcp")

import httpx  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import Response  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.types import Receive, Scope, Send  # noqa: E402
from telephone_agent import __version__  # noqa: E402
from telephone_agent.consult import Consultation  # noqa: E402
from telephone_agent.servers.common import (  # noqa: E402
    BearerAuthMiddleware,
    health_endpoint,
    json_response,
    task_summary,
)
from telephone_agent.service import CallTask, TaskState  # noqa: E402

_TOKEN = "test-server-token"
_NUMBER = "+15555550100"


def _build_app(token: str = _TOKEN) -> Starlette:
    async def echo(request: Request) -> Response:
        return json_response({"ok": True})

    app = Starlette(
        routes=[
            Route("/health", health_endpoint, methods=["GET"]),
            Route("/echo", echo, methods=["GET"]),
        ]
    )
    app.add_middleware(BearerAuthMiddleware, token=token)
    return app


def _get(app: Starlette, path: str, *, token: str | None = None) -> httpx.Response:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}

    async def scenario() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(path, headers=headers)

    return asyncio.run(scenario())


def test_health_is_served_without_a_token() -> None:
    response = _get(_build_app(), "/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "telephone-agent",
        "version": __version__,
    }


def test_missing_token_is_rejected() -> None:
    response = _get(_build_app(), "/echo")

    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}
    assert response.headers["www-authenticate"] == "Bearer"


def test_wrong_token_is_rejected() -> None:
    response = _get(_build_app(), "/echo", token="wrong-token")

    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}
    assert response.headers["www-authenticate"] == "Bearer"


def test_exact_bearer_token_is_accepted() -> None:
    response = _get(_build_app(), "/echo", token=_TOKEN)

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_empty_token_is_rejected_at_construction() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:  # pragma: no cover
        return None

    with pytest.raises(ValueError, match="token"):
        BearerAuthMiddleware(app, token="")


def test_non_http_scopes_pass_through() -> None:
    seen: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    async def receive() -> dict[str, object]:
        return {"type": "lifespan.startup"}

    async def send(message: dict[str, object]) -> None:
        return None

    middleware = BearerAuthMiddleware(inner, token=_TOKEN)
    asyncio.run(middleware({"type": "lifespan"}, receive, send))
    asyncio.run(middleware({"type": "websocket", "path": "/mcp"}, receive, send))

    assert seen == ["lifespan", "websocket"]


def test_task_summary_omits_the_number_and_reports_pending_question() -> None:
    task = CallTask(
        id="task-1",
        objective="Confirm the delivery.",
        number=_NUMBER,
        state=TaskState.INPUT_REQUIRED,
        created_at=12.5,
    )
    assert task_summary(task)["pending_question"] is None

    task.pending_consultation = Consultation(
        id="question-1",
        question="Which Friday?",
        created_at=13.0,
    )
    summary = task_summary(task)

    assert summary == {
        "task_id": "task-1",
        "state": "input_required",
        "objective": "Confirm the delivery.",
        "created_at": 12.5,
        "pending_question": "Which Friday?",
    }
    assert _NUMBER not in json.dumps(summary)
