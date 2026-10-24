"""HTTP client for the phone-side dial helper.

The helper is expected to expose this small JSON contract on the LAN:

======================  =====================================================
``GET /health``         Liveness check; returns a JSON object.
``GET /state``          Call phase: ``idle``, ``ringing``, ``offhook``, or
                        ``unknown``.
``POST /call``          Body ``{"number": "+E164"}``; places the call.
``POST /answer``        Body ``{}``; answers the currently ringing call.
``POST /hangup``        Body ``{}``; ends the current call when supported.
======================  =====================================================

Every request carries ``Authorization: Bearer <token>``. ``POST /call`` also
carries an ``Idempotency-Key`` header, so a retry after a 5xx or a lost
connection does not place a second call if the helper honours it. 4xx responses
are never retried. 5xx and connection errors are retried with bounded
exponential backoff.

The helper never returns a caller's phone number, so ``/state`` and ``/answer``
contain no caller ID. Phone numbers are masked in logs. Error bodies returned by
the helper are sanitised for the number that was dialled before they are stored
or logged.
"""

from __future__ import annotations

import json
import logging
import math
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

__all__ = ["Dialer", "DialerError", "mask_number"]

logger = logging.getLogger(__name__)

_USER_AGENT = "telephone-agent/0.1"
_MAX_BACKOFF_S = 5.0
_MAX_ERROR_DETAIL = 300
_ALLOWED_STATES = frozenset({"idle", "ringing", "offhook", "unknown"})


class DialerError(RuntimeError):
    """Raised when the dial helper cannot complete a request.

    Attributes:
        status: HTTP status code, or ``None`` for a connection-level failure.
        body: Response body, sanitised and safe to log.
    """

    def __init__(self, message: str, *, status: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class _Readable(Protocol):
    def read(self) -> bytes: ...


@dataclass(frozen=True, slots=True)
class Dialer:
    """Client for the helper app that controls Phone A.

    ``backoff_s`` is the first retry delay; it doubles per attempt. It is not a
    configuration field because callers normally should not tune it.
    """

    url: str
    token: str = field(repr=False)
    timeout_s: float = 10.0
    retries: int = 2
    backoff_s: float = 0.5

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError(
                f"dialer URL must be an absolute http:// or https:// URL (got {self.url!r})"
            )
        if not self.token.strip():
            raise ValueError("dialer token must not be empty")
        if not math.isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise ValueError(f"dialer timeout_s must be > 0 (got {self.timeout_s})")
        if not 0 <= self.retries <= 5:
            raise ValueError(f"dialer retries must be between 0 and 5 (got {self.retries})")
        if not math.isfinite(self.backoff_s) or self.backoff_s < 0:
            raise ValueError(f"dialer backoff_s must be >= 0 (got {self.backoff_s})")

    def health(self) -> dict[str, object]:
        """Return the helper's health payload.

        Raises:
            DialerError: If the helper is unreachable or returns a non-2xx
                response.
        """

        return self._request("GET", "/health")

    def place_call(self, number: str) -> dict[str, object]:
        """Ask the helper to place a call to an E.164 *number*.

        The number is expected to have been validated by the configuration
        layer. It is masked in logs and scrubbed from error bodies.
        """

        if not number:
            raise ValueError("number must not be empty")
        logger.info("placing call to %s", mask_number(number))
        return self._request(
            "POST",
            "/call",
            payload={"number": number},
            sensitive=number,
            idempotency_key=uuid.uuid4().hex,
        )

    def state(self) -> dict[str, object]:
        """Return the helper's current call phase.

        The payload contains a ``state`` key whose value is one of ``idle``,
        ``ringing``, ``offhook``, or ``unknown``. A malformed payload raises
        :class:`DialerError` rather than returning an object the watcher cannot
        interpret. The helper never exposes the caller's number.

        Raises:
            DialerError: If the helper is unreachable, returns a non-2xx
                response, or returns an invalid state payload.
        """

        payload = self._request("GET", "/state")
        value = payload.get("state")
        if not isinstance(value, str) or value not in _ALLOWED_STATES:
            raise DialerError(
                "dial helper returned an invalid call state; expected one of "
                f"{', '.join(sorted(_ALLOWED_STATES))} (got {value!r})",
                status=200,
                body=str(value),
            )
        return payload

    def answer(self) -> dict[str, object]:
        """Ask the helper to answer the currently ringing call.

        A successful response means Telecom accepted the answer request, not
        that the call is already connected. The helper returns HTTP 409 when no
        call is ringing, 403 when answer permission is denied, and 500 when the
        platform answer attempt fails. Those are 4xx/5xx responses and follow
        the normal retry policy; 4xx is never retried.
        """

        return self._request("POST", "/answer", payload={})

    def hangup(self) -> dict[str, object]:
        """Ask the helper to end the current call.

        Some helper implementations cannot end a call without privileged
        access. In that case the remote party must hang up; see the README.
        """

        return self._request("POST", "/hangup", payload={})

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, object] | None = None,
        sensitive: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, object]:
        url = f"{self.url.rstrip('/')}{path}"
        data = (
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
            if payload is not None
            else None
        )
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
            "User-Agent": _USER_AGENT,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key

        attempts = self.retries + 1
        for attempt in range(attempts):
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                    status = int(getattr(response, "status", 200))
                    body = _sanitize(_read_body(response), sensitive)
            except urllib.error.HTTPError as exc:
                status = exc.code
                body = _sanitize(_read_body(exc), sensitive)
                exc.close()
                if 500 <= status < 600 and attempt + 1 < attempts:
                    logger.warning(
                        "dial helper returned HTTP %d; retrying (%d/%d)",
                        status,
                        attempt + 1,
                        self.retries,
                    )
                    self._sleep(attempt)
                    continue
                raise DialerError(
                    f"dial helper returned HTTP {status} for {method} {path}{_detail(body)}",
                    status=status,
                    body=body,
                ) from exc
            except (urllib.error.URLError, OSError) as exc:
                if attempt + 1 < attempts:
                    logger.warning(
                        "cannot reach dial helper (%s); retrying (%d/%d)",
                        _reason(exc),
                        attempt + 1,
                        self.retries,
                    )
                    self._sleep(attempt)
                    continue
                raise DialerError(f"cannot reach dial helper at {url}: {_reason(exc)}") from exc

            if not 200 <= status < 300:
                if 500 <= status < 600 and attempt + 1 < attempts:
                    logger.warning(
                        "dial helper returned HTTP %d; retrying (%d/%d)",
                        status,
                        attempt + 1,
                        self.retries,
                    )
                    self._sleep(attempt)
                    continue
                raise DialerError(
                    f"dial helper returned HTTP {status} for {method} {path}{_detail(body)}",
                    status=status,
                    body=body,
                )
            return _parse_object(body, status)

        raise AssertionError("retry loop exited without a response")  # pragma: no cover

    def _sleep(self, attempt: int) -> None:
        delay = min(self.backoff_s * (2**attempt), _MAX_BACKOFF_S)
        if delay > 0:
            time.sleep(delay)


