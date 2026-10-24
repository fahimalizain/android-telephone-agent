"""One long-lived call engine with a local Unix-socket control plane.

The daemon owns the Bluetooth audio path and the phone call. It runs outbound
calls submitted by ``telephone-agent run`` and inbound calls discovered by
:class:`~telephone_agent.inbound.InboundWatcher` through the same
:class:`CallExecutor`, so two calls can never open the SCO audio nodes at once.

The control protocol is newline-delimited JSON over a Unix-domain socket, one
request per connection::

    {"command": "call", "number": "+15555550100", "objective": "",
     "max_call_seconds": 300}

The response is written only after the call ends::

    {"ok": true, "result": {"state": "idle", "reason": "completed",
                            "turns": 3, "duration_s": 42.5}}

``status`` returns the masked active destination, and ``hangup`` sets the
active call's stop event. Errors are ``{"ok": false, "error": "...",
"message": "..."}`` with ``error`` one of ``busy``, ``invalid``,
``no_active_call``, or ``failed``.

Only local processes in the daemon user's session can connect: the parent
directory is created with mode 0700 and the socket is mode 0600. Bearer tokens
and API keys never cross the socket, and numbers are masked in status responses
and logs.
"""

from __future__ import annotations

import json
import logging
import math
import os
import queue
import socket
import socketserver
import stat
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .config import ensure_e164
from .dialer import mask_number
from .pipeline import CallEndReason, CallResult, CallState, SessionEvent

__all__ = [
    "BusyError",
    "CallExecutor",
    "CallHandle",
    "CallReply",
    "CallRequest",
    "ControlServer",
    "DaemonClient",
    "DaemonError",
    "DaemonUnavailableError",
    "resolve_socket_path",
]

logger = logging.getLogger(__name__)

_MAX_REQUEST_BYTES = 64 * 1024
_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_OBJECTIVE_CHARS = 4000
_MAX_ERROR_CHARS = 500
_STOP_JOIN_S = 5.0
_PROBE_TIMEOUT_S = 0.5


class DaemonError(RuntimeError):
    """Raised when the daemon cannot start or the control protocol fails."""


class DaemonUnavailableError(DaemonError):
    """Raised when no daemon is listening on the control socket."""


class BusyError(RuntimeError):
    """Raised when a call is submitted while another call is active."""


class SessionLike(Protocol):
    """The one method the executor needs from a call session."""

    def run(self) -> object:
        """Run the call to completion and return a result."""


class TranscriptLike(Protocol):
    """The transcript operations used by the executor."""

    def record(self, event: SessionEvent) -> None: ...

    def call_end(
        self,
        *,
        state: str,
        reason: str,
        turns: int,
        duration_s: float,
    ) -> None: ...

    def close(self) -> None: ...


SessionFactory = Callable[["CallRequest", Callable[[SessionEvent], None]], SessionLike]
TranscriptFactory = Callable[["CallRequest", str], TranscriptLike | None]


@dataclass(slots=True)
class CallRequest:
    """One inbound or outbound call submitted to the executor.

    ``direction`` is ``inbound`` or ``outbound``. An outbound request requires
    an E.164 ``number``; inbound requests leave it empty because the helper does
    not expose caller ID. ``stop_event`` is fresh per call and is set by
    :meth:`CallExecutor.hangup` or shutdown.
    """

    direction: str
    number: str = ""
    objective: str = ""
    max_call_seconds: float | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)

    def __post_init__(self) -> None:
        if self.direction not in {"inbound", "outbound"}:
            raise ValueError(
                f"call direction must be 'inbound' or 'outbound' (got {self.direction!r})"
            )
        if self.direction == "outbound":
            if not isinstance(self.number, str) or not self.number:
                raise ValueError("an outbound call requires a destination number")
            self.number = ensure_e164(self.number)
        elif self.number:
            raise ValueError("an inbound call must not include a destination number")
        if not isinstance(self.objective, str):
            raise ValueError("call objective must be a string")
        if len(self.objective) > _MAX_OBJECTIVE_CHARS:
            raise ValueError(
                f"call objective must be at most {_MAX_OBJECTIVE_CHARS} characters "
                f"(got {len(self.objective)})"
            )
        if self.max_call_seconds is not None and (
            isinstance(self.max_call_seconds, bool)
            or not isinstance(self.max_call_seconds, (int, float))
            or not math.isfinite(self.max_call_seconds)
            or self.max_call_seconds <= 0
        ):
            raise ValueError(
                "max_call_seconds must be a finite number greater than 0 "
                f"(got {self.max_call_seconds!r})"
            )


