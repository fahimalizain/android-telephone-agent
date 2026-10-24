"""Append-only local transcripts for one telephone call.

Each call gets its own JSONL file under the configured directory. A line is
written for start, each callee utterance, each agent reply, and the final
result. Audio is never written. Phone numbers are always masked with
:func:`~telephone_agent.dialer.mask_number` before they reach a file.

The writer is intentionally best-effort: a full disk, a permissions problem, or
a closed file must not abort a live call. On the first I/O error the logger logs
a warning and disables further writes for that call.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .dialer import mask_number
from .pipeline import AgentReply, CallFinished, SessionEvent, Utterance

__all__ = ["TranscriptLogger"]

logger = logging.getLogger(__name__)

_SHORT_ID_CHARS = 8
_UNSAFE_ID_CHARS = re.compile(r"[^A-Za-z0-9]+")


def _local_now() -> datetime:
    """Return the current wall-clock time in the host's local time zone."""

    return datetime.now().astimezone()


def _stamp(now: datetime) -> str:
    """Format *now* for a file name, assuming local time for naive values."""

    if now.tzinfo is None:
        now = now.astimezone()
    return now.strftime("%Y%m%dT%H%M%S")


def _short_id(call_id: str) -> str:
    """Return a filesystem-safe short form of *call_id*."""

    compact = _UNSAFE_ID_CHARS.sub("", call_id)
    if compact:
        return compact[:_SHORT_ID_CHARS]
    return uuid.uuid4().hex[:_SHORT_ID_CHARS]


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte of *data* to *fd*."""

    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written == 0:  # pragma: no cover - regular files do not do this
            raise OSError("short write to transcript file")
        view = view[written:]


def _timestamp(now: datetime) -> str:
    """Return an ISO 8601 timestamp with milliseconds and an offset."""

    if now.tzinfo is None:
        now = now.astimezone()
    return now.isoformat(timespec="milliseconds")


class TranscriptLogger:
    """Write one call's events to an append-only JSONL file.

    Args:
        directory: Directory for transcript files. Created with mode 0700 if it
            does not exist. Relative paths are resolved against the process
            working directory.
        call_id: Identifier copied onto every line. Defaults to a random UUID
            hex string.
        clock: Wall-clock source. Injecting an aware ``datetime`` keeps tests
            deterministic; a naive value is interpreted as local time.
        enabled: When false, the logger is a no-op and creates nothing.

    Files are named ``YYYYmmddTHHMMSS-<short-id>.jsonl`` and are opened with
    mode 0600. Call :meth:`close` when the call ends; it is idempotent. Writes
    from the session callback and from ``close`` are serialised by a lock.
    """

    def __init__(
        self,
        directory: str | os.PathLike[str],
        *,
        call_id: str | None = None,
        clock: Callable[[], datetime] = _local_now,
        enabled: bool = True,
    ) -> None:
        self.call_id = call_id or uuid.uuid4().hex
        self.path: Path | None = None
        self._directory = Path(directory)
        self._clock = clock
        self._lock = threading.Lock()
        self._fd: int | None = None
        self._closed = False
        self._disabled = not enabled

        if not self._disabled:
            self._open()

    def _open(self) -> None:
        """Create the directory and open this call's file, or disable writes."""

        try:
            self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self._directory, 0o700)
            stamp = _stamp(self._clock())
            path = self._directory / f"{stamp}-{_short_id(self.call_id)}.jsonl"
            flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY
            fd = os.open(path, flags | getattr(os, "O_CLOEXEC", 0), 0o600)
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                os.close(fd)
                raise
        except (OSError, ValueError):
            self._disabled = True
            self._fd = None
            logger.warning(
                "failed to open the call transcript; transcript logging is disabled for this call",
                exc_info=True,
            )
            return

        self._fd = fd
        self.path = path

    def call_start(self, *, direction: str, number: str) -> None:
        """Record the start of a call with a masked destination."""

        self.log_event("call_start", direction=direction, number=mask_number(number))

    def transcript(self, text: str, *, role: str = "callee") -> None:
        """Record one callee utterance."""

        self.log_event("transcript", role=role, text=text)

    def reply(self, text: str, *, role: str = "agent") -> None:
        """Record one agent reply."""

        self.log_event("reply", role=role, text=text)

    def call_end(
        self,
        *,
        state: str,
        reason: str,
        turns: int,
        duration_s: float,
    ) -> None:
        """Record the final call result."""

        self.log_event(
            "call_end",
            state=state,
            reason=reason,
            turns=turns,
            duration_s=duration_s,
        )

    def log_event(self, event: str, /, **fields: Any) -> None:
        """Append one extensible event.

        ``ts``, ``call_id``, and ``event`` are reserved and cannot be overridden
        by *fields*.
        """

        record: dict[str, Any] = {
            "ts": _timestamp(self._clock()),
            "call_id": self.call_id,
            "event": event,
        }
        for key, value in fields.items():
            if key not in record:
                record[key] = value
        self._write(record)

    def record(self, event: SessionEvent) -> None:
        """Record a pipeline or realtime session event.

        ``CallFinished`` also writes ``call_end`` and closes the file, so a
        session callback alone cannot leave the transcript open.
        """

        if isinstance(event, Utterance):
            self.transcript(event.text)
        elif isinstance(event, AgentReply):
            self.reply(event.text)
        elif isinstance(event, CallFinished):
            result = event.result
            self.call_end(
                state=result.state.value,
                reason=result.reason.value,
                turns=result.turns,
                duration_s=result.duration_s,
            )
            self.close()

    def _write(self, record: dict[str, Any]) -> None:
        if self._disabled:
            return
        try:
            line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
            data = line.encode("utf-8")
        except (TypeError, ValueError):
            logger.warning("failed to serialise a transcript event; skipping it", exc_info=True)
            return

        with self._lock:
            fd = self._fd
            if fd is None or self._disabled:
                return
            try:
                _write_all(fd, data)
            except OSError:
                self._disabled = True
                logger.warning(
                    "failed to append a call transcript event; transcript logging is "
                    "disabled for this call",
                    exc_info=True,
                )

    def close(self) -> None:
        """Close the file. Safe to call more than once and after a failure."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._disabled = True
            fd = self._fd
            self._fd = None

        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                logger.warning("failed to close the call transcript", exc_info=True)

    def __enter__(self) -> TranscriptLogger:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