def mask_number(number: str) -> str:
    """Mask a phone number for logs, keeping only the last four characters."""

    if not number:
        return "<none>"
    digits = number.removeprefix("+")
    if len(digits) <= 4:
        return "*" * len(digits)
    return "*" * (len(digits) - 4) + digits[-4:]


def _read_body(response: _Readable) -> str:
    try:
        return response.read().decode("utf-8", "replace")
    except OSError:
        return ""


def _parse_object(body: str, status: int) -> dict[str, object]:
    text = body.strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DialerError(
            f"dial helper returned invalid JSON (HTTP {status})",
            status=status,
            body=body,
        ) from exc
    if not isinstance(payload, dict):
        raise DialerError(
            f"dial helper returned JSON {type(payload).__name__}, expected an object",
            status=status,
            body=body,
        )
    return {str(key): value for key, value in payload.items()}


def _sanitize(text: str, sensitive: str | None) -> str:
    if not text or not sensitive:
        return text
    digits = sensitive.removeprefix("+")
    sanitized = text.replace(sensitive, mask_number(sensitive))
    if digits:
        sanitized = sanitized.replace(digits, mask_number(sensitive))
    return sanitized


def _detail(body: str) -> str:
    summary = " ".join(body.split())
    if not summary:
        return ""
    if len(summary) > _MAX_ERROR_DETAIL:
        summary = f"{summary[: _MAX_ERROR_DETAIL - 3]}..."
    return f": {summary}"


def _reason(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.URLError) and exc.reason is not None:
        return str(exc.reason)
    return str(exc) or type(exc).__name__