@dataclass(slots=True)
class CallHandle:
    """Completion handle for one submitted call."""

    request: CallRequest
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, object] | None = None
    error: BaseException | None = None
    started_at: float | None = None

    def wait(self, timeout: float | None = None) -> dict[str, object]:
        """Block for the result, re-raising a session failure.

        Raises:
            TimeoutError: If *timeout* expires before the call ends.
        """

        if not self.done.wait(timeout):
            raise TimeoutError("the call did not finish before the timeout")
        if self.error is not None:
            raise self.error
        if self.result is None:  # pragma: no cover - only a worker bug can do this
            raise DaemonError("the call finished without a result")
        return self.result


class CallExecutor:
    """Run at most one call at a time on a single worker thread.

    A second submission while a call is active raises :class:`BusyError`; calls
    are never queued indefinitely. Each request gets a fresh session from
    *session_factory*, which is called as ``factory(request, on_event)`` from the
    worker thread. *transcript_factory* is optional and is called as
    ``factory(request, call_id)``; the executor records session events and closes
    it when the call ends.

    :meth:`submit` blocks until the result; :meth:`enqueue` is the non-blocking
    half used by the inbound watcher. :meth:`hangup` sets the active request's
    stop event and is safe to call from any thread.
    """

    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        transcript_factory: TranscriptFactory | None = None,
        clock: Callable[[], float] = time.time,
        id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._session_factory = session_factory
        self._transcript_factory = transcript_factory
        self._clock = clock
        self._id_factory = id_factory
        self._lock = threading.RLock()
        self._queue: queue.Queue[tuple[CallRequest, CallHandle] | None] = queue.Queue(maxsize=1)
        self._pending: tuple[CallRequest, CallHandle] | None = None
        self._active: tuple[CallRequest, CallHandle] | None = None
        self._stopping = False
        self._worker: threading.Thread | None = None

    @property
    def busy(self) -> bool:
        """True while a call is active or accepted but not yet started."""

        with self._lock:
            return self._active is not None or self._pending is not None

    def start(self) -> None:
        """Start the worker thread. Idempotent while it is alive."""

        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stopping = False
            self._queue = queue.Queue(maxsize=1)
            self._pending = None
            self._active = None
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="telephone-agent-call-executor",
                daemon=True,
            )
            self._worker.start()

    def enqueue(self, request: CallRequest) -> CallHandle:
        """Accept *request* without waiting for the call to finish.

        Raises:
            BusyError: If another call is active, queued, or the executor is
                shutting down.
        """

        if not isinstance(request, CallRequest):
            raise TypeError("submit expects a CallRequest")
        handle = CallHandle(request)
        with self._lock:
            if self._stopping:
                raise BusyError("the daemon is shutting down")
            if self._worker is None or not self._worker.is_alive():
                raise BusyError("the call executor is not running")
            if self._active is not None or self._pending is not None:
                raise BusyError("a call is already active")
            item = (request, handle)
            self._pending = item
            try:
                self._queue.put_nowait(item)
            except queue.Full as exc:  # pragma: no cover - guarded by _pending
                self._pending = None
                raise BusyError("a call is already active") from exc
        return handle

    def submit(self, request: CallRequest) -> dict[str, object]:
        """Run *request* and block until it ends or fails.

        Raises:
            BusyError: If another call is active.
            Exception: Whatever the session raised.
        """

        return self.enqueue(request).wait()

    def hangup(self) -> bool:
        """Set the active call's stop event.

        Returns ``False`` when no call is active. The call ends cooperatively,
        as soon as its audio loop next checks the event.
        """

        with self._lock:
            item = self._active if self._active is not None else self._pending
        if item is None:
            return False
        item[0].stop_event.set()
        return True

    def status(self) -> dict[str, object]:
        """Return an idle or in-call snapshot, with the number masked."""

        with self._lock:
            item = self._active if self._active is not None else self._pending
            started_at = item[1].started_at if item is not None else None
        if item is None:
            return {
                "state": "idle",
                "direction": None,
                "number": "",
                "since": None,
            }
        request = item[0]
        return {
            "state": "in_call",
            "direction": request.direction,
            "number": mask_number(request.number),
            "since": started_at,
        }

    def stop(self, *, timeout: float = _STOP_JOIN_S) -> None:
        """Stop accepting calls, cancel the active call, and join the worker."""

        with self._lock:
            self._stopping = True
            active = self._active
            pending = self._pending
        if active is not None:
            active[0].stop_event.set()
        if pending is not None:
            pending[0].stop_event.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            # A pending request is already in the queue. The worker fails it
            # after seeing _stopping and then returns without the sentinel.
            logger.debug("call executor stop sentinel not needed for a queued request")
        worker = self._worker
        if worker is not None:
            worker.join(timeout)
            if worker.is_alive():
                logger.warning("the active call did not stop within %.1f s", timeout)
        with self._lock:
            self._worker = None

    def _worker_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            request, handle = item
            with self._lock:
                if self._pending is item:
                    self._pending = None
                self._active = item
                handle.started_at = self._clock()
                stopping = self._stopping
            if stopping:
                handle.error = BusyError("the daemon is shutting down")
                with self._lock:
                    self._active = None
                handle.done.set()
                return
            try:
                self._run_call(request, handle)
            finally:
                with self._lock:
                    self._active = None

    def _run_call(self, request: CallRequest, handle: CallHandle) -> None:
        call_id = self._id_factory()
        started = time.monotonic()
        outcome: dict[str, object] | None = None
        transcript: TranscriptLike | None = None

        logger.info(
            "call started: direction=%s number=%s",
            request.direction,
            mask_number(request.number),
        )
        try:
            if self._transcript_factory is not None:
                try:
                    transcript = self._transcript_factory(request, call_id)
                except Exception:
                    logger.warning("cannot open the call transcript", exc_info=True)

            def on_event(event: SessionEvent) -> None:
                if transcript is None:
                    return
                try:
                    transcript.record(event)
                except Exception:
                    logger.warning("cannot record a transcript event", exc_info=True)

            session = self._session_factory(request, on_event)
            result = session.run()
            outcome = _result_to_dict(result, time.monotonic() - started)
        except Exception as exc:
            outcome = {
                "state": CallState.FAILED.value,
                "reason": CallEndReason.TURN_FAILED.value,
                "turns": 0,
                "duration_s": round(max(time.monotonic() - started, 0.0), 3),
            }
            handle.error = exc
            logger.warning(
                "call failed: direction=%s number=%s error=%s",
                request.direction,
                mask_number(request.number),
                _short_error(exc),
            )
        finally:
            if outcome is None:  # pragma: no cover - defensive
                outcome = {
                    "state": CallState.FAILED.value,
                    "reason": CallEndReason.TURN_FAILED.value,
                    "turns": 0,
                    "duration_s": 0.0,
                }
            if transcript is not None:
                try:
                    transcript.call_end(
                        state=str(outcome["state"]),
                        reason=str(outcome["reason"]),
                        turns=int(outcome["turns"]),  # type: ignore[arg-type]
                        duration_s=float(outcome["duration_s"]),  # type: ignore[arg-type]
                    )
                except Exception:
                    logger.warning("cannot finalise the call transcript", exc_info=True)
                transcript.close()
            handle.result = outcome
            handle.done.set()
            logger.info(
                "call ended: direction=%s state=%s reason=%s turns=%s duration=%.1fs",
                request.direction,
                outcome["state"],
                outcome["reason"],
                outcome["turns"],
                float(outcome["duration_s"]),  # type: ignore[arg-type]
            )


