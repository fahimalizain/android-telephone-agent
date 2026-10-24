"""ALSA audio for the USB Audio Class gadget.

The gadget's ALSA direction is inverted relative to a normal sound card: the
remote party is heard by recording from the gadget card, and audio written to
the gadget card is sent to the phone's uplink. This module keeps that detail out
of the call pipeline.

``arecord`` and ``aplay`` are invoked through the ``plughw`` plugin, which
performs format conversion and resampling. The gadget runs at 48 kHz stereo;
the default processing rate here is 16 kHz mono, which is sufficient for the
cellular codec and cheaper on a Pi Zero 2 W. ``plughw`` handles the conversion,
so the values in :class:`~telephone_agent.config.AudioConfig` are the processing
format rather than the USB descriptor format.

No audio is written to disk. Capture diagnostics from ``arecord`` are kept in an
anonymous temporary file only so that a failed start can be reported.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from typing import IO

from .vad import dbfs_from_rms, pcm_to_samples, rms

__all__ = ["AudioError", "AudioStats", "CaptureDevice", "GadgetAudio"]

logger = logging.getLogger(__name__)

_CARD_LINE = re.compile(
    r"^card\s+(?P<card_index>\d+):\s+(?P<card_id>\S+)\s+"
    r"\[(?P<card_name>.*?)\],\s+device\s+(?P<device_index>\d+):\s+"
    r"(?P<device_name>.*?)\s+\[(?P<device_id>.*?)\]"
)
_STOP_TIMEOUT_S = 2.0


class AudioError(RuntimeError):
    """Raised when arecord or aplay cannot be run or exits with an error."""


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


class GadgetAudio:
    """Capture and playback through the gadget's ALSA card.

    A single capture process is used. ``frames()`` starts it on demand and stops
    it when the generator is closed. Playback is one ``aplay`` process per
    utterance; that keeps lifecycle handling simple and costs a short process
    start per reply. Do not call :meth:`start` or :meth:`stop` from another
    thread while ``frames()`` is running.
    """

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

        self._card = card
        self._device = device
        self._sample_rate = sample_rate
        self._channels = channels
        self._frame_ms = frame_ms
        self._frame_bytes = frame_samples * channels * 2
        self._arecord = arecord
        self._aplay = aplay
        self._capture: subprocess.Popen[bytes] | None = None
        self._capture_stderr: IO[bytes] | None = None
        self._last_capture_stderr = ""

    @property
    def card(self) -> str:
        return self._card

    @property
    def device(self) -> int:
        return self._device

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
    def alsa_device(self) -> str:
        """The ALSA device string used for capture and playback."""

        return f"plughw:CARD={self._card},DEV={self._device}"

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
            raise AudioError(
                "arecord was not found; install ALSA utilities with `sudo apt install alsa-utils`"
            ) from exc
        except OSError as exc:
            self._close_capture_stderr()
            raise AudioError(f"cannot start arecord: {exc}") from exc

    def stop(self) -> None:
        """Stop the capture process and release its pipes. Safe to call twice."""

        self._stop_capture()

    def frames(self) -> Iterator[bytes]:
        """Yield fixed-size 16-bit PCM frames from the gadget microphone.

        The generator starts capture if necessary and stops it when exhausted,
        when the caller closes it, or when an exception propagates. A partial
        frame at the end of the stream is discarded. A non-zero ``arecord`` exit
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
                f"arecord exited with status {process.returncode}{detail}; "
                "run `arecord -l` and check the configured card"
            )

    def play(self, pcm: bytes) -> None:
        """Play raw 16-bit PCM through the gadget speaker path.

        Playback blocks until ``aplay`` exits. There is no barge-in in v0; see
        :mod:`telephone_agent.pipeline`.
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
            raise AudioError(
                "aplay was not found; install ALSA utilities with `sudo apt install alsa-utils`"
            ) from exc
        except OSError as exc:
            raise AudioError(f"cannot start aplay: {exc}") from exc

        duration_s = len(pcm) / (self.sample_rate * frame_size)
        timeout_s = duration_s + 5.0
        try:
            _, stderr = process.communicate(pcm, timeout=timeout_s)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.communicate()
            raise AudioError(f"aplay did not finish within {timeout_s:.1f} s") from exc
        except KeyboardInterrupt:
            process.kill()
            with contextlib.suppress(OSError):
                process.communicate()
            raise
        except OSError as exc:
            process.kill()
            with contextlib.suppress(OSError):
                process.communicate()
            raise AudioError(f"cannot write PCM to aplay: {exc}") from exc

        if process.returncode != 0:
            detail = (stderr or b"").decode("utf-8", "replace").strip()
            message = f"aplay exited with status {process.returncode}"
            if detail:
                message = f"{message}: {detail}"
            raise AudioError(message)

    def selftest(self, duration_s: float = 2.0) -> AudioStats:
        """Record for approximately *duration_s* and report signal statistics.

        This records the gadget capture path only. It does not play anything and
        does not verify that Android routes call audio to the headset.
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
            raise AudioError(
                "captured no audio; check the card id with `arecord -l` and verify "
                "that the USB gadget is enumerated"
            )

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

    def _stop_capture(self) -> None:
        process = self._capture
        self._capture = None
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=_STOP_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=_STOP_TIMEOUT_S)
                    except subprocess.TimeoutExpired:
                        logger.warning("arecord did not exit after SIGKILL")
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
