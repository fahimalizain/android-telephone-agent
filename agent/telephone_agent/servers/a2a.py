"""A2A adapter over :class:`~telephone_agent.service.CallService`.

The adapter publishes an Agent Card and a JSON-RPC endpoint, and maps one A2A
task to one service-owned call task. It owns no call state: queueing,
consultations, cancellation, and the final transcript all come from the shared
service, so the MCP listener beside it sees the same calls.

The A2A SDK calls :meth:`CallAgentExecutor.execute` again with the same A2A task
id when a message answers a task in ``input-required``. The executor uses that
to resume streaming from a per-task event watermark, which prevents replayed
consultation events from asking the same question twice.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping

from a2a.helpers import new_task, new_text_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Message,
    TaskState,
)
from a2a.utils.constants import PROTOCOL_VERSION_CURRENT, TransportProtocol
from google.protobuf import json_format
from starlette.applications import Starlette
from starlette.routing import Route

from .. import __version__
from ..service import CallService, CallTask
from .common import BearerAuthMiddleware, health_endpoint

__all__ = ["CallAgentExecutor", "build_agent_card", "create_a2a_app"]

_TERMINAL_CALL_STATES = frozenset({"completed", "failed", "canceled"})
_MAX_ERROR_CHARS = 300

_AGENT_DESCRIPTION = (
    "Places real outbound PSTN calls, asks the requester for a missing fact "
    "while the call is live, and reports back a structured summary and transcript."
)
_SKILL_DESCRIPTION = (
    "Accept an objective in natural language, place one outbound call, pause "
    "mid-call to ask the requester for a missing fact when needed, and return a "
    "structured summary and transcript."
)


def build_agent_card(*, url: str) -> AgentCard:
    """Build the public Agent Card for the JSON-RPC listener.

    Args:
        url: Absolute URL of the JSON-RPC endpoint, normally ``<base>/a2a``.
            The card is served separately at
            ``/.well-known/agent-card.json``.

    The card contains no destination number and no credential; it describes
    only the skill and the transport.
    """

    return AgentCard(
        name="telephone-agent",
        description=_AGENT_DESCRIPTION,
        version=__version__,
        supported_interfaces=[
            AgentInterface(
                url=url,
                protocol_binding=TransportProtocol.JSONRPC.value,
                protocol_version=PROTOCOL_VERSION_CURRENT,
            )
        ],
        capabilities=AgentCapabilities(
            streaming=True,
            push_notifications=False,
        ),
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/plain", "application/json"],
        skills=[
            AgentSkill(
                id="place_phone_call",
                name="Place a phone call",
                description=_SKILL_DESCRIPTION,
                tags=["telephony", "voice", "outbound-call"],
                examples=[
                    "Call the clinic and ask whether the 3 pm slot is free.",
                    "Call the courier and reschedule tomorrow's pickup.",
                ],
                input_modes=["text/plain", "application/json"],
                output_modes=["text/plain", "application/json"],
            )
        ],
    )


class CallAgentExecutor(AgentExecutor):
    """Translate A2A messages into calls on the shared service.

    New A2A tasks submit one call. A later message on the same task answers a
    pending consultation and resumes observation. Terminal call states become
    A2A status updates; a completed call also publishes a ``call-summary``
    artifact that holds a JSON document with the result and transcript.

    v1 limitation: if a consultation times out while the A2A task is
    ``input-required``, the A2A task stays in that state until the requester
    sends another message. That later message resumes observation and returns
    the call outcome; there is no server-initiated timeout update.
    """

    def __init__(self, service: CallService) -> None:
        self._service = service
        self._calls: dict[str, str] = {}
        self._watermarks: dict[str, float] = {}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Submit a call or resume observation of an existing one."""

        a2a_task_id, context_id = _task_identifiers(context)
        updater = TaskUpdater(event_queue, a2a_task_id, context_id)
        call_id = self._calls.get(a2a_task_id)

        request = _request_data(context)

        if call_id is None:
            if context.current_task is not None:
                await updater.failed(_agent_message("This call is no longer active.", updater))
                return

            objective = _string_field(request, "objective") or context.get_user_input().strip()
            if not objective:
                await updater.reject(_agent_message("An objective is required.", updater))
                return

            number = _string_field(request, "number") or _string_field(request, "to")
            task = await self._service.submit(objective=objective, number=number)
            call_id = task.id
            self._calls[a2a_task_id] = call_id

            # The A2A 1.x event consumer requires a Task object before any
            # TaskStatusUpdateEvent. TaskUpdater only emits status updates, so
            # publish the initial task explicitly.
            message = context.message
            history: list[Message] | None = None
            if message is not None:
                message.task_id = a2a_task_id
                message.context_id = context_id
                history = [message]
            await event_queue.enqueue_event(
                new_task(
                    task_id=a2a_task_id,
                    context_id=context_id,
                    state=TaskState.TASK_STATE_SUBMITTED,
                    history=history,
                )
            )
            await updater.start_work()
            await self._stream(call_id, updater, skip_at=None)
            return

        answer = _string_field(request, "answer") or context.get_user_input().strip()
        if answer:
            # A False result means the consultation timed out or was already
            # resolved. Do not fail the task: keep observing the live call.
            await self._service.answer(call_id, answer)
        await updater.start_work()
        await self._stream(call_id, updater, skip_at=self._watermarks.get(a2a_task_id))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Cancel the mapped call and finish the A2A task as canceled.

        Unknown or unmapped A2A tasks are still marked canceled, which keeps a
        repeated cancellation request idempotent.
        """

        a2a_task_id, context_id = _task_identifiers(context)
        updater = TaskUpdater(event_queue, a2a_task_id, context_id)
        call_id = self._calls.get(a2a_task_id)
        if call_id is not None:
            await self._service.cancel(call_id)
        await updater.cancel(_agent_message("The call was canceled.", updater))

    async def _stream(
        self,
        call_id: str,
        updater: TaskUpdater,
        *,
        skip_at: float | None,
    ) -> None:
        """Follow one call, yielding only protocol-relevant events.

        Transcript and reply events advance the watermark but do not become
        A2A events; the final artifact carries the whole transcript.
        """

        async for event in self._service.subscribe(call_id):
            if skip_at is not None and event.at <= skip_at:
                continue
            self._watermarks[updater.task_id] = event.at

            if event.kind == "consultation" and event.data.get("status") == "pending":
                question = str(event.data["question"])
                await updater.requires_input(
                    new_text_message(
                        question,
                        task_id=updater.task_id,
                        context_id=updater.context_id,
                    )
                )
                return

            if event.kind == "state" and event.data.get("state") in _TERMINAL_CALL_STATES:
                task = await self._service.get(call_id)
                if task is None:
                    await updater.failed(
                        _agent_message("The call is no longer available.", updater)
                    )
                    return
                state = str(event.data.get("state", ""))
                if state == "completed":
                    await self._publish_summary(task, updater)
                elif state == "failed":
                    error = (task.error or "unknown error")[:_MAX_ERROR_CHARS]
                    await updater.failed(_agent_message(f"The call failed: {error}", updater))
                else:
                    await updater.cancel(_agent_message("The call was canceled.", updater))
                return

        await updater.failed(_agent_message("The call ended without a result.", updater))

    async def _publish_summary(self, task: CallTask, updater: TaskUpdater) -> None:
        """Attach the final JSON summary, then complete the A2A task."""

        payload = _summary_payload(task)
        await updater.add_artifact(
            [
                new_text_part(
                    json.dumps(payload, indent=2, sort_keys=True),
                    media_type="application/json",
                )
            ],
            name="call-summary",
        )
        await updater.complete()


def create_a2a_app(
    service: CallService,
    *,
    host: str,
    port: int,
    token: str,
    public_url: str = "",
) -> Starlette:
    """Build the authenticated A2A application.

    Args:
        service: The shared call service. The caller owns its lifecycle.
        host: Host used in the Agent Card when *public_url* is empty. It does
            not bind a socket; a ``serve`` command passes the value to uvicorn.
        port: Port used in the Agent Card when *public_url* is empty.
        token: Bearer token required by the JSON-RPC endpoint.
        public_url: Optional externally reachable base URL, for example behind
            a reverse proxy. Trailing slashes are ignored.

    Raises:
        ValueError: If *token* is empty or whitespace.

    The Agent Card and ``/health`` are deliberately unauthenticated so a
    requester can discover the agent and a supervisor can probe liveness. The
    JSON-RPC endpoint at ``/a2a`` still requires the bearer token.
    """

    if not token.strip():
        raise ValueError("bearer token must not be empty")

    base = (public_url or f"http://{host}:{port}").rstrip("/")
    card = build_agent_card(url=f"{base}/a2a")
    handler = DefaultRequestHandler(
        agent_executor=CallAgentExecutor(service),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    app = Starlette(
        routes=[
            *create_agent_card_routes(card),
            *create_jsonrpc_routes(handler, rpc_url="/a2a"),
            Route("/health", health_endpoint, methods=["GET"]),
        ]
    )
    app.add_middleware(
        BearerAuthMiddleware,
        token=token,
        exempt_paths=("/health", "/.well-known/agent-card.json"),
    )
    return app


def _request_data(context: RequestContext) -> dict[str, object]:
    """Merge the message's structured data parts into one mapping.

    A2A clients may send a data part with ``objective``, ``number`` (or
    ``to``), and, when resuming, ``answer``. Plain text messages remain
    supported; the executor falls back to the text part.
    """

    message = getattr(context, "message", None)
    if message is None:
        return {}

    data: dict[str, object] = {}
    for part in message.parts:
        if part.WhichOneof("content") != "data":
            continue
        value = json_format.MessageToDict(part.data, preserving_proto_field_name=True)
        if isinstance(value, dict):
            data.update(value)
    return data


def _string_field(data: Mapping[str, object], name: str) -> str | None:
    """Return a stripped non-empty string field, or ``None``."""

    value = data.get(name)
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _task_identifiers(context: RequestContext) -> tuple[str, str]:
    """Return stable A2A task and context ids, tolerating an empty task id."""

    a2a_task_id = context.task_id or context.context_id or uuid.uuid4().hex
    context_id = context.context_id or a2a_task_id
    return a2a_task_id, context_id


def _agent_message(text: str, updater: TaskUpdater) -> Message:
    """Build an agent message tied to the updater's task and context."""

    return new_text_message(
        text,
        task_id=updater.task_id,
        context_id=updater.context_id,
    )


def _summary_payload(task: CallTask) -> dict[str, object]:
    """Return the artifact document, omitting the destination entirely."""

    result: dict[str, object] | None = None
    if task.result is not None:
        result = {
            "state": task.result.state.value,
            "reason": task.result.reason.value,
            "duration_s": task.result.duration_s,
            "turns": task.result.turns,
        }

    return {
        "task_id": task.id,
        "state": task.state.value,
        "objective": task.objective,
        "result": result,
        "transcript": list(task.transcript),
    }