def _result_to_dict(result: object, measured_duration_s: float) -> dict[str, object]:
    if isinstance(result, CallResult):
        return {
            "state": result.state.value,
            "reason": result.reason.value,
            "turns": int(result.turns),
            "duration_s": float(result.duration_s),
        }
    if isinstance(result, Mapping):
        return {
            "state": _enum_value(result.get("state", CallState.IDLE.value)),
            "reason": _enum_value(result.get("reason", CallEndReason.COMPLETED.value)),
            "turns": int(result.get("turns", 0)),  # type: ignore[arg-type]
            "duration_s": float(result.get("duration_s", measured_duration_s)),  # type: ignore[arg-type]
        }
    return {
        "state": CallState.IDLE.value,
        "reason": CallEndReason.COMPLETED.value,
        "turns": 0,
        "duration_s": round(max(measured_duration_s, 0.0), 3),
    }


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _short_error(exc: BaseException) -> str:
    message = str(exc).strip() or type(exc).__name__
    return message[:_MAX_ERROR_CHARS]


def resolve_socket_path(value: str | os.PathLike[str] | None = "") -> Path:
    """Resolve the configured control socket path.

    An empty value selects ``$XDG_RUNTIME_DIR/telephone-agent.sock``, or
    ``<tempdir>/telephone-agent-<uid>.sock`` when the runtime directory is
    unset. ``~`` is expanded. The path is not required to exist yet.
    """

    text = "" if value is None else os.fspath(value).strip()
    if text:
        path = Path(text).expanduser()
    else:
        runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
        if runtime_dir:
            path = Path(runtime_dir).expanduser() / "telephone-agent.sock"
        else:
            path = Path(tempfile.gettempdir()) / f"telephone-agent-{os.getuid()}.sock"
    if not path.name:
        raise DaemonError("the daemon control socket path is empty")
    encoded = os.fsencode(path)
    if len(encoded) > 103:
        raise DaemonError(
            f"daemon control socket path is too long ({len(encoded)} bytes); "
            "Unix-domain socket paths are limited to about 108 bytes on Linux"
        )
    return path


