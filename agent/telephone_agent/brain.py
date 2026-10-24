"""Text-in, text-out conversation brains.

The runtime keeps speech recognition and synthesis outside this module. A
:class:`Brain` receives one caller transcript and returns one reply string; that
seam is what makes the echo brain useful for loopback tests and the HTTP brain
easy to replace with a provider-specific adapter.

``HttpBrain`` requests a non-streaming completion because the v0 pipeline speaks
one complete reply at a time. This does not meet the latency budget in the
design brief. Production deployments should implement a streaming brain that
exposes token or sentence chunks as they arrive; the pipeline can then hand
sentence-sized chunks to a streaming synthesizer while it keeps reading the
microphone for barge-in. That adapter belongs beside this module and should
implement :class:`Brain` for the simple path or a new streaming protocol for the
incremental path.
"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

from .config import BrainConfig

__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "Brain",
    "BrainError",
    "EchoBrain",
    "HttpBrain",
    "build_brain",
]

DEFAULT_SYSTEM_PROMPT = (
    "You are a concise voice assistant on a telephone call. Reply in one or two "
    "short sentences, in plain spoken language. Do not use markdown, lists, or "
    "stage directions."
)

_USER_AGENT = "telephone-agent/0.1"
_ECHO_MAX_CHARS = 200
_MAX_ERROR_DETAIL = 500


class BrainError(RuntimeError):
    """Raised when a brain cannot produce a reply.

    Attributes:
        status: HTTP status code for an HTTP brain, otherwise ``None``.
        body: Response body for an HTTP brain, truncated for display.
    """

    def __init__(self, message: str, *, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class Brain(Protocol):
    """A conversation brain.

    ``reply`` must return a plain string. ``reset`` is optional for custom
    implementations; the call session probes for it before each call. Both
    built-in brains implement it.
    """

    def reply(self, transcript: str) -> str:
        """Return a spoken-style reply to one caller transcript."""

    def reset(self) -> None:
        """Discard conversation history, if any."""


@dataclass(slots=True)
class EchoBrain:
    """Return a short echo of the transcript. Intended for loopback tests."""

    prefix: str = "I heard:"

    def reply(self, transcript: str) -> str:
        text = transcript.strip()
        if not text:
            return "I did not hear anything."
        if len(text) > _ECHO_MAX_CHARS:
            text = f"{text[: _ECHO_MAX_CHARS - 3]}..."
        return f"{self.prefix} {text}".strip()

    def reset(self) -> None:
        """No-op; the echo brain has no state."""


@dataclass(slots=True)
class HttpBrain:
    """OpenAI-compatible chat-completions brain.

    ``base_url`` is the API root, for example ``https://api.example.invalid/v1``.
    The client appends ``/chat/completions``. ``api_key`` may be empty for a
    local server that does not require authentication.

    The full conversation history is kept for the duration of a call. It is
    cleared by :meth:`reset`.
    """

    base_url: str
    model: str
    api_key: str = field(default="", repr=False)
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    max_tokens: int = 200
    timeout_s: float = 30.0
    _history: list[dict[str, str]] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(
                "brain base_url must be an absolute http:// or https:// URL "
                f"(got {self.base_url!r})"
            )
        if not self.model.strip():
            raise ValueError("brain model must not be empty")
        if self.max_tokens <= 0:
            raise ValueError(f"brain max_tokens must be > 0 (got {self.max_tokens})")
        if not math.isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise ValueError(f"brain timeout_s must be positive (got {self.timeout_s})")

    def reply(self, transcript: str) -> str:
        text = transcript.strip()
        if not text:
            raise BrainError("cannot reply to an empty transcript")

        prompt = self.system_prompt.strip() or DEFAULT_SYSTEM_PROMPT
        messages: list[dict[str, str]] = [
            {"role": "system", "content": prompt},
            *self._history,
            {"role": "user", "content": text},
        ]
        payload = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        body, status = self._post(payload)
        content = _assistant_content(body, status)
        self._history.append({"role": "user", "content": text})
        self._history.append({"role": "assistant", "content": content})
        return content

    def reset(self) -> None:
        """Clear conversation history, keeping configuration intact."""

        self._history.clear()

    def _post(self, payload: dict[str, object]) -> tuple[str, int]:
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": _USER_AGENT,
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                status = int(getattr(response, "status", 200))
                body = _read_body(response)
        except urllib.error.HTTPError as exc:
            status = exc.code
            body = _read_body(exc)
            exc.close()
            raise BrainError(
                f"brain endpoint returned HTTP {status}{_detail(body)}",
                status=status,
                body=_truncate(body),
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise BrainError(f"cannot reach brain endpoint at {url}: {_reason(exc)}") from exc

        if not 200 <= status < 300:
            raise BrainError(
                f"brain endpoint returned HTTP {status}{_detail(body)}",
                status=status,
                body=_truncate(body),
            )
        return body, status


def build_brain(config: BrainConfig) -> Brain:
    """Construct the brain described by *config*.

    Configuration loading validates these fields; the checks here make the
    factory safe to call directly as well.
    """

    if config.kind == "echo":
        return EchoBrain()
    if config.kind == "http":
        return HttpBrain(
            base_url=config.base_url,
            model=config.model,
            api_key=config.api_key,
            system_prompt=config.system_prompt,
            max_tokens=config.max_tokens,
            timeout_s=config.timeout_s,
        )
    raise BrainError(f"unsupported brain kind {config.kind!r}")


def _assistant_content(body: str, status: int) -> str:
    text = body.strip()
    if not text:
        raise BrainError("brain endpoint returned an empty response", status=status, body=body)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BrainError(
            f"brain endpoint returned invalid JSON (HTTP {status})",
            status=status,
            body=_truncate(body),
        ) from exc
    if not isinstance(payload, dict):
        raise BrainError(
            f"brain endpoint returned JSON {type(payload).__name__}, expected an object",
            status=status,
            body=_truncate(body),
        )

    try:
        choices = payload["choices"]
        first = choices[0]
        message = first["message"]
        content = message["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise BrainError(
            "brain response did not match the chat-completions schema",
            status=status,
            body=_truncate(body),
        ) from exc
    if not isinstance(content, str) or not content.strip():
        raise BrainError(
            "brain response contained no assistant content",
            status=status,
            body=_truncate(body),
        )
    return content.strip()


def _read_body(response: object) -> str:
    read = getattr(response, "read", None)
    if not callable(read):
        return ""
    try:
        data = read()
    except OSError:
        return ""
    if not isinstance(data, bytes):
        return str(data)
    return data.decode("utf-8", "replace")


def _truncate(body: str) -> str:
    if len(body) <= _MAX_ERROR_DETAIL:
        return body
    return f"{body[: _MAX_ERROR_DETAIL - 3]}..."


def _detail(body: str) -> str:
    summary = " ".join(body.split())
    if not summary:
        return ""
    return f": {_truncate(summary)}"


def _reason(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.URLError) and exc.reason is not None:
        return str(exc.reason)
    return str(exc) or type(exc).__name__
