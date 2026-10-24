"""Tests for the executor, control protocol, and local client."""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest
from telephone_agent import daemon as daemon_module
from telephone_agent.daemon import (
    BusyError,
    CallExecutor,
    CallRequest,
    ControlServer,
    DaemonClient,
    DaemonError,
    DaemonUnavailableError,
    resolve_socket_path,
)
from telephone_agent.pipeline import (
    CallEndReason,
    CallResult,
    CallState,
    Utterance,
)


def _completed(*, turns: int = 0, duration_s: float = 1.5) -> CallResult:
    return CallResult(
        state=CallState.IDLE,
        reason=CallEndReason.COMPLETED,
        duration_s=duration_s,
        turns=turns,
    )


def _wait_until(predicate: object, timeout: float = 2.0) -> bool:
    assert callable(predicate)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _raw_request(path: Path, data: bytes) -> dict[str, object]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(2.0)
    try:
        sock.connect(str(path))
        sock.sendall(data)
        buffer = bytearray()
        while b"\n" not in buffer:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buffer.extend(chunk)
    finally:
        sock.close()
    line, _, _rest = bytes(buffer).partition(b"\n")
    return json.loads(line.decode("utf-8"))


def test_executor_serializes_calls_and_rejects_busy() -> None:
    started = threading.Event()
    release = threading.Event()
    seen: list[CallRequest] = []

    def factory(request: CallRequest, _on_event: object) -> object:
        seen.append(request)

        class Session:
            def run(self) -> CallResult:
                started.set()
                release.wait(2.0)
                return _completed(turns=2, duration_s=3.5)

        return Session()

    executor = CallExecutor(session_factory=factory)  # type: ignore[arg-type]
    executor.start()
    outcomes: list[dict[str, object]] = []

    def first_call() -> None:
        outcomes.append(executor.submit(CallRequest("outbound", "+15555550100")))

    thread = threading.Thread(target=first_call)
    thread.start()
    try:
        assert started.wait(2.0)
        assert executor.busy is True
        status = executor.status()
        assert status["state"] == "in_call"
        assert status["direction"] == "outbound"
        assert status["number"] == "*******0100"
        assert status["since"] is not None

        with pytest.raises(BusyError, match="already active"):
            executor.submit(CallRequest("outbound", "+15555550101"))

        release.set()
        thread.join(2.0)
        assert not thread.is_alive()
        assert outcomes[0]["state"] == "idle"
        assert outcomes[0]["reason"] == "completed"
        assert outcomes[0]["turns"] == 2
        assert len(seen) == 1
        assert executor.busy is False
    finally:
        release.set()
        executor.stop()


def test_executor_records_transcript_direction_and_metadata() -> None:
    transcript_requests: list[tuple[CallRequest, str]] = []
    events: list[object] = []
    end: list[dict[str, object]] = []

    class Transcript:
        def record(self, event: object) -> None:
            events.append(event)

        def call_end(self, **fields: object) -> None:
            end.append(fields)

        def close(self) -> None:
            pass

    def transcript_factory(request: CallRequest, call_id: str) -> Transcript:
        transcript_requests.append((request, call_id))
        return Transcript()

    def session_factory(request: CallRequest, on_event: object) -> object:
        class Session:
            def run(self) -> CallResult:
                assert callable(on_event)
                on_event(Utterance("hello"))
                return _completed(turns=1, duration_s=2.0)

        return Session()

    executor = CallExecutor(
        session_factory=session_factory,  # type: ignore[arg-type]
        transcript_factory=transcript_factory,
        id_factory=lambda: "call-id",
    )
    executor.start()
    try:
        result = executor.submit(CallRequest("inbound", max_call_seconds=30))
    finally:
        executor.stop()

    assert result["reason"] == "completed"
    assert transcript_requests[0][0].direction == "inbound"
    assert transcript_requests[0][1] == "call-id"
    assert any(isinstance(event, Utterance) for event in events)
    assert end == [
        {
            "state": "idle",
            "reason": "completed",
            "turns": 1,
            "duration_s": 2.0,
        }
    ]