def _prepare_socket_parent(path: Path) -> None:
    parent = path.parent
    try:
        if not parent.exists():
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(parent, 0o700)
            except OSError:
                logger.debug("cannot chmod %s to 0700", parent, exc_info=True)
        elif not parent.is_dir():
            raise DaemonError(f"daemon socket parent {parent} is not a directory")
    except OSError as exc:
        raise DaemonError(f"cannot create the daemon socket directory {parent}: {exc}") from exc


def _remove_stale_socket(path: Path) -> None:
    """Refuse to replace a live daemon; remove a socket nobody is listening on."""

    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return
    except OSError as exc:
        raise DaemonError(f"cannot inspect the daemon socket {path}: {exc}") from exc
    if not stat.S_ISSOCK(mode):
        raise DaemonError(
            f"{path} exists and is not a Unix-domain socket; remove it or choose another "
            "daemon.socket path"
        )

    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(_PROBE_TIMEOUT_S)
    try:
        probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError):
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise DaemonError(f"cannot remove the stale daemon socket {path}: {exc}") from exc
    except OSError as exc:
        raise DaemonError(
            f"cannot probe the existing daemon socket {path}: {exc}; another daemon may be active"
        ) from exc
    else:
        raise DaemonError(
            f"another telephone-agent daemon is already listening on {path}; stop it first"
        )
    finally:
        probe.close()


