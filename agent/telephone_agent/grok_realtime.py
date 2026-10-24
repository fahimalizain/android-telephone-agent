"""Independent xAI Grok Voice realtime adapter.

This module is deliberately separate from :mod:`telephone_agent.pipeline` and
:mod:`telephone_agent.brain`. The pipeline is an utterance-at-a-time, text-in /
text-out design: VAD, then an STT adapter, then a brain, then a TTS adapter. The
Grok Voice API is a full-duplex speech-to-speech service that performs all four
steps in one WebSocket session, so squeezing it into those seams would hide the
latency and turn-taking that make it useful.

:class:`GrokRealtimeSession` is a drop-in replacement for
:class:`~telephone_agent.pipeline.CallSession`: it dials, streams the call
downlink to xAI as 16-bit PCM, plays the returned PCM into the uplink, and
returns the same :class:`~telephone_agent.pipeline.CallResult`. It does not use
:class:`~telephone_agent.pipeline.Transcriber`,
:class:`~telephone_agent.pipeline.Synthesizer`, or
:class:`~telephone_agent.brain.Brain`.

Audio is sent with the binary WebSocket transport so neither direction pays a
base64 cost. The processing rate is ``audio.sample_rate`` (16 kHz by default),
which is one of the rates the API accepts. Server-side VAD supplies endpointing
and barge-in. The adapter stops local playback on
``input_audio_buffer.speech_started`` and drops the cancelled response's audio.

The API key is read from ``realtime.api_key`` or, preferably, the
``XAI_API_KEY`` environment variable. It is never logged or returned.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import math
import os
import queue
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol
from urllib.parse import quote

from .audio import AudioDevice, AudioError
from .config import RealtimeConfig, ensure_e164
from .consult import ConsultationBroker
from .dialer import Dialer, mask_number
from .pipeline import (
    AgentReply,
    CallEndReason,
    CallFinished,
    CallResult,
    CallState,
    ConsultationFinished,
    ConsultationStarted,
    SessionEvent,
    StateChanged,
    Utterance,
)

__all__ = [
    "DEFAULT_INSTRUCTIONS",
    "GrokRealtimeError",
    "GrokRealtimeSession",
    "build_session_update",
    "resolve_api_key",
]

logger = logging.getLogger(__name__)

DEFAULT_INSTRUCTIONS = (
    "You are a concise, friendly voice assistant on a live telephone call. "
    "Keep replies to one or two short sentences, normally under 25 words. "
    "Ask one question at a time. Use plain spoken language: no lists, markdown, "
    "URLs, emoji, or stage directions. If the caller asks whether you are a "
    "person, say that you are an automated assistant. Do not invent facts. "
    "When the objective is complete, say goodbye and call the end_call tool."
)

_USER_AGENT = "telephone-agent/0.1"
_SESSION_READY_TIMEOUT_S = 15.0
_PLAYBACK_QUEUE_CHUNKS = 100
_OUTBOUND_QUEUE_MESSAGES = 256


class GrokRealtimeError(RuntimeError):
    """Raised when the realtime session cannot be configured or used."""


def resolve_api_key(config: RealtimeConfig, env: Mapping[str, str] | None = None) -> str:
    """Return the xAI key from config or ``XAI_API_KEY``.

    The config value wins so an operator can use a dedicated environment
    override. A blank value is never treated as configured.
    """

    if config.api_key.strip():
        return config.api_key.strip()
    source = os.environ if env is None else env
    return source.get("XAI_API_KEY", "").strip()


def build_session_update(
    config: RealtimeConfig,
    *,
    sample_rate: int,
    objective: str = "",
    has_consult: bool = False,
    holding_phrase: str = "One moment please.",
) -> dict[str, Any]:
    """Build the ``session.update`` payload for one call.

    This is pure so it can be tested without a WebSocket. ``sample_rate`` is the
    pipeline's processing rate, which the audio backend already speaks.
    """

    transcription: dict[str, Any] = {"model": "grok-transcribe"}
    language = config.language.strip()
    if language:
        transcription["language_hint"] = language

    tools: list[dict[str, Any]] = [
        {
            "type": "function",
            "name": "end_call",
            "description": (
                "End the telephone call. Use this only after you have finished "
                "speaking, either because the objective is complete or because "
                "the caller asked to hang up."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Short reason for ending the call.",
                    }
                },
            },
        }
    ]
    if has_consult:
        tools.append(
            {
                "type": "function",
                "name": "consult",
                "description": (
                    "Ask the requester for a fact that is genuinely missing and "
                    "that the caller cannot supply. Say the holding phrase before "
                    "calling this; the call pauses while the requester answers. "
                    "Do not use this for small talk."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "One short, specific question.",
                        }
                    },
                    "required": ["question"],
                },
            }
        )

    session: dict[str, Any] = {
        "instructions": _compose_instructions(
            config,
            objective=objective,
            has_consult=has_consult,
            holding_phrase=holding_phrase,
        ),
        "voice": config.voice,
        "reasoning": {"effort": config.reasoning_effort},
        "audio": {
            "input": {
                "format": {"type": "audio/pcm", "rate": sample_rate},
                "transport": "binary",
                "transcription": transcription,
            },
            "output": {
                "format": {"type": "audio/pcm", "rate": sample_rate},
                "transport": "binary",
            },
        },
        "turn_detection": {
            "type": "server_vad",
            "threshold": config.vad_threshold,
            "prefix_padding_ms": config.prefix_padding_ms,
            "silence_duration_ms": config.silence_duration_ms,
        },
        "tools": tools,
    }
    if config.idle_timeout_ms:
        session["turn_detection"]["idle_timeout_ms"] = config.idle_timeout_ms
    return {"type": "session.update", "session": session}


def _compose_instructions(
    config: RealtimeConfig,
    *,
    objective: str,
    has_consult: bool,
    holding_phrase: str,
) -> str:
    parts = [config.instructions.strip() or DEFAULT_INSTRUCTIONS]
    if objective.strip():
        parts.append(f"Call objective: {objective.strip()}")
    parts.append("Reply in the language the caller uses when you can.")
    if has_consult:
        phrase = holding_phrase.strip() or "One moment please."
        parts.append(
            f'If you need a fact from the requester, say "{phrase}" and then '
            "call the consult tool. Ask one short question."
        )
    return " ".join(parts)


class RealtimeConnection(Protocol):
    """The subset of a WebSocket connection the session needs.

    The protocol exists so tests can run the whole call loop against an
    in-memory fake, including barge-in and tool calls, without a network.
    """

    def connect(self) -> None:
        """Open the connection and block until it is ready."""

    def send_json(self, payload: dict[str, Any]) -> None:
        """Send one JSON event."""

    def send_bytes(self, payload: bytes) -> None:
        """Send one binary audio chunk."""

    def recv(self, timeout: float = 0.0) -> str | bytes | None:
        """Return one message, or ``None`` when none arrived within *timeout*."""

    def close(self) -> None:
        """Close the connection and join its worker."""

    @property
    def closed(self) -> bool:
        """Whether the connection is closed or has failed."""


class _WebSocketConnection:
    """Synchronous facade over the ``websockets`` asyncio client.

    ``websockets`` is an optional dependency. It is imported only when this
    class is constructed, so the core runtime and the test suite do not require
    it.
    """

    def __init__(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        connect_fn: Callable[..., Any] | None = None,
        open_timeout: float = 10.0,
    ) -> None:
        self._url = url
        self._headers = dict(headers)
        self._open_timeout = open_timeout
        self._connect_fn = connect_fn or _load_connect()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._outbound: asyncio.Queue[tuple[str, object] | None] | None = None
        self._stop: asyncio.Event | None = None
        self._inbound: queue.Queue[str | bytes | BaseException] = queue.Queue()
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._thread_main,
            name="grok-realtime-ws",
            daemon=True,
        )

    def connect(self) -> None:
        self._thread.start()
        if not self._ready.wait(timeout=self._open_timeout + 5.0):
            raise GrokRealtimeError("timed out connecting to the Grok Voice WebSocket")
        if self._error is not None:
            raise GrokRealtimeError(
                f"cannot connect to the Grok Voice WebSocket: {self._error}"
            ) from self._error

    def send_json(self, payload: dict[str, Any]) -> None:
        self._submit(("json", payload))

    def send_bytes(self, payload: bytes) -> None:
        self._submit(("bytes", payload))

    def recv(self, timeout: float = 0.0) -> str | bytes | None:
        try:
            item = self._inbound.get(timeout=timeout)
        except queue.Empty:
            return None
        if isinstance(item, BaseException):
            raise GrokRealtimeError(str(item)) from item
        return item

    def close(self) -> None:
        if self._closed.is_set():
            return
        loop = self._loop
        stop = self._stop
        if loop is not None and stop is not None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(stop.set)
        if self._thread.is_alive():
            self._thread.join(timeout=self._open_timeout + 5.0)
        self._closed.set()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def _submit(self, item: tuple[str, object]) -> None:
        loop = self._loop
        outbound = self._outbound
        if loop is None or outbound is None:
            raise GrokRealtimeError("the Grok Voice WebSocket is not connected")
        if self._closed.is_set():
            raise GrokRealtimeError("the Grok Voice WebSocket is closed")

        def enqueue() -> None:
            if item[0] == "json":
                # Control events must win over queued audio. Drop the oldest
                # audio chunk if the writer is behind.
                while outbound.full():
                    with contextlib.suppress(asyncio.QueueEmpty):
                        outbound.get_nowait()
                outbound.put_nowait(item)
                return
            if outbound.full():
                # Call audio is real-time. A stale frame is worth less than a
                # control event or a clean shutdown, so drop it.
                return
            outbound.put_nowait(item)

        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(enqueue)

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except BaseException as exc:  # noqa: BLE001 - surfaced through recv/connect
            self._error = exc
            self._inbound.put(exc)
        finally:
            self._ready.set()
            self._closed.set()
            loop.close()

    async def _main(self) -> None:
        from websockets.exceptions import ConnectionClosedOK

        self._outbound = asyncio.Queue(maxsize=_OUTBOUND_QUEUE_MESSAGES)
        self._stop = asyncio.Event()
        try:
            async with self._connect_fn(
                self._url,
                additional_headers=self._headers,
                compression=None,
                max_size=None,
                max_queue=64,
                open_timeout=self._open_timeout,
            ) as websocket:
                self._ready.set()
                sender = asyncio.create_task(self._sender(websocket))
                receiver = asyncio.create_task(self._receiver(websocket))
                stopper = asyncio.create_task(self._stop.wait())
                done, pending = await asyncio.wait(
                    {sender, receiver, stopper},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for task in done:
                    if task.cancelled():
                        continue
                    exc = task.exception()
                    if exc is not None and not isinstance(exc, ConnectionClosedOK):
                        raise exc
        finally:
            self._ready.set()

    async def _sender(self, websocket: Any) -> None:
        assert self._outbound is not None
        while True:
            item = await self._outbound.get()
            if item is None:
                return
            kind, payload = item
            if kind == "json":
                await websocket.send(json.dumps(payload, separators=(",", ":")))
            else:
                await websocket.send(payload)

    async def _receiver(self, websocket: Any) -> None:
        async for message in websocket:
            self._inbound.put(message)


def _load_connect() -> Callable[..., Any]:
    try:
        import websockets
    except ImportError as exc:
        raise GrokRealtimeError(
            "the Grok Voice adapter needs the optional 'websockets' package; "
            "install it with `pip install -e '.[realtime]'` in the agent directory"
        ) from exc
    return websockets.connect


class PlaybackSink(Protocol):
    """Queued playback used by the session; injectable for tests."""

    def enqueue(self, pcm: bytes) -> None:
        """Queue assistant PCM for playback."""

    def begin_response(self) -> None:
        """Clear barge-in suppression at the start of a new response."""

    def flush(self) -> None:
        """Stop playback and discard queued audio."""

    def close(self) -> None:
        """Drain queued audio and close the sink."""


class _PlaybackPump:
    """Write assistant PCM to a streaming sink from a worker thread.

    Keeping writes off the receive loop means a full player pipe cannot delay
    handling ``input_audio_buffer.speech_started``. Barge-in calls ``flush``,
    which drains the queue and terminates the player; a write already blocked in
    the pipe returns with a broken-pipe error and is ignored.
    """

    def __init__(self, audio: AudioDevice) -> None:
        self._stream = audio.start_playback()
        self._queue: queue.Queue[bytes | None] = queue.Queue(maxsize=_PLAYBACK_QUEUE_CHUNKS)
        self._closed = threading.Event()
        self._suppress = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="grok-realtime-playback",
            daemon=True,
        )
        self._thread.start()

    def enqueue(self, pcm: bytes) -> None:
        if not pcm or self._closed.is_set() or self._suppress.is_set():
            return
        try:
            self._queue.put_nowait(pcm)
        except queue.Full:
            # Drop the oldest chunk rather than block the receive loop. A short
            # gap is preferable to losing barge-in or a tool call.
            with contextlib.suppress(queue.Empty):
                self._queue.get_nowait()
            with contextlib.suppress(queue.Full):
                self._queue.put_nowait(pcm)

    def begin_response(self) -> None:
        self._suppress.clear()

    @property
    def error(self) -> BaseException | None:
        """The playback failure that stopped the pump, if any."""

        return self._error

    def flush(self) -> None:
        self._suppress.set()
        self._drain()
        self._stream.stop()

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        while True:
            try:
                self._queue.put_nowait(None)
                break
            except queue.Full:
                with contextlib.suppress(queue.Empty):
                    self._queue.get_nowait()
        self._thread.join(timeout=20.0)
        try:
            self._stream.close()
        except AudioError as exc:
            logger.warning("streaming playback close failed: %s", exc)
        if self._error is not None:
            logger.warning("streaming playback failed: %s", self._error)

    def _drain(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            if self._suppress.is_set():
                continue
            try:
                self._stream.write(item)
            except AudioError as exc:
                if self._closed.is_set():
                    self._error = exc
                    return
                if self._suppress.is_set():
                    # A barge-in kills the player while a write is in flight.
                    # The next response starts a fresh process.
                    logger.debug("playback write interrupted: %s", exc)
                    continue
                self._error = exc
                logger.warning("streaming playback failed: %s", exc)
                return


class GrokRealtimeSession:
    """Run one outbound call on the Grok Voice speech-to-speech API.

    Args are the same as :class:`~telephone_agent.pipeline.CallSession`, with
    *config* replacing the brain and speech adapters. *connection_factory* and
    *playback_factory* are test seams; production callers leave them unset.
    """

    def __init__(
        self,
        *,
        audio: AudioDevice,
        dialer: Dialer,
        number: str,
        config: RealtimeConfig,
        objective: str = "",
        consultations: ConsultationBroker | None = None,
        max_call_seconds: float = 300.0,
        silence_timeout_s: float = 20.0,
        consult_timeout_s: float = 8.0,
        holding_phrase: str = "One moment please.",
        on_event: Callable[[SessionEvent], None] | None = None,
        stop_event: threading.Event | None = None,
        clock: Callable[[], float] = time.monotonic,
        connection_factory: Callable[[], RealtimeConnection] | None = None,
        playback_factory: Callable[[AudioDevice], PlaybackSink] | None = None,
    ) -> None:
        if config.kind != "grok":
            raise ValueError("GrokRealtimeSession requires realtime.kind = 'grok'")
        if not math.isfinite(max_call_seconds) or max_call_seconds <= 0:
            raise ValueError(f"max_call_seconds must be a positive number (got {max_call_seconds})")
        if not math.isfinite(silence_timeout_s) or silence_timeout_s <= 0:
            raise ValueError(
                f"silence_timeout_s must be a positive number (got {silence_timeout_s})"
            )
        if not math.isfinite(consult_timeout_s) or consult_timeout_s <= 0:
            raise ValueError(
                f"consult_timeout_s must be a positive number (got {consult_timeout_s})"
            )
        if not holding_phrase.strip():
            raise ValueError("holding_phrase must not be empty or whitespace")

        self.audio = audio
        self.dialer = dialer
        self.number = ensure_e164(number)
        self.config = config
        self.objective = objective.strip()
        self._consultations = consultations
        self.max_call_seconds = max_call_seconds
        self.silence_timeout_s = silence_timeout_s
        self._consult_timeout_s = consult_timeout_s
        self._holding_phrase = holding_phrase
        self._on_event = on_event
        self._stop_event = stop_event
        self._clock = clock
        self._api_key = resolve_api_key(config)
        self._connection_factory = connection_factory or self._websocket_factory
        self._playback_factory = playback_factory or _PlaybackPump

        self._state = CallState.IDLE
        self._ran = False
        self._dial_attempted = False
        self._hangup_requested = False
        self._hangup_deadline = 0.0
        self._response_done = False
        self._connection: RealtimeConnection | None = None
        self._playback: PlaybackSink | None = None
        self._last_event_at = 0.0
        self._turns = 0
        self._finalized_items: set[str] = set()
        self._handled_calls: set[str] = set()

    @property
    def state(self) -> CallState:
        """Current lifecycle state."""

        return self._state

    def run(self) -> CallResult:
        """Place the call, run the realtime conversation, and hang up."""

        if self._ran:
            raise RuntimeError("GrokRealtimeSession.run() may only be called once")
        self._ran = True
        if not self._api_key:
            raise GrokRealtimeError(
                "XAI_API_KEY is not set; export it or put it in the service's "
                "EnvironmentFile outside the repository"
            )

        started = self._clock()
        reason = CallEndReason.COMPLETED
        final_state = CallState.IDLE

        try:
            self._set_state(CallState.DIALING)
            logger.info("placing call to %s", mask_number(self.number))
            self._dial_attempted = True
            self.dialer.place_call(self.number)

            self._set_state(CallState.IN_CALL)
            logger.info("call placed; opening Grok Voice realtime session")

            connection = self._connection_factory()
            self._connection = connection
            connection.connect()
            connection.send_json(
                build_session_update(
                    self.config,
                    sample_rate=self.audio.sample_rate,
                    objective=self.objective,
                    has_consult=self._consultations is not None,
                    holding_phrase=self._holding_phrase,
                )
            )
            self._await_session_updated(connection)

            self._playback = self._playback_factory(self.audio)
            self.audio.start()
            self._last_event_at = self._clock()
            deadline = started + self.max_call_seconds
            frames = self.audio.frames()
            try:
                for frame in frames:
                    if self._stop_event is not None and self._stop_event.is_set():
                        reason = CallEndReason.INTERRUPTED
                        break
                    now = self._clock()
                    if now >= deadline:
                        reason = CallEndReason.MAX_DURATION
                        break
                    if connection.closed:
                        logger.error("Grok Voice WebSocket closed during the call")
                        reason = CallEndReason.TURN_FAILED
                        final_state = CallState.FAILED
                        break
                    try:
                        if not self._hangup_requested:
                            connection.send_bytes(frame)
                    except GrokRealtimeError as exc:
                        logger.error("cannot send call audio to Grok Voice: %s", exc)
                        reason = CallEndReason.TURN_FAILED
                        final_state = CallState.FAILED
                        break
                    self._drain_events(connection)
                    if self._hangup_requested and (
                        self._response_done or self._clock() >= self._hangup_deadline
                    ):
                        reason = CallEndReason.COMPLETED
                        break
                    playback_error = getattr(self._playback, "error", None)
                    if playback_error is not None:
                        logger.error("realtime playback failed: %s", playback_error)
                        reason = CallEndReason.TURN_FAILED
                        final_state = CallState.FAILED
                        break
                    if self._clock() - self._last_event_at >= self.silence_timeout_s:
                        logger.info(
                            "no speech or response for %.1f s; ending call",
                            self.silence_timeout_s,
                        )
                        reason = CallEndReason.SILENCE_TIMEOUT
                        break
                else:
                    reason = CallEndReason.AUDIO_ENDED
            finally:
                frames.close()
        except KeyboardInterrupt:
            logger.info("interrupted; ending call")
            reason = CallEndReason.INTERRUPTED
        except GrokRealtimeError as exc:
            logger.error("realtime call failed: %s", exc)
            final_state = CallState.FAILED
            reason = CallEndReason.TURN_FAILED
        except Exception:
            final_state = CallState.FAILED
            raise
        finally:
            self._set_state(CallState.ENDING)
            self._close_playback()
            self._close_connection()
            hangup_ok = self._teardown()
            if not hangup_ok:
                if final_state == CallState.IDLE:
                    final_state = CallState.FAILED
                if reason == CallEndReason.COMPLETED:
                    reason = CallEndReason.HANGUP_FAILED
            self._set_state(final_state)

        duration_s = self._clock() - started
        result = CallResult(
            state=final_state,
            reason=reason,
            duration_s=duration_s,
            turns=self._turns,
        )
        logger.info(
            "call finished: state=%s reason=%s turns=%d duration=%.1f s",
            result.state,
            result.reason,
            result.turns,
            result.duration_s,
        )
        self._emit(CallFinished(result))
        return result

    def _websocket_factory(self) -> RealtimeConnection:
        base_url = self.config.base_url.rstrip("/")
        separator = "&" if "?" in base_url else "?"
        url = f"{base_url}{separator}model={quote(self.config.model, safe='')}"
        return _WebSocketConnection(
            url=url,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "User-Agent": _USER_AGENT,
            },
        )

    def _await_session_updated(self, connection: RealtimeConnection) -> None:
        deadline = time.monotonic() + _SESSION_READY_TIMEOUT_S
        while True:
            if connection.closed:
                raise GrokRealtimeError("Grok Voice closed before the session was ready")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GrokRealtimeError("timed out waiting for session.updated")
            item = connection.recv(timeout=min(0.2, remaining))
            if item is None:
                continue
            if isinstance(item, bytes):
                continue
            event = _event(item)
            if event is None:
                continue
            event_type = event.get("type")
            if event_type == "session.updated":
                logger.debug("Grok Voice session configured")
                return
            if event_type == "error":
                raise GrokRealtimeError(f"Grok Voice rejected the session: {_error_text(event)}")

    def _drain_events(self, connection: RealtimeConnection) -> None:
        while True:
            item = connection.recv(timeout=0.0)
            if item is None:
                return
            self._last_event_at = self._clock()
            if isinstance(item, bytes):
                if self._playback is not None:
                    self._playback.enqueue(item)
                continue
            event = _event(item)
            if event is not None:
                self._handle_event(event)

    def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if not isinstance(event_type, str):
            return

        if event_type == "input_audio_buffer.speech_started":
            logger.debug("caller speech detected; stopping playback")
            if self._playback is not None:
                self._playback.flush()
            return

        if event_type == "response.created":
            self._response_done = False
            if self._playback is not None:
                self._playback.begin_response()
            return

        if event_type == "response.done":
            self._response_done = True
            return

        if event_type in {"response.output_audio.delta", "response.audio.delta"}:
            delta = event.get("delta") or event.get("audio")
            if isinstance(delta, str) and self._playback is not None:
                try:
                    pcm = base64.b64decode(delta, validate=True)
                except ValueError:
                    logger.warning("ignoring malformed base64 audio delta")
                    return
                self._playback.enqueue(pcm)
            return

        if event_type == "conversation.item.input_audio_transcription.completed":
            transcript = event.get("transcript")
            if not isinstance(transcript, str) or not transcript.strip():
                return
            item_id = event.get("item_id")
            if isinstance(item_id, str):
                if item_id in self._finalized_items:
                    return
                self._finalized_items.add(item_id)
            self._turns += 1
            self._emit(Utterance(transcript.strip()))
            return

        if event_type in {
            "response.output_audio_transcript.done",
            "response.audio_transcript.done",
        }:
            transcript = event.get("transcript") or event.get("text")
            if isinstance(transcript, str) and transcript.strip():
                self._emit(AgentReply(transcript.strip()))
            return

        if event_type == "response.function_call_arguments.done":
            self._handle_tool_call(
                name=event.get("name"),
                call_id=event.get("call_id"),
                arguments=event.get("arguments"),
            )
            return

        if event_type == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "function_call":
                self._handle_tool_call(
                    name=item.get("name"),
                    call_id=item.get("call_id"),
                    arguments=item.get("arguments"),
                )
            return

        if event_type == "error":
            logger.warning("Grok Voice error event: %s", _error_text(event))
            return

    def _handle_tool_call(
        self,
        *,
        name: object,
        call_id: object,
        arguments: object,
    ) -> None:
        if not isinstance(name, str) or not name.strip():
            return
        if not isinstance(call_id, str) or not call_id.strip():
            return
        if call_id in self._handled_calls:
            return
        self._handled_calls.add(call_id)
        parsed = _json_object(arguments)

        if name == "end_call":
            reason = parsed.get("reason")
            logger.info("model requested hangup (%s)", reason or "completed")
            self._send_tool_output(
                call_id,
                {"status": "ending", "reason": str(reason or "completed")},
            )
            self._hangup_requested = True
            self._response_done = False
            self._hangup_deadline = self._clock() + 2.0
            return

        if name == "consult":
            self._consult(call_id, parsed)
            return

        logger.warning("ignoring unknown model tool %r", name)
        self._send_tool_output(call_id, {"error": f"unknown tool: {name}"})
        self._request_continuation()

    def _consult(self, call_id: str, arguments: dict[str, Any]) -> None:
        question = arguments.get("question")
        if not isinstance(question, str) or not question.strip():
            self._send_tool_output(call_id, {"error": "consult requires a question"})
            self._request_continuation()
            return
        if self._consultations is None:
            self._send_tool_output(
                call_id,
                {"error": "the requester is not reachable on this call"},
            )
            self._request_continuation()
            return

        try:
            outcome = self._consultations.ask(
                question.strip(),
                self._consult_timeout_s,
                on_open=lambda consultation: self._emit(ConsultationStarted(consultation)),
            )
        except (RuntimeError, ValueError) as exc:
            logger.warning("consultation failed: %s", exc)
            self._send_tool_output(call_id, {"error": "the requester could not be reached"})
            self._request_continuation()
            return

        self._emit(ConsultationFinished(outcome))
        if outcome.answer is None:
            self._send_tool_output(
                call_id,
                {"error": "no answer from the requester; apologize and offer a callback"},
            )
        else:
            self._send_tool_output(call_id, {"answer": outcome.answer})
        self._request_continuation()
        self._last_event_at = self._clock()

    def _send_tool_output(self, call_id: str, output: dict[str, Any]) -> None:
        connection = self._connection
        if connection is None:
            return
        connection.send_json(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(output, separators=(",", ":")),
                },
            }
        )

    def _request_continuation(self) -> None:
        connection = self._connection
        if connection is None:
            return
        connection.send_json({"type": "response.create"})

    def _close_playback(self) -> None:
        playback = self._playback
        self._playback = None
        if playback is None:
            return
        try:
            playback.close()
        except Exception:
            logger.warning("failed to close realtime playback", exc_info=True)

    def _close_connection(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            connection.close()
        except Exception:
            logger.warning("failed to close the Grok Voice connection", exc_info=True)

    def _teardown(self) -> bool:
        try:
            self.audio.stop()
        except Exception:
            logger.exception("failed to stop the audio device")

        hangup_ok = True
        if self._dial_attempted:
            try:
                self.dialer.hangup()
                logger.info("hangup requested")
            except Exception as exc:
                logger.warning("hangup request failed: %s", exc)
                hangup_ok = False
        return hangup_ok

    def _set_state(self, state: CallState) -> None:
        if state == self._state:
            return
        logger.debug("state %s -> %s", self._state, state)
        self._state = state
        self._emit(StateChanged(state))

    def _emit(self, event: SessionEvent) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception:
            logger.warning("session event observer failed; continuing", exc_info=True)


def _event(raw: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("ignoring malformed JSON from Grok Voice")
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _json_object(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _error_text(event: dict[str, Any]) -> str:
    error = event.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
    if isinstance(error, str) and error:
        return error
    message = event.get("message")
    return message if isinstance(message, str) and message else "unknown error"
