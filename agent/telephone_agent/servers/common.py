"""HTTP pieces shared by the MCP and A2A listeners.

This module depends on Starlette, which is part of the optional ``server``
extra. It must not import either protocol adapter, so both adapters can reuse
the same authentication and response helpers without a dependency cycle.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping

from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .. import __version__
from ..service import CallTask

__all__ = [
    "BearerAuthMiddleware",
    "health_endpoint",
    "json_response",
    "task_summary",
]


class BearerAuthMiddleware:
    """Require a bearer token on every HTTP route except *exempt_paths*.

    This is a raw ASGI middleware rather than a Starlette
    ``BaseHTTPMiddleware`` so it can sit in front of an application whose
    lifespan or streaming behaviour must not be wrapped in a task group. Only
    HTTP scopes are authenticated; lifespan and websocket scopes pass through.

    Args:
        app: The wrapped ASGI application.
        token: The accepted bearer token. Must not be empty.
        exempt_paths: Exact URL paths that never require authentication. The
            default exempts the health endpoint used by process supervisors.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        token: str,
        exempt_paths: tuple[str, ...] = ("/health",),
    ) -> None:
        if not token:
            raise ValueError("bearer token must not be empty")
        self._app = app
        self._token = token.encode("utf-8")
        self._exempt_paths = frozenset(exempt_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in self._exempt_paths:
            await self._app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        scheme, _, credentials = headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(
            credentials.encode("utf-8"), self._token
        ):
            response = json_response({"error": "unauthorized"}, status_code=401)
            response.headers["WWW-Authenticate"] = "Bearer"
            await response(scope, receive, send)
            return

        await self._app(scope, receive, send)


def task_summary(task: CallTask) -> dict[str, object]:
    """Return the compact task representation used by listing responses.

    The destination number is deliberately absent, not merely masked, so a
    summary can never leak the raw E.164 value through a serializer.
    """

    return {
        "task_id": task.id,
        "state": task.state.value,
        "objective": task.objective,
        "created_at": task.created_at,
        "pending_question": (
            task.pending_consultation.question if task.pending_consultation is not None else None
        ),
    }


def json_response(payload: Mapping[str, object], status_code: int = 200) -> Response:
    """Return a JSON response for a plain mapping payload."""

    return JSONResponse(dict(payload), status_code=status_code)


async def health_endpoint(request: Request) -> Response:
    """Report liveness and the agent version without touching call state."""

    return json_response(
        {
            "status": "ok",
            "service": "telephone-agent",
            "version": __version__,
        }
    )