class _ControlHandler(socketserver.StreamRequestHandler):
    """Read one newline-delimited JSON request and write one JSON response."""

    def handle(self) -> None:
        try:
            line = self.rfile.readline(_MAX_REQUEST_BYTES + 1)
        except OSError:
            return
        if not line:
            return
        if len(line) > _MAX_REQUEST_BYTES:
            self._write({"ok": False, "error": "invalid", "message": "request is too large"})
            return
        try:
            payload = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write({"ok": False, "error": "invalid", "message": "request is not valid JSON"})
            return

        server = self.server
        assert isinstance(server, _ThreadingUnixServer)
        response = server.control.dispatch(payload)
        self._write(response)

    def _write(self, payload: Mapping[str, object]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except OSError:
            logger.debug("control client disconnected before the response was written")


class _ThreadingUnixServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    block_on_close = False

    def __init__(
        self,
        socket_path: str,
        handler: type[socketserver.BaseRequestHandler],
        *,
        control: ControlServer,
    ) -> None:
        self.control = control
        super().__init__(socket_path, handler)

    def server_bind(self) -> None:
        super().server_bind()
        try:
            os.chmod(self.server_address, 0o600)
        except OSError:
            self.server_close()
            raise

    def handle_error(self, request: object, client_address: object) -> None:
        logger.warning("control connection failed", exc_info=True)


class ControlServer:
    """Serve the JSON control protocol on a Unix-domain socket.

    The listening socket and handler threads start immediately. :meth:`stop`
    shuts down the accept loop, closes the socket, and removes the path. The
    parent directory is created with mode 0700 when missing, and the socket is
    mode 0600.
    """

    def __init__(
        self,
        socket_path: str | os.PathLike[str] | None,
        executor: CallExecutor,
    ) -> None:
        self.path = resolve_socket_path(socket_path)
        self._executor = executor
        self._server: _ThreadingUnixServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def serving(self) -> bool:
        return self._server is not None

    def start(self) -> None:
        """Bind and start serving. Raises :class:`DaemonError` on conflict."""

        if self._server is not None:
            return
        _prepare_socket_parent(self.path)
        _remove_stale_socket(self.path)
        try:
            server = _ThreadingUnixServer(str(self.path), _ControlHandler, control=self)
        except OSError as exc:
            raise DaemonError(f"cannot listen on the daemon socket {self.path}: {exc}") from exc
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.1},
            name="telephone-agent-control-server",
            daemon=True,
        )
        self._thread.start()
        logger.info("control socket listening at %s", self.path)

    def stop(self) -> None:
        """Stop accepting requests, close the listener, and unlink our socket."""

        server = self._server
        if server is None:
            return
        thread = self._thread
        if thread is not None and thread.is_alive():
            server.shutdown()
        server.server_close()
        if thread is not None:
            thread.join(2.0)
        self._server = None
        self._thread = None
        try:
            if self.path.is_socket():
                self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("cannot remove the daemon socket %s", self.path, exc_info=True)

    def dispatch(self, payload: object) -> dict[str, object]:
        """Validate one request and return the response object.

        Exposed for tests; handlers call it after parsing a line.
        """

        if not isinstance(payload, dict):
            return _invalid("request must be a JSON object")
        command = payload.get("command")
        if command == "call":
            return self._dispatch_call(payload)
        if command == "status":
            return {"ok": True, **self._executor.status()}
        if command == "hangup":
            if not self._executor.hangup():
                return {
                    "ok": False,
                    "error": "no_active_call",
                    "message": "no call is active",
                }
            return {"ok": True}
        return _invalid("command must be 'call', 'status', or 'hangup'")

    def _dispatch_call(self, payload: Mapping[str, object]) -> dict[str, object]:
        allowed = {"command", "number", "objective", "max_call_seconds"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            return _invalid(f"unknown call field(s): {', '.join(unknown)}")

        number = payload.get("number")
        objective = payload.get("objective", "")
        max_call_seconds = payload.get("max_call_seconds")
        if not isinstance(number, str) or not number:
            return _invalid("call requires a number in E.164 format")
        if not isinstance(objective, str):
            return _invalid("objective must be a string")
        if max_call_seconds is not None and (
            isinstance(max_call_seconds, bool) or not isinstance(max_call_seconds, (int, float))
        ):
            return _invalid("max_call_seconds must be a number")

        try:
            request = CallRequest(
                direction="outbound",
                number=number,
                objective=objective,
                max_call_seconds=max_call_seconds,
            )
        except ValueError as exc:
            return _invalid(str(exc))

        try:
            result = self._executor.submit(request)
        except BusyError as exc:
            return {"ok": False, "error": "busy", "message": str(exc)}
        except Exception as exc:
            return {"ok": False, "error": "failed", "message": _short_error(exc)}
        return {"ok": True, "result": result}


def _invalid(message: str) -> dict[str, object]:
    return {"ok": False, "error": "invalid", "message": message}


@dataclass(frozen=True, slots=True)
class CallReply:
    """A daemon response plus whether the local caller was interrupted."""

    payload: Mapping[str, object]
    interrupted: bool = False


class DaemonClient:
    """Small client for the local daemon control protocol.

    Each call opens one connection, writes one line, and reads one response.
    ``call`` keeps the connection open until the call ends, so ``status`` and
    ``hangup`` can be sent on separate connections while it is in progress.
    """

    def __init__(
        self,
        socket_path: str | os.PathLike[str] | None = None,
        *,
        connect_timeout_s: float = 2.0,
    ) -> None:
        self.path = resolve_socket_path(socket_path)
        self._connect_timeout_s = connect_timeout_s

    def call(
        self,
        number: str,
        *,
        objective: str = "",
        max_call_seconds: float | None = None,
        timeout_s: float | None = None,
        on_interrupt: Callable[[], None] | None = None,
    ) -> CallReply:
        """Submit an outbound call and wait for the daemon's final result.

        When *on_interrupt* is supplied, Ctrl-C invokes it (normally a hangup on
        a separate connection) and then keeps waiting so the caller can print
        the interrupted result. Without it, ``KeyboardInterrupt`` propagates.
        """

        payload: dict[str, object] = {
            "command": "call",
            "number": number,
            "objective": objective,
        }
        if max_call_seconds is not None:
            payload["max_call_seconds"] = max_call_seconds

        sock = self._connect()
        buffer = bytearray()
        interrupted = False
        try:
            if timeout_s is not None:
                sock.settimeout(timeout_s)
            else:
                sock.settimeout(None)
            _send(sock, payload)
            while True:
                try:
                    response = _read_response(sock, buffer)
                    break
                except KeyboardInterrupt:
                    if on_interrupt is None:
                        raise
                    interrupted = True
                    try:
                        on_interrupt()
                    except Exception:
                        logger.warning("cannot send hangup after the interrupt", exc_info=True)
        except OSError as exc:
            raise DaemonError(f"the daemon connection failed: {exc}") from exc
        finally:
            sock.close()
        return CallReply(response, interrupted=interrupted)

    def status(self, *, timeout_s: float = 5.0) -> dict[str, object]:
        """Return the daemon's idle or in-call status."""

        return self._request({"command": "status"}, timeout_s)

    def hangup(self, *, timeout_s: float = 5.0) -> dict[str, object]:
        """Ask the daemon to stop the active call."""

        return self._request({"command": "hangup"}, timeout_s)

    def _request(self, payload: Mapping[str, object], timeout_s: float | None) -> dict[str, object]:
        sock = self._connect()
        try:
            sock.settimeout(timeout_s)
            _send(sock, payload)
            return _read_response(sock, bytearray())
        except OSError as exc:
            raise DaemonError(f"the daemon connection failed: {exc}") from exc
        finally:
            sock.close()

    def _connect(self) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self._connect_timeout_s)
        try:
            sock.connect(str(self.path))
        except (FileNotFoundError, ConnectionRefusedError, NotADirectoryError) as exc:
            sock.close()
            raise DaemonUnavailableError(f"no daemon is listening on {self.path}: {exc}") from exc
        except OSError as exc:
            sock.close()
            raise DaemonUnavailableError(
                f"cannot connect to the daemon socket {self.path}: {exc}"
            ) from exc
        return sock


def _send(sock: socket.socket, payload: Mapping[str, object]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
    sock.sendall(data)


def _read_response(sock: socket.socket, buffer: bytearray) -> dict[str, object]:
    while b"\n" not in buffer:
        chunk = sock.recv(4096)
        if not chunk:
            raise DaemonError("the daemon closed the connection without a response")
        buffer.extend(chunk)
        if len(buffer) > _MAX_RESPONSE_BYTES:
            raise DaemonError("the daemon response is too large")
    line, _, _rest = bytes(buffer).partition(b"\n")
    try:
        payload = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DaemonError("the daemon returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise DaemonError("the daemon response must be a JSON object")
    return payload