def test_control_server_status_call_and_hangup(tmp_path: Path) -> None:
    started = threading.Event()

    def factory(request: CallRequest, _on_event: object) -> object:
        class Session:
            def run(self) -> CallResult:
                started.set()
                request.stop_event.wait(5.0)
                return CallResult(
                    state=CallState.IDLE,
                    reason=CallEndReason.INTERRUPTED,
                    duration_s=0.5,
                    turns=0,
                )

        return Session()

    executor = CallExecutor(session_factory=factory)  # type: ignore[arg-type]
    executor.start()
    server = ControlServer(tmp_path / "daemon.sock", executor)
    server.start()
    socket_path = server.path
    try:
        client = DaemonClient(socket_path)
        status = client.status()
        assert status == {
            "ok": True,
            "state": "idle",
            "direction": None,
            "number": "",
            "since": None,
        }
        hangup = client.hangup()
        assert hangup["ok"] is False
        assert hangup["error"] == "no_active_call"

        replies: list[object] = []

        def place_call() -> None:
            replies.append(DaemonClient(socket_path).call("+15555550101", timeout_s=5.0))

        thread = threading.Thread(target=place_call)
        thread.start()
        assert started.wait(2.0)

        busy = DaemonClient(socket_path).call("+15555550102", timeout_s=5.0)
        assert busy.payload["ok"] is False
        assert busy.payload["error"] == "busy"

        active = client.status()
        assert active["state"] == "in_call"
        assert active["direction"] == "outbound"
        assert active["number"] == "*******0101"
        assert active["since"] is not None

        assert client.hangup()["ok"] is True
        thread.join(5.0)
        assert not thread.is_alive()
        reply = replies[0]
        assert isinstance(reply, daemon_module.CallReply)
        assert reply.payload["ok"] is True
        result = reply.payload["result"]
        assert isinstance(result, dict)
        assert result["state"] == "idle"
        assert result["reason"] == "interrupted"

        assert _wait_until(lambda: client.status()["state"] == "idle")
    finally:
        server.stop()
        executor.stop()

    assert not socket_path.exists()


def test_control_server_rejects_invalid_requests(tmp_path: Path) -> None:
    executor = CallExecutor(session_factory=lambda _request, _event: object())
    executor.start()
    server = ControlServer(tmp_path / "daemon.sock", executor)
    server.start()
    try:
        assert _raw_request(server.path, b'{"command":"call"}\n') == {
            "ok": False,
            "error": "invalid",
            "message": "call requires a number in E.164 format",
        }
        assert _raw_request(server.path, b"not json\n") == {
            "ok": False,
            "error": "invalid",
            "message": "request is not valid JSON",
        }
        assert _raw_request(server.path, b'{"command":"bogus"}\n') == {
            "ok": False,
            "error": "invalid",
            "message": "command must be 'call', 'status', or 'hangup'",
        }
        malformed = _raw_request(
            server.path,
            b'{"command":"call","number":"+15555550100","objective":7}\n',
        )
        assert malformed["ok"] is False
        assert malformed["error"] == "invalid"
        oversized = _raw_request(server.path, b"{" + b" " * 70_000 + b"}\n")
        assert oversized == {
            "ok": False,
            "error": "invalid",
            "message": "request is too large",
        }
    finally:
        server.stop()
        executor.stop()


def test_control_server_creates_a_private_socket_and_removes_stale_files(
    tmp_path: Path,
) -> None:
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir()
    socket_path = runtime_dir / "agent.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(socket_path))
    stale.close()
    assert socket_path.exists()

    executor = CallExecutor(session_factory=lambda _request, _event: object())
    executor.start()
    server = ControlServer(socket_path, executor)
    server.start()
    try:
        assert server.serving is True
        assert socket_path.is_socket()
        assert socket_path.stat().st_mode & 0o777 == 0o600
        assert DaemonClient(socket_path).status()["ok"] is True

        second = ControlServer(socket_path, executor)
        with pytest.raises(DaemonError, match="already listening"):
            second.start()
    finally:
        server.stop()
        executor.stop()

    assert not socket_path.exists()


