"""Audio backends for the USB gadget and Bluetooth HFP headset paths.

Two transports can put the agent on both directions of a cellular call:

* **USB Audio Class gadget** (:class:`GadgetAudio`): the host enumerates as a
  wired headset on Phone A. ALSA's direction is inverted relative to a normal
  sound card: recording from the gadget card receives the remote party, and
  playing to the card sends audio to Phone A's uplink.
* **Bluetooth HFP** (:class:`PipeWireAudio`): the host advertises the Hands-Free
  Profile headset role (HF) and Phone A is the audio gateway (AG). PipeWire
  exposes the call as a SCO source (downlink) and a SCO sink (uplink) while the
  call is active. The node names carry the phone's Bluetooth address and a
  per-connection suffix, so they are discovered with ``pw-dump`` rather than
  hard-coded.

``arecord``/``aplay`` and ``pw-record``/``pw-play`` are invoked through
subprocesses. The USB gadget runs at 48 kHz stereo; the default processing rate
is 16 kHz mono, which is enough for the cellular codec and cheaper on a small
host. ``plughw`` and PipeWire perform the format conversion, so the values in
:class:`~telephone_agent.config.AudioConfig` are the processing format rather
than the wire format. ``play()`` is the one-shot path for the text pipeline;
``start_playback()`` returns a reusable stream for the realtime adapter and can
be interrupted for barge-in.

No audio is written to disk. Capture diagnostics are kept in an anonymous
temporary file only so that a failed start can be reported.
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import IO, Protocol

from .vad import dbfs_from_rms, pcm_to_samples, rms

__all__ = [
    "AudioDevice",
    "AudioError",
    "AudioStats",
    "CaptureDevice",
    "GadgetAudio",
    "PipeWireAudio",
    "PlaybackStream",
]

logger = logging.getLogger(__name__)

_CARD_LINE = re.compile(
    r"^card\s+(?P<card_index>\d+):\s+(?P<card_id>\S+)\s+"
    r"\[(?P<card_name>.*?)\],\s+device\s+(?P<device_index>\d+):\s+"
    r"(?P<device_name>.*?)\s+\[(?P<device_id>.*?)\]"
)
_BLUETOOTH_ADDRESS = re.compile(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")
_STOP_TIMEOUT_S = 2.0
_PLAYBACK_DRAIN_TIMEOUT_S = 10.0
_PW_DUMP_TIMEOUT_S = 10.0
# Stop pw-cat from silently falling back to the default source/sink if the SCO
# node disappears (call ended) or is not created. Without these properties,
# WirePlumber may relink the stream and the agent would record the local
# microphone or play TTS through the local speakers.
_STREAM_PROPERTIES = '{"node.dont-fallback": true, "node.dont-reconnect": true}'


class AudioError(RuntimeError):
    """Raised when an audio command cannot be run or exits with an error."""


@dataclass(frozen=True, slots=True)
class CaptureDevice:
    """One capture device reported by ``arecord -l``."""

    card_index: int
    card_id: str
    card_name: str
    device_index: int
    device_name: str

    @property
    def alsa_device(self) -> str:
        """The device string accepted by ``-D`` and ALSA's ``plughw`` plugin."""

        return f"plughw:CARD={self.card_id},DEV={self.device_index}"

    def __str__(self) -> str:
        return (
            f"card {self.card_index}: {self.card_id} [{self.card_name}], "
            f"device {self.device_index}: {self.device_name}"
        )


@dataclass(frozen=True, slots=True)
class AudioStats:
    """Result of a capture self-test."""

    frames: int
    duration_s: float
    rms: float
    dbfs: float
    peak: int


class AudioDevice(Protocol):
    """Structural interface used by the call sessions.

    ``frames()`` and :meth:`play` are enough for the utterance-at-a-time
    pipeline. A full-duplex session uses :meth:`start_playback` so it can write
    audio chunks as they arrive and stop them on barge-in.
    """

    sample_rate: int
    channels: int
    frame_ms: int
    frame_bytes: int

    @property
    def description(self) -> str:
        """Human-readable device description for logs and ``doctor``."""

    def start(self) -> None:
        """Start capture if it is not already running."""

    def stop(self) -> None:
        """Stop capture and release its pipes."""

    def frames(self) -> Iterator[bytes]:
        """Yield fixed-size 16-bit PCM frames from the call downlink."""

    def play(self, pcm: bytes) -> None:
        """Play 16-bit PCM into the call uplink."""

    def start_playback(self) -> PlaybackStream:
        """Open a streaming sink for 16-bit PCM playback."""

    def selftest(self, duration_s: float = 2.0) -> AudioStats:
        """Record for approximately *duration_s* and report signal statistics."""


