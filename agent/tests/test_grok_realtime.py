"""Tests for the independent Grok Voice realtime adapter."""

from __future__ import annotations

import base64
import json
import threading
from collections import deque
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from telephone_agent.config import RealtimeConfig
from telephone_agent.consult import Consultation, ConsultationOutcome
from telephone_agent.grok_realtime import (
    GrokRealtimeError,
    GrokRealtimeSession,
    build_session_update,
    resolve_api_key,
)
from telephone_agent.pipeline import (
    AgentReply,
    CallEndReason,
    CallFinished,
    CallState,
    ConsultationFinished,
    ConsultationStarted,
    StateChanged,
    Utterance,
)

_NUMBER = "+15555550100"
_FRAME = bytes(640)


class FakeConnection:
    def __init__(self, events: list[str | bytes | BaseException]) -> None:
        self.events = deque(events)
        self.sent_json: list[dict[str, Any]] = []
        self.sent_bytes: list[bytes] = []
        self.connected = False
        self._closed = False

    def connect(self) -> None:
        self.connected = True

    def send_json(self, payload: dict[str, Any]) -> None:
        self.sent_json.append(payload)

    def send_bytes(self, payload: bytes) -> None:
        self.sent_bytes.append(payload)

    def recv(self, timeout: float = 0.0) -> str | bytes | None:
        if not self.events:
            return None
        item = self.events.popleft()
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self) -> None:
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed


class FakePlayback:
    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.responses = 0
        self.flushes = 0
        self.closed = 0

    def enqueue(self, pcm: bytes) -> None:
        self.chunks.append(pcm)

    def begin_response(self) -> None:
        self.responses += 1

    def flush(self) -> None:
        self.flushes += 1

    def close(self) -> None:
        self.closed += 1


class FakeAudio:
    sample_rate = 16_000
    channels = 1
    frame_ms = 20
    frame_bytes = len(_FRAME)
    description = "fake realtime audio"

    def __init__(self, frames: list[bytes], playback: FakePlayback) -> None:
        self._frames = frames
        self.playback = playback
        self.started = 0
        self.stopped = 0
        self.on_start: Callable[[], None] | None = None

    def start(self) -> None:
        self.started += 1
        if self.on_start is not None:
            self.on_start()

    def stop(self) -> None:
        self.stopped += 1

    def frames(self) -> Iterator[bytes]:
        yield from self._frames

    def play(self, pcm: bytes) -> None:
        self.playback.enqueue(pcm)

    def start_playback(self) -> FakePlayback:
        return self.playback


class FakeDialer:
    def __init__(self) -> None:
        self.placed: list[str] = []
        self.hangups = 0

    def place_call(self, number: str) -> dict[str, object]:
        self.placed.append(number)
        return {}

    def hangup(self) -> dict[str, object]:
        self.hangups += 1
        return {}


class FakeBroker:
    def __init__(self, answer: str | None = "4 pm is fine") -> None:
        self.answer = answer
        self.questions: list[tuple[str, float]] = []

    def ask(self, question: str, timeout_s: float, *, on_open: Any = None) -> Any:
        self.questions.append((question, timeout_s))
        consultation = Consultation(id="consult-1", question=question, created_at=0.0)
        if on_open is not None:
            on_open(consultation)
        return ConsultationOutcome(
            consultation=consultation,
            answer=self.answer,
            timed_out=self.answer is None,
        )


def _config(**overrides: Any) -> RealtimeConfig:
    values: dict[str, Any] = {"kind": "grok", "api_key": "test-key"}
    values.update(overrides)
    return RealtimeConfig(**values)


def _event(payload: dict[str, Any]) -> str:
    return json.dumps(payload)


def _make_session(
    *,
    events: list[str | bytes | BaseException],
    frames: list[bytes] | None = None,
    config: RealtimeConfig | None = None,
    broker: FakeBroker | None = None,
    on_event: Any = None,
    stop_event: threading.Event | None = None,
) -> tuple[GrokRealtimeSession, FakeConnection, FakeAudio, FakePlayback, FakeDialer]:
    connection = FakeConnection(events)
    playback = FakePlayback()
    audio = FakeAudio(frames or [_FRAME] * 6, playback)
    dialer = FakeDialer()
    session = GrokRealtimeSession(
        audio=audio,
        dialer=dialer,
        number=_NUMBER,
        config=config or _config(),
        consultations=broker,
        on_event=on_event,
        stop_event=stop_event,
        connection_factory=lambda: connection,
        playback_factory=lambda _audio: playback,
    )
    return session, connection, audio, playback, dialer