def test_control_server_creates_the_parent_directory_with_0700(tmp_path: Path) -> None:
    parent = tmp_path / "run" / "user"
    executor = CallExecutor(session_factory=lambda _request, _event: object())
    executor.start()
    server = ControlServer(parent / "agent.sock", executor)
    server.start()
    try:
        assert parent.is_dir()
        assert parent.stat().st_mode & 0o777 == 0o700
    finally:
        server.stop()
        executor.stop()


def test_executor_stop_cancels_the_active_call_and_rejects_new_work() -> None:
    started = threading.Event()

    def factory(request: CallRequest, _on_event: object) -> object:
        class Session:
            def run(self) -> CallResult:
                started.set()
                request.stop_event.wait(5.0)
                return CallResult(
                    state=CallState.IDLE,
                    reason=CallEndReason.INTERRUPTED,
                    duration_s=0.25,
                    turns=0,
                )

        return Session()

    executor = CallExecutor(session_factory=factory)  # type: ignore[arg-type]
    executor.start()
    outcomes: list[dict[str, object]] = []
    thread = threading.Thread(
        target=lambda: outcomes.append(executor.submit(CallRequest("outbound", "+15555550103")))
    )
    thread.start()
    assert started.wait(2.0)

    executor.stop()
    thread.join(2.0)

    assert not thread.is_alive()
    assert outcomes[0]["reason"] == "interrupted"
    with pytest.raises(BusyError):
        executor.enqueue(CallRequest("outbound", "+15555550103"))


def test_client_reports_an_unavailable_daemon(tmp_path: Path) -> None:
    client = DaemonClient(tmp_path / "missing.sock")

    with pytest.raises(DaemonUnavailableError, match="no daemon is listening"):
        client.status()
    with pytest.raises(DaemonUnavailableError, match="no daemon is listening"):
        client.call("+15555550100")


class _FakeSocket:
    def __init__(self) -> None:
        self.sent = bytearray()
        self.closed = False

    def settimeout(self, _timeout: float | None) -> None:
        pass

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)

    def recv(self, _size: int) -> bytes:
        return b""

    def close(self) -> None:
        self.closed = True


def test_client_interrupt_reports_hangup_and_keeps_waiting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = DaemonClient(tmp_path / "daemon.sock")
    fake = _FakeSocket()
    monkeypatch.setattr(client, "_connect", lambda: fake)
    attempts = 0
    hangups: list[bool] = []

    def flaky_read(_sock: object, _buffer: bytearray) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise KeyboardInterrupt
        return {
            "ok": True,
            "result": {
                "state": "idle",
                "reason": "interrupted",
                "turns": 0,
                "duration_s": 0.5,
            },
        }

    monkeypatch.setattr(daemon_module, "_read_response", flaky_read)

    reply = client.call(
        "+15555550100",
        on_interrupt=lambda: hangups.append(True),
    )

    assert reply.interrupted is True
    assert hangups == [True]
    assert reply.payload["ok"] is True
    assert fake.closed is True
    assert b'"command": "call"' in fake.sent


def test_resolve_socket_path_uses_xdg_runtime_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))

    assert resolve_socket_path("") == tmp_path / "telephone-agent.sock"


def test_resolve_socket_path_falls_back_to_the_temp_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

    assert resolve_socket_path("") == Path(tempfile.gettempdir()) / (
        f"telephone-agent-{os.getuid()}.sock"
    )


def test_resolve_socket_path_expands_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))

    assert resolve_socket_path("~/agent.sock") == tmp_path / "agent.sock"


def test_resolve_socket_path_rejects_a_too_long_path() -> None:
    with pytest.raises(DaemonError, match="too long"):
        resolve_socket_path("/tmp/" + "a" * 120 + ".sock")
