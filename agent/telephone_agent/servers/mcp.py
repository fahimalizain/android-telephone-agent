"""MCP adapter over :class:`~telephone_agent.service.CallService`.

The adapter exposes five tools over the MCP Streamable HTTP transport. It owns
no call state: every operation delegates to the shared service, so an A2A
adapter can be added beside it without duplicating queueing or cancellation.
"""

from __future__ import annotations

import math

from mcp.server.mcpserver import MCPServer
from starlette.applications import Starlette
from starlette.routing import Route

from .. import __version__
from ..service import CallService, CallTask
from .common import BearerAuthMiddleware, health_endpoint, task_summary

__all__ = ["McpTools", "create_mcp_app"]

_INSTRUCTIONS = (
    "Control plane for an outbound telephone agent. place_call queues a call "
    "and returns a task id immediately; it does not wait for the call. Poll "
    "get_call until the task is completed, failed, or canceled. Calls run one "
    "at a time, so a queued task can stay queued behind an active call. When a "
    "task is input_required, use answer_call with the pending question's "
    "answer. Use cancel_call to stop a queued or active call."
)

_PLACE_CALL_DESCRIPTION = (
    "Queue an outbound call and return immediately with a task id. The call "
    "runs asynchronously; poll get_call for progress, the pending question, and "
    "the final result. max_minutes optionally bounds the call duration."
)
_GET_CALL_DESCRIPTION = (
    "Return the current state of one call task, including the masked "
    "destination, pending question, result, and transcript. Poll this after "
    "place_call."
)
_ANSWER_CALL_DESCRIPTION = (
    "Answer the pending question of a call in the input_required state. "
    "Returns accepted=false when the task is not waiting for an answer."
)
_CANCEL_CALL_DESCRIPTION = (
    "Request cancellation of a queued or active call. Returns canceled=false "
    "when the task has already reached a terminal state."
)
_LIST_CALLS_DESCRIPTION = (
    "List call tasks as short summaries, newest first. Summaries omit the destination number."
)


class McpTools:
    """Translate MCP tool calls into :class:`CallService` operations."""

    def __init__(self, service: CallService) -> None:
        self._service = service

    async def place_call(
        self,
        objective: str,
        number: str | None = None,
        max_minutes: float | None = None,
    ) -> dict[str, object]:
        """Enqueue a call and return its task id without waiting for the call."""

        max_call_seconds: float | None = None
        if max_minutes is not None:
            if not math.isfinite(max_minutes) or max_minutes <= 0:
                raise ValueError(
                    f"max_minutes must be a finite number greater than 0 (got {max_minutes!r})"
                )
            max_call_seconds = max_minutes * 60.0

        task = await self._service.submit(
            objective=objective,
            number=number,
            max_call_seconds=max_call_seconds,
        )
        return {"task_id": task.id, "state": task.state.value}

    async def get_call(self, task_id: str) -> dict[str, object]:
        """Return a full snapshot of one task, with the destination masked."""

        task = await self._require_task(task_id)
        return task.to_dict()

    async def answer_call(self, task_id: str, answer: str) -> dict[str, object]:
        """Resolve a pending consultation; ``accepted`` reports whether it won."""

        task = await self._require_task(task_id)
        accepted = await self._service.answer(task_id, answer)
        return {
            "task_id": task.id,
            "accepted": accepted,
            "state": task.state.value,
        }

    async def cancel_call(self, task_id: str) -> dict[str, object]:
        """Request cancellation of a queued or active task."""

        task = await self._require_task(task_id)
        canceled = await self._service.cancel(task_id)
        return {
            "task_id": task.id,
            "canceled": canceled,
            "state": task.state.value,
        }

    async def list_calls(self) -> list[dict[str, object]]:
        """Return compact summaries for every task, newest first."""

        tasks = await self._service.list_tasks()
        tasks.sort(key=lambda task: (task.created_at, task.id), reverse=True)
        return [task_summary(task) for task in tasks]

    async def _require_task(self, task_id: str) -> CallTask:
        task = await self._service.get(task_id)
        if task is None:
            raise ValueError("unknown task")
        return task


def create_mcp_app(service: CallService, *, host: str, token: str) -> Starlette:
    """Build the authenticated MCP Streamable HTTP application.

    Args:
        service: The shared call service. The caller owns its lifecycle.
        host: Host passed to the Streamable HTTP transport for DNS-rebinding
            protection. It does not bind a socket; a future ``serve`` command
            passes the value to uvicorn.
        token: Bearer token required on every route except ``/health``.

    Raises:
        ValueError: If *token* is empty or whitespace.

    The ``/health`` route is appended to the application returned by
    :meth:`MCPServer.streamable_http_app` instead of being mounted beside it.
    That keeps the MCP session manager's lifespan on the outer application, so
    starting the app still starts the transport's session manager.
    """

    if not token.strip():
        raise ValueError("bearer token must not be empty")

    server = MCPServer(
        name="telephone-agent",
        version=__version__,
        instructions=_INSTRUCTIONS,
    )
    tools = McpTools(service)
    server.add_tool(
        tools.place_call,
        name="place_call",
        description=_PLACE_CALL_DESCRIPTION,
    )
    server.add_tool(
        tools.get_call,
        name="get_call",
        description=_GET_CALL_DESCRIPTION,
    )
    server.add_tool(
        tools.answer_call,
        name="answer_call",
        description=_ANSWER_CALL_DESCRIPTION,
    )
    server.add_tool(
        tools.cancel_call,
        name="cancel_call",
        description=_CANCEL_CALL_DESCRIPTION,
    )
    server.add_tool(
        tools.list_calls,
        name="list_calls",
        description=_LIST_CALLS_DESCRIPTION,
    )

    app = server.streamable_http_app(host=host)
    app.router.routes.append(Route("/health", health_endpoint, methods=["GET"], name="health"))
    app.add_middleware(BearerAuthMiddleware, token=token)
    return app