def test_build_session_update_uses_binary_pcm_and_server_vad() -> None:
    update = build_session_update(
        _config(language="en-IN"),
        sample_rate=16_000,
        objective="Confirm the 4 pm call.",
        has_consult=True,
        holding_phrase="One moment please.",
    )

    assert update["type"] == "session.update"
    session = update["session"]
    assert session["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 16_000}
    assert session["audio"]["input"]["transport"] == "binary"
    assert session["audio"]["input"]["transcription"]["language_hint"] == "en-IN"
    assert session["audio"]["output"]["format"] == {"type": "audio/pcm", "rate": 16_000}
    assert session["audio"]["output"]["transport"] == "binary"
    assert session["turn_detection"]["type"] == "server_vad"
    assert session["turn_detection"]["threshold"] == 0.5
    assert [tool["name"] for tool in session["tools"]] == ["end_call", "consult"]
    assert "Confirm the 4 pm call." in session["instructions"]
    assert "One moment please." in session["instructions"]
    assert session["reasoning"] == {"effort": "none"}


def test_build_session_update_omits_consult_without_a_broker() -> None:
    update = build_session_update(_config(), sample_rate=16_000)

    assert [tool["name"] for tool in update["session"]["tools"]] == ["end_call"]
    assert "consult tool" not in update["session"]["instructions"]


def test_greeting_is_requested_after_audio_starts() -> None:
    greeting = "Hello, this is the telephone agent calling about your appointment."
    session, connection, audio, _playback, _dialer = _make_session(
        events=[
            _event({"type": "session.updated"}),
            _event({"type": "response.created"}),
            _event({"type": "response.done"}),
        ],
        config=_config(greeting=greeting),
    )
    sent_before_start: list[int] = []
    audio.on_start = lambda: sent_before_start.append(len(connection.sent_json))

    session.run()

    # The session.update is the only event sent before the audio backend is
    # ready; the greeting must not be synthesized into a dead call.
    assert sent_before_start == [1]
    assert connection.sent_json[0]["type"] == "session.update"

    greeting_events = [
        event for event in connection.sent_json if event.get("type") == "conversation.item.create"
    ]
    assert len(greeting_events) == 1
    item = greeting_events[0]["item"]
    assert item["type"] == "message"
    assert item["role"] == "system"
    assert item["content"][0]["type"] == "input_text"
    assert greeting in item["content"][0]["text"]

    index = connection.sent_json.index(greeting_events[0])
    assert connection.sent_json[index + 1] == {"type": "response.create"}


def test_empty_greeting_sends_nothing() -> None:
    session, connection, _audio, _playback, _dialer = _make_session(
        events=[
            _event({"type": "session.updated"}),
            _event({"type": "response.created"}),
            _event({"type": "response.done"}),
        ],
    )

    session.run()

    assert [
        event for event in connection.sent_json if event.get("type") == "conversation.item.create"
    ] == []
    assert [event for event in connection.sent_json if event.get("type") == "response.create"] == []


def test_greeting_is_requested_only_once() -> None:
    greeting = "Hello there."
    session, connection, audio, _playback, _dialer = _make_session(
        events=[
            _event({"type": "session.updated"}),
            _event({"type": "response.created"}),
            _event({"type": "response.done"}),
            _event({"type": "input_audio_buffer.speech_started"}),
            _event({"type": "response.created"}),
            _event({"type": "response.done"}),
        ],
        config=_config(greeting=greeting),
    )

    def request_twice() -> None:
        session._request_greeting()
        session._request_greeting()

    audio.on_start = request_twice

    session.run()

    greeting_events = [
        event for event in connection.sent_json if event.get("type") == "conversation.item.create"
    ]
    assert len(greeting_events) == 1
    assert sum(1 for event in connection.sent_json if event.get("type") == "response.create") == 1


def test_run_streams_audio_and_emits_transcripts() -> None:
    emitted: list[object] = []
    session, connection, audio, playback, dialer = _make_session(
        events=[
            _event({"type": "session.updated"}),
            _event(
                {
                    "type": "conversation.item.input_audio_transcription.completed",
                    "item_id": "item-1",
                    "transcript": "hello",
                }
            ),
            _event(
                {
                    "type": "response.output_audio_transcript.done",
                    "transcript": "hi there",
                }
            ),
            _event(
                {
                    "type": "response.function_call_arguments.done",
                    "name": "end_call",
                    "call_id": "call-1",
                    "arguments": "{}",
                }
            ),
            _event({"type": "response.done"}),
        ],
        on_event=emitted.append,
    )

    result = session.run()

    assert result.state is CallState.IDLE
    assert result.reason is CallEndReason.COMPLETED
    assert result.turns == 1
    assert connection.connected
    assert connection.sent_json[0]["type"] == "session.update"
    assert connection.sent_bytes
    assert dialer.placed == [_NUMBER]
    assert dialer.hangups == 1
    assert audio.started == 1
    assert audio.stopped == 1
    assert playback.closed == 1
    assert any(isinstance(event, Utterance) and event.text == "hello" for event in emitted)
    assert any(isinstance(event, AgentReply) and event.text == "hi there" for event in emitted)
    assert any(isinstance(event, CallFinished) for event in emitted)
    assert any(isinstance(event, StateChanged) for event in emitted)


def test_barge_in_flushes_playback() -> None:
    session, _connection, _audio, playback, _dialer = _make_session(
        events=[
            _event({"type": "session.updated"}),
            _FRAME,
            _event({"type": "input_audio_buffer.speech_started"}),
            _event(
                {
                    "type": "response.function_call_arguments.done",
                    "name": "end_call",
                    "call_id": "call-1",
                    "arguments": "{}",
                }
            ),
            _event({"type": "response.done"}),
        ],
    )

    result = session.run()

    assert result.reason is CallEndReason.COMPLETED
    assert playback.flushes == 1
    assert playback.chunks == [_FRAME]


def test_response_created_clears_suppression_and_decodes_json_audio() -> None:
    pcm = b"\x01\x02" * 320
    session, _connection, _audio, playback, _dialer = _make_session(
        events=[
            _event({"type": "session.updated"}),
            _event({"type": "response.created"}),
            _event(
                {
                    "type": "response.output_audio.delta",
                    "delta": base64.b64encode(pcm).decode("ascii"),
                }
            ),
            _event(
                {
                    "type": "response.function_call_arguments.done",
                    "name": "end_call",
                    "call_id": "call-6",
                    "arguments": "{}",
                }
            ),
            _event({"type": "response.done"}),
        ],
    )

    result = session.run()

    assert result.reason is CallEndReason.COMPLETED
    assert playback.responses == 1
    assert playback.chunks == [pcm]


def test_consult_tool_round_trips_through_the_broker() -> None:
    emitted: list[object] = []
    broker = FakeBroker()
    session, connection, _audio, _playback, _dialer = _make_session(
        events=[
            _event({"type": "session.updated"}),
            _event(
                {
                    "type": "response.function_call_arguments.done",
                    "name": "consult",
                    "call_id": "call-2",
                    "arguments": json.dumps({"question": "Is 4 pm still okay?"}),
                }
            ),
            _event(
                {
                    "type": "response.function_call_arguments.done",
                    "name": "end_call",
                    "call_id": "call-3",
                    "arguments": "{}",
                }
            ),
            _event({"type": "response.done"}),
        ],
        broker=broker,
        on_event=emitted.append,
    )

    result = session.run()

    assert result.reason is CallEndReason.COMPLETED
    assert broker.questions == [("Is 4 pm still okay?", 8.0)]
    outputs = [
        event["item"]["output"]
        for event in connection.sent_json
        if event.get("type") == "conversation.item.create"
    ]
    assert outputs[0] == json.dumps({"answer": "4 pm is fine"}, separators=(",", ":"))
    assert "ending" in outputs[1]
    assert any(event.get("type") == "response.create" for event in connection.sent_json)
    assert any(isinstance(event, ConsultationStarted) for event in emitted)
    assert any(isinstance(event, ConsultationFinished) for event in emitted)


def test_consult_timeout_returns_an_error_to_the_model() -> None:
    broker = FakeBroker(answer=None)
    session, connection, _audio, _playback, _dialer = _make_session(
        events=[
            _event({"type": "session.updated"}),
            _event(
                {
                    "type": "response.function_call_arguments.done",
                    "name": "consult",
                    "call_id": "call-4",
                    "arguments": json.dumps({"question": "Is 4 pm still okay?"}),
                }
            ),
            _event(
                {
                    "type": "response.function_call_arguments.done",
                    "name": "end_call",
                    "call_id": "call-5",
                    "arguments": "{}",
                }
            ),
            _event({"type": "response.done"}),
        ],
        broker=broker,
    )

    result = session.run()

    assert result.reason is CallEndReason.COMPLETED
    outputs = [
        event["item"]["output"]
        for event in connection.sent_json
        if event.get("type") == "conversation.item.create"
    ]
    assert "no answer" in outputs[0]


def test_missing_api_key_fails_before_dialing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    session, connection, _audio, _playback, dialer = _make_session(
        events=[],
        config=_config(api_key=""),
    )

    with pytest.raises(GrokRealtimeError, match="XAI_API_KEY"):
        session.run()

    assert dialer.placed == []
    assert dialer.hangups == 0
    assert not connection.connected


def test_connection_error_ends_the_call_as_failed() -> None:
    session, _connection, _audio, _playback, dialer = _make_session(
        events=[
            _event({"type": "session.updated"}),
            GrokRealtimeError("socket dropped"),
        ],
    )

    result = session.run()

    assert result.state is CallState.FAILED
    assert result.reason is CallEndReason.TURN_FAILED
    assert dialer.hangups == 1


def test_stop_event_interrupts_without_waiting_for_speech() -> None:
    stop_event = threading.Event()
    stop_event.set()
    session, _connection, _audio, _playback, _dialer = _make_session(
        events=[_event({"type": "session.updated"})],
        stop_event=stop_event,
    )

    result = session.run()

    assert result.reason is CallEndReason.INTERRUPTED
    assert result.state is CallState.IDLE


def test_resolve_api_key_prefers_config_then_environment() -> None:
    assert (
        resolve_api_key(_config(api_key="config-key"), {"XAI_API_KEY": "env-key"}) == "config-key"
    )
    assert resolve_api_key(_config(api_key=""), {"XAI_API_KEY": "env-key"}) == "env-key"
    assert resolve_api_key(_config(api_key="  "), {}) == ""


def test_session_update_rejects_non_grok_config() -> None:
    with pytest.raises(ValueError, match="realtime.kind"):
        GrokRealtimeSession(
            audio=FakeAudio([], FakePlayback()),
            dialer=FakeDialer(),
            number=_NUMBER,
            config=RealtimeConfig(),
        )


def test_websocket_connection_round_trips_json_and_binary() -> None:
    pytest.importorskip("websockets.sync.server")
    from telephone_agent.grok_realtime import _WebSocketConnection
    from websockets.sync.server import serve

    seen_headers: list[str] = []

    def handler(websocket: Any) -> None:
        seen_headers.append(websocket.request.headers.get("Authorization", ""))
        for message in websocket:
            websocket.send(message)

    server = serve(handler, "127.0.0.1", 0)
    port = server.socket.getsockname()[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    connection = _WebSocketConnection(
        url=f"ws://127.0.0.1:{port}",
        headers={"Authorization": "Bearer test-key"},
    )
    try:
        connection.connect()
        connection.send_json({"type": "session.update"})
        connection.send_bytes(b"\x00\x01")

        first = connection.recv(timeout=2.0)
        second = connection.recv(timeout=2.0)

        assert first == json.dumps({"type": "session.update"}, separators=(",", ":"))
        assert second == b"\x00\x01"
        assert seen_headers == ["Bearer test-key"]
        assert not connection.closed
    finally:
        connection.close()
        server.shutdown()
        thread.join(timeout=5.0)

    assert connection.closed