class PlaybackStream(Protocol):
    """A write-ahead playback sink with an interrupt path.

    ``write`` may block while the player consumes the pipe. ``stop`` must be
    safe to call from another thread; it terminates the current player so a
    blocked ``write`` returns. After ``stop``, a later ``write`` starts a fresh
    player, which is what barge-in needs. ``close`` drains and releases the sink.
    """

    def write(self, pcm: bytes) -> None:
        """Write one chunk of 16-bit PCM."""

    def stop(self) -> None:
        """Stop the current player immediately; the sink stays reusable."""

    def close(self) -> None:
        """Close the sink, allowing queued audio to drain."""

    def __enter__(self) -> PlaybackStream:
        """Return this stream."""

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        """Close the stream."""


class _ProcessAudio:
    """Shared subprocess lifecycle for the ALSA and PipeWire backends.

    Subclasses provide the capture/playback commands and the user-facing names
    used in error messages. The capture process is single-instance. ``play``
    starts one player process per utterance; ``start_playback`` keeps one player
    open for streaming and can stop it on barge-in.
    """

    _capture_tool = "capture tool"
    _playback_tool = "playback tool"
    _tool_hint = "check that the audio tools are installed"
    _capture_exit_hint = "check the configured audio device"
    _no_audio_hint = "check the audio device and the call routing"

    def __init__(self, *, sample_rate: int, channels: int, frame_ms: int) -> None:
        if sample_rate <= 0:
            raise ValueError(f"sample_rate must be > 0 (got {sample_rate})")
        if channels <= 0:
            raise ValueError(f"channels must be > 0 (got {channels})")
        if frame_ms <= 0:
            raise ValueError(f"frame_ms must be > 0 (got {frame_ms})")
        sample_count = sample_rate * frame_ms
        if sample_count % 1_000 != 0:
            raise ValueError(
                f"frame_ms={frame_ms} is not a whole number of samples at {sample_rate} Hz"
            )
        frame_samples = sample_count // 1_000
        if frame_samples <= 0:
            raise ValueError("frame_ms is too short for the configured sample rate")

        self._sample_rate = sample_rate
        self._channels = channels
        self._frame_ms = frame_ms
        self._frame_bytes = frame_samples * channels * 2
        self._capture: subprocess.Popen[bytes] | None = None
        self._capture_stderr: IO[bytes] | None = None
        self._last_capture_stderr = ""

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def channels(self) -> int:
        return self._channels

    @property
    def frame_ms(self) -> int:
        return self._frame_ms

    @property
    def frame_bytes(self) -> int:
        return self._frame_bytes

    @property
    def description(self) -> str:
        raise NotImplementedError

    def start(self) -> None:
        """Start the capture process if it is not already running."""

        if self._capture is not None:
            if self._capture.poll() is None:
                return
            self._stop_capture()

        command = self._capture_command()
        logger.debug("starting capture: %s", " ".join(command))
        self._last_capture_stderr = ""
        # The handle must outlive this method; it is closed in _stop_capture().
        self._capture_stderr = tempfile.TemporaryFile()  # noqa: SIM115
        try:
            self._capture = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=self._capture_stderr,
                bufsize=0,
                text=False,
                close_fds=True,
            )
        except FileNotFoundError as exc:
            self._close_capture_stderr()
            raise AudioError(f"{self._capture_tool} was not found; {self._tool_hint}") from exc
        except OSError as exc:
            self._close_capture_stderr()
            raise AudioError(f"cannot start {self._capture_tool}: {exc}") from exc

    def stop(self) -> None:
        """Stop the capture process and release its pipes. Safe to call twice."""

        self._stop_capture()

    def frames(self) -> Iterator[bytes]:
        """Yield fixed-size 16-bit PCM frames from the call downlink.

        The generator starts capture if necessary and stops it when exhausted,
        when the caller closes it, or when an exception propagates. A partial
        frame at the end of the stream is discarded. A non-zero capture exit
        status raises :class:`AudioError` with the captured diagnostic output.
        """

        self.start()
        process = self._capture
        if process is None or process.stdout is None:
            raise AudioError("capture process did not provide a stdout pipe")

        stream = process.stdout
        try:
            while True:
                frame = _read_exact(stream, self.frame_bytes)
                if frame is None:
                    break
                yield frame
        finally:
            self._stop_capture()

        if process.returncode not in (0, None):
            detail = f": {self._last_capture_stderr}" if self._last_capture_stderr else ""
            raise AudioError(
                f"{self._capture_tool} exited with status {process.returncode}{detail}; "
                f"{self._capture_exit_hint}"
            )

    def play(self, pcm: bytes) -> None:
        """Play raw 16-bit PCM through the call uplink path.

        Playback blocks until the player exits. A full-duplex caller should use
        :meth:`start_playback` instead.
        """

        if not pcm:
            return
        frame_size = self.channels * 2
        if len(pcm) % frame_size:
            raise AudioError(f"PCM length {len(pcm)} is not a multiple of {frame_size} bytes")

        command = self._playback_command()
        logger.debug("starting playback: %s", " ".join(command))
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0,
                text=False,
            )
        except FileNotFoundError as exc:
            raise AudioError(f"{self._playback_tool} was not found; {self._tool_hint}") from exc
        except OSError as exc:
            raise AudioError(f"cannot start {self._playback_tool}: {exc}") from exc

        duration_s = len(pcm) / (self.sample_rate * frame_size)
        timeout_s = duration_s + 5.0
        try:
            _, stderr = process.communicate(pcm, timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.communicate()
            raise AudioError(
                f"{self._playback_tool} did not finish within {timeout_s:.1f} s"
            ) from exc
        except KeyboardInterrupt:
            process.kill()
            with contextlib.suppress(OSError):
                process.communicate()
            raise
        except OSError as exc:
            process.kill()
            with contextlib.suppress(OSError):
                process.communicate()
            raise AudioError(f"cannot write PCM to {self._playback_tool}: {exc}") from exc

        if process.returncode != 0:
            detail = (stderr or b"").decode("utf-8", "replace").strip()
            message = f"{self._playback_tool} exited with status {process.returncode}"
            if detail:
                message = f"{message}: {detail}"
            raise AudioError(message)

    def start_playback(self) -> PlaybackStream:
        """Return a streaming playback sink for this device."""

        return _SubprocessPlayback(self)

    def selftest(self, duration_s: float = 2.0) -> AudioStats:
        """Record for approximately *duration_s* and report signal statistics.

        This records the downlink path only. It does not play anything and does
        not verify that Android routes call audio to the headset.
        """

        if duration_s <= 0:
            raise ValueError(f"duration_s must be > 0 (got {duration_s})")
        target_frames = max(1, round(duration_s * 1_000 / self.frame_ms))

        captured = bytearray()
        stream = self.frames()
        try:
            for _ in range(target_frames):
                try:
                    captured.extend(next(stream))
                except StopIteration:
                    break
        finally:
            stream.close()

        if not captured:
            raise AudioError(f"captured no audio; {self._no_audio_hint}")

        payload = bytes(captured)
        samples = pcm_to_samples(payload)
        level = rms(payload)
        peak = max((abs(sample) for sample in samples), default=0)
        return AudioStats(
            frames=len(payload) // self.frame_bytes,
            duration_s=len(samples) / (self.sample_rate * self.channels),
            rms=level,
            dbfs=dbfs_from_rms(level),
            peak=peak,
        )

    def _capture_command(self) -> list[str]:
        raise NotImplementedError

    def _playback_command(self) -> list[str]:
        raise NotImplementedError

    def _stop_capture(self) -> None:
        process = self._capture
        self._capture = None
        if process is not None:
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()
                try:
                    process.wait(timeout=_STOP_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    try:
                        process.wait(timeout=_STOP_TIMEOUT_S)
                    except subprocess.TimeoutExpired:
                        logger.warning("%s did not exit after SIGKILL", self._capture_tool)
            else:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=0)
            if process.stdout is not None:
                with contextlib.suppress(OSError):
                    process.stdout.close()
        self._read_capture_stderr()

    def _read_capture_stderr(self) -> None:
        handle = self._capture_stderr
        self._capture_stderr = None
        if handle is None:
            return
        data = b""
        try:
            handle.flush()
            handle.seek(0)
            data = handle.read()
        except OSError:
            data = b""
        finally:
            self._close_capture_stderr()
        self._last_capture_stderr = data.decode("utf-8", "replace").strip()

    def _close_capture_stderr(self) -> None:
        handle = self._capture_stderr
        self._capture_stderr = None
        if handle is not None:
            with contextlib.suppress(OSError):
                handle.close()


class _SubprocessPlayback:
    """Streaming playback for one ``_ProcessAudio`` backend.

    A player process is started lazily on the first ``write``. ``stop`` kills
    that process so a blocked write returns immediately; a later write starts a
    fresh player. ``stderr`` is discarded because the streaming path is
    long-lived and reading it concurrently with writes would need another
    thread; the exit status is logged instead.
    """

    def __init__(self, audio: _ProcessAudio) -> None:
        self._audio = audio
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._closed = False

    def write(self, pcm: bytes) -> None:
        if not pcm:
            return
        frame_size = self._audio.channels * 2
        if len(pcm) % frame_size:
            raise AudioError(f"PCM length {len(pcm)} is not a multiple of {frame_size} bytes")

        process = self._ensure_process()
        stdin = process.stdin
        if stdin is None:  # pragma: no cover - Popen was called with a pipe
            raise AudioError("playback process did not provide a stdin pipe")
        try:
            stdin.write(pcm)
            stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            with self._lock:
                if self._process is process:
                    self._process = None
            raise AudioError(
                f"cannot write PCM to {self._audio._playback_tool}; the player exited"
            ) from exc

    def stop(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
        self._terminate(process)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            process = self._process
            self._process = None

        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        try:
            process.wait(timeout=_PLAYBACK_DRAIN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            self._terminate(process)
            return
        if process.returncode not in (0, None):
            logger.warning(
                "%s exited with status %s during streaming playback",
                self._audio._playback_tool,
                process.returncode,
            )

    def __enter__(self) -> _SubprocessPlayback:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _ensure_process(self) -> subprocess.Popen[bytes]:
        with self._lock:
            if self._closed:
                raise AudioError("playback stream is closed")
            process = self._process
            if process is not None:
                if process.poll() is None:
                    return process
                self._process = None
                raise AudioError(
                    f"{self._audio._playback_tool} exited with status {process.returncode}"
                )

            command = self._audio._playback_command()
            logger.debug("starting streaming playback: %s", " ".join(command))
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                    text=False,
                )
            except FileNotFoundError as exc:
                raise AudioError(
                    f"{self._audio._playback_tool} was not found; {self._audio._tool_hint}"
                ) from exc
            except OSError as exc:
                raise AudioError(f"cannot start {self._audio._playback_tool}: {exc}") from exc
            self._process = process
            return process

    def _terminate(self, process: subprocess.Popen[bytes] | None) -> None:
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except OSError:
            pass
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                process.wait(timeout=_STOP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                try:
                    process.wait(timeout=_STOP_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    logger.warning("%s did not exit after SIGKILL", self._audio._playback_tool)


class GadgetAudio(_ProcessAudio):
    """Capture and playback through the USB gadget's ALSA card.

    A single capture process is used. ``frames()`` starts it on demand and stops
    it when the generator is closed. Playback is one ``aplay`` process per
    utterance for ``play()``, or a reusable streaming process for
    ``start_playback()``. Do not call :meth:`start` or :meth:`stop` from another
    thread while ``frames()`` is running.
    """

    _capture_tool = "arecord"
    _playback_tool = "aplay"
    _tool_hint = "install ALSA utilities with `sudo apt install alsa-utils`"
    _capture_exit_hint = "run `arecord -l` and check the configured card"
    _no_audio_hint = (
        "check the card id with `arecord -l` and verify that the USB gadget is enumerated"
    )

    def __init__(
        self,
        card: str,
        device: int = 0,
        sample_rate: int = 16_000,
        channels: int = 1,
        frame_ms: int = 20,
        *,
        arecord: str = "arecord",
        aplay: str = "aplay",
    ) -> None:
        if not card:
            raise ValueError("audio card must not be empty")
        if device < 0:
            raise ValueError(f"audio device must be >= 0 (got {device})")
        super().__init__(sample_rate=sample_rate, channels=channels, frame_ms=frame_ms)
        self._card = card
        self._device = device
        self._arecord = arecord
        self._aplay = aplay

    @property
    def card(self) -> str:
        return self._card

    @property
    def device(self) -> int:
        return self._device

    @property
    def alsa_device(self) -> str:
        """The ALSA device string used for capture and playback."""

        return f"plughw:CARD={self._card},DEV={self._device}"

    @property
    def description(self) -> str:
        return self.alsa_device

    def _capture_command(self) -> list[str]:
        return [
            self._arecord,
            "-q",
            "-D",
            self.alsa_device,
            "-f",
            "S16_LE",
            "-r",
            str(self.sample_rate),
            "-c",
            str(self.channels),
            "-t",
            "raw",
        ]

    def _playback_command(self) -> list[str]:
        return [
            self._aplay,
            "-q",
            "-D",
            self.alsa_device,
            "-f",
            "S16_LE",
            "-r",
            str(self.sample_rate),
            "-c",
            str(self.channels),
            "-t",
            "raw",
        ]

    @staticmethod
    def list_cards(arecord: str = "arecord") -> list[CaptureDevice]:
        """Parse ``arecord -l`` into capture devices.

        Raises:
            AudioError: If ``arecord`` is missing, times out, or exits non-zero.
        """

        env = {**os.environ, "LC_ALL": "C"}
        try:
            result = subprocess.run(
                [arecord, "-l"],
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
                env=env,
            )
        except FileNotFoundError as exc:
            raise AudioError(
                "arecord was not found; install ALSA utilities with `sudo apt install alsa-utils`"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AudioError("`arecord -l` timed out") from exc
        except OSError as exc:
            raise AudioError(f"cannot run arecord: {exc}") from exc

        if result.returncode != 0:
            detail = result.stderr.strip()
            message = f"`arecord -l` exited with status {result.returncode}"
            if detail:
                message = f"{message}: {detail}"
            raise AudioError(message)

        devices: list[CaptureDevice] = []
        for line in result.stdout.splitlines():
            match = _CARD_LINE.match(line)
            if match is None:
                continue
            devices.append(
                CaptureDevice(
                    card_index=int(match.group("card_index")),
                    card_id=match.group("card_id"),
                    card_name=match.group("card_name").strip(),
                    device_index=int(match.group("device_index")),
                    device_name=match.group("device_name").strip(),
                )
            )
        return devices


class PipeWireAudio(_ProcessAudio):
    """Capture and playback through a Bluetooth HFP SCO link.

    Phone A is the HFP audio gateway (AG); the host is the headset (HF). While a
    call is active, PipeWire creates a SCO source named ``bluez_input.<mac>.<id>``
    for the downlink and a SCO sink named ``bluez_output.<mac>.<id>`` for the
    uplink. The suffix is a SPA object id, so the nodes are discovered from
    ``pw-dump`` by Bluetooth address and ``factory.name``.

    The SCO nodes do not exist while the phone is idle. ``start()`` waits up to
    ``node_timeout_s`` for a call to open them and raises :class:`AudioError`
    with the Bluetooth troubleshooting hint if it does not.
    """

    _capture_tool = "pw-record"
    _playback_tool = "pw-play"
    _tool_hint = "install PipeWire's command-line tools (package pipewire-bin)"

    def __init__(
        self,
        bluetooth_address: str,
        sample_rate: int = 16_000,
        channels: int = 1,
        frame_ms: int = 20,
        *,
        node_timeout_s: float = 30.0,
        poll_interval_s: float = 0.5,
        pw_record: str = "pw-record",
        pw_play: str = "pw-play",
        pw_dump: str = "pw-dump",
    ) -> None:
        address = normalize_bluetooth_address(bluetooth_address)
        if not math.isfinite(node_timeout_s) or node_timeout_s <= 0:
            raise ValueError(f"node_timeout_s must be a positive number (got {node_timeout_s})")
        if not math.isfinite(poll_interval_s) or poll_interval_s <= 0:
            raise ValueError(f"poll_interval_s must be a positive number (got {poll_interval_s})")
        super().__init__(sample_rate=sample_rate, channels=channels, frame_ms=frame_ms)
        self._bluetooth_address = address
        self._node_timeout_s = node_timeout_s
        self._poll_interval_s = poll_interval_s
        self._pw_record = pw_record
        self._pw_play = pw_play
        self._pw_dump = pw_dump
        self._capture_node: str | None = None
        self._playback_node: str | None = None
        # Instance hints include the address, which the class attributes cannot.
        self._capture_exit_hint = (
            "check that a call is active and the SCO nodes exist "
            f"(`scripts/bluetooth-check.sh {self._bluetooth_address}`)"
        )
        self._no_audio_hint = (
            "place a call and verify the Bluetooth HFP SCO nodes with "
            f"`scripts/bluetooth-check.sh {self._bluetooth_address}`"
        )

    @property
    def bluetooth_address(self) -> str:
        return self._bluetooth_address

    @property
    def description(self) -> str:
        return f"Bluetooth HFP {self._bluetooth_address} (PipeWire SCO)"

    @property
    def capture_node(self) -> str | None:
        """Discovered SCO source node, or ``None`` before the first call."""

        return self._capture_node

    @property
    def playback_node(self) -> str | None:
        """Discovered SCO sink node, or ``None`` before the first call."""

        return self._playback_node

    def start(self) -> None:
        """Rediscover the call-scoped SCO nodes, then start capture.

        The nodes and their ``.<id>`` suffixes are created per call, so a cached
        pair is not reused after a call ends. If capture is already running,
        return without touching the nodes.
        """

        if self._capture is not None and self._capture.poll() is None:
            return
        self._capture_node = None
        self._playback_node = None
        self._discover_nodes()
        super().start()

    def _discover_nodes(self) -> None:
        deadline = time.monotonic() + self._node_timeout_s
        last_error: AudioError | None = None
        while True:
            try:
                capture, playback = _find_sco_nodes(self._bluetooth_address, self._pw_dump)
            except AudioError as exc:
                # A missing binary or a hung PipeWire will not recover by
                # waiting; fail immediately. Other pw-dump failures can be
                # transient while a call is being set up, so retry until the
                # node deadline.
                if "was not found" in str(exc) or "timed out" in str(exc):
                    raise
                last_error = exc
                capture, playback = None, None
            if capture is not None and playback is not None:
                self._capture_node = capture
                self._playback_node = playback
                logger.debug(
                    "Bluetooth SCO nodes for %s: capture=%s playback=%s",
                    self._bluetooth_address,
                    capture,
                    playback,
                )
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(self._poll_interval_s)

        detail = f"; last error: {last_error}" if last_error is not None else ""
        raise AudioError(
            f"Bluetooth HFP SCO nodes for {self._bluetooth_address} did not appear within "
            f"{self._node_timeout_s:g} s; place a call and check that the phone is connected "
            "with 'Calls and audio' enabled "
            f"(see scripts/bluetooth-check.sh {self._bluetooth_address})"
            f"{detail}"
        )

    def _capture_command(self) -> list[str]:
        if self._capture_node is None:
            raise AudioError("Bluetooth capture node is not discovered; place a call first")
        return [
            self._pw_record,
            "--raw",
            "--format",
            "s16",
            "--rate",
            str(self.sample_rate),
            "--channels",
            str(self.channels),
            "--properties",
            _STREAM_PROPERTIES,
            "--target",
            self._capture_node,
            "-",
        ]

    def _playback_command(self) -> list[str]:
        if self._playback_node is None:
            raise AudioError("Bluetooth playback node is not discovered; place a call first")
        return [
            self._pw_play,
            "--raw",
            "--format",
            "s16",
            "--rate",
            str(self.sample_rate),
            "--channels",
            str(self.channels),
            "--properties",
            _STREAM_PROPERTIES,
            "--target",
            self._playback_node,
            "-",
        ]

    @classmethod
    def find_sco_nodes(
        cls,
        bluetooth_address: str,
        *,
        pw_dump: str = "pw-dump",
    ) -> tuple[str | None, str | None]:
        """Return the current SCO ``(capture, playback)`` node names.

        Either element is ``None`` when the phone is idle or the node is not
        present. This is used by ``doctor`` and by the setup/check scripts.
        """

        return _find_sco_nodes(normalize_bluetooth_address(bluetooth_address), pw_dump)


def normalize_bluetooth_address(address: str) -> str:
    """Validate and upper-case a Bluetooth MAC address.

    Raises:
        ValueError: If *address* is not six colon-separated hex octets.
    """

    if not isinstance(address, str) or not _BLUETOOTH_ADDRESS.fullmatch(address.strip()):
        raise ValueError(f"Bluetooth address must look like AA:BB:CC:DD:EE:FF (got {address!r})")
    return address.strip().upper()


def _find_sco_nodes(
    bluetooth_address: str,
    pw_dump: str = "pw-dump",
) -> tuple[str | None, str | None]:
    """Query ``pw-dump`` and return the HFP SCO node names for *bluetooth_address*.

    Raises:
        AudioError: If ``pw-dump`` is missing, times out, exits non-zero, or
            returns malformed JSON.
    """

    env = {**os.environ, "LC_ALL": "C"}
    try:
        result = subprocess.run(
            [pw_dump],
            capture_output=True,
            text=True,
            timeout=_PW_DUMP_TIMEOUT_S,
            check=False,
            env=env,
        )
    except FileNotFoundError as exc:
        raise AudioError(
            "pw-dump was not found; install PipeWire's command-line tools (package pipewire-bin)"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioError("`pw-dump` timed out; is PipeWire running?") from exc
    except OSError as exc:
        raise AudioError(f"cannot run `pw-dump`: {exc}") from exc

    if result.returncode != 0:
        detail = result.stderr.strip()
        message = f"`pw-dump` exited with status {result.returncode}"
        if detail:
            message = f"{message}: {detail}"
        raise AudioError(message)
    if not result.stdout.strip():
        raise AudioError("`pw-dump` returned no data; is PipeWire running?")

    try:
        objects = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AudioError(f"`pw-dump` returned invalid JSON: {exc}") from exc
    return sco_nodes_from_dump(objects, bluetooth_address)


def sco_nodes_from_dump(
    objects: object,
    bluetooth_address: str,
) -> tuple[str | None, str | None]:
    """Extract HFP SCO node names from a parsed ``pw-dump`` payload.

    This pure function is exposed for tests and tooling. It matches the phone
    address and PipeWire's SCO factories, not the volatile ``.<id>`` suffix.
    """

    target = bluetooth_address.upper()
    capture: str | None = None
    playback: str | None = None
    if not isinstance(objects, list):
        raise AudioError("`pw-dump` payload is not a list")
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        info = obj.get("info")
        if not isinstance(info, dict):
            continue
        props = info.get("props")
        if not isinstance(props, dict):
            continue
        address = props.get("api.bluez5.address")
        if not isinstance(address, str) or address.upper() != target:
            continue
        name = props.get("node.name")
        factory = props.get("factory.name")
        if not isinstance(name, str):
            continue
        if (
            capture is None
            and factory == "api.bluez5.sco.source"
            and name.startswith("bluez_input.")
        ):
            capture = name
        elif (
            playback is None
            and factory == "api.bluez5.sco.sink"
            and name.startswith("bluez_output.")
        ):
            playback = name
    return capture, playback


def _read_exact(stream: IO[bytes], count: int) -> bytes | None:
    buffer = bytearray()
    while len(buffer) < count:
        chunk = stream.read(count - len(buffer))
        if not chunk:
            break
        buffer.extend(chunk)
    if len(buffer) < count:
        if buffer:
            logger.debug("discarding %d trailing bytes from capture", len(buffer))
        return None
    return bytes(buffer)
