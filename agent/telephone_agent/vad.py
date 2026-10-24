"""Energy-based voice activity detection and utterance endpointing.

All frames are 16-bit little-endian mono PCM. The helpers deliberately use the
standard library's :mod:`array` module instead of :mod:`audioop`, which was
removed in Python 3.13.

The endpointer is intentionally simple: it compares frame RMS against a dBFS
threshold and uses silence duration to decide when a turn has ended. It is not a
speech model, so a loud fan can keep a turn open. Tune ``vad_threshold`` on the
device and prefer a real VAD model if the acoustic environment is difficult.
"""

from __future__ import annotations

import math
import sys
from array import array
from collections import deque

__all__ = ["Endpointer", "dbfs_from_rms", "is_speech", "pcm_to_samples", "rms"]

FULL_SCALE = 32_768.0
"""Absolute amplitude of a full-scale signed 16-bit sample."""


def pcm_to_samples(frame: bytes) -> array:
    """Convert a 16-bit little-endian PCM frame to signed integer samples.

    Args:
        frame: Raw PCM bytes. The length must be even.

    Raises:
        ValueError: If the frame has an odd number of bytes.
    """

    if len(frame) % 2:
        raise ValueError(f"PCM frame length must be a multiple of 2 bytes, got {len(frame)}")
    samples = array("h")
    samples.frombytes(frame)
    if sys.byteorder == "big":
        samples.byteswap()
    return samples


def rms(frame: bytes) -> float:
    """Return the root-mean-square amplitude of a PCM frame.

    The value is in sample units (0 to 32768), not dBFS. An empty frame returns
    ``0.0``. An odd-length frame raises :class:`ValueError`.
    """

    samples = pcm_to_samples(frame)
    if not samples:
        return 0.0
    total = sum(sample * sample for sample in samples)
    return math.sqrt(total / len(samples))


def dbfs_from_rms(value: float) -> float:
    """Convert a linear RMS amplitude to dBFS.

    Silence returns ``-inf`` so callers can compare against any finite
    threshold without special cases.
    """

    if value <= 0:
        return -math.inf
    return 20.0 * math.log10(value / FULL_SCALE)


def is_speech(frame: bytes, threshold_db: float) -> bool:
    """Return whether a frame's level is at or above *threshold_db* dBFS."""

    return dbfs_from_rms(rms(frame)) >= threshold_db


class Endpointer:
    """Accumulate frames into utterances using energy and silence timing.

    The endpointer is a small state machine:

    * Frames below ``threshold_db`` are held as leading pre-roll until speech
      starts. At most ``leading_silence_ms`` of that pre-roll is retained, which
      preserves the start of the first word without buffering an arbitrary
      amount of silence.
    * Speech starts an utterance. Audio is accumulated until
      ``trailing_silence_ms`` of continuous silence, or until
      ``max_utterance_ms`` is reached.
    * An utterance is returned only if it contains at least
      ``min_speech_ms`` of speech frames. Short clicks and door slams are
      discarded. Returned PCM includes the retained pre-roll and the trailing
      silence; that is useful for speech-to-text finalisation.
    * ``max_utterance_ms`` is a hard cut. A long monologue is returned in
      chunks rather than growing memory without bound.

    ``push`` accepts frames of any even length. Thresholds are converted to
    sample counts at construction time, so timing does not drift with floating
    point arithmetic. This class is not thread-safe.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 16_000,
        threshold_db: float = -40.0,
        leading_silence_ms: float = 200.0,
        trailing_silence_ms: float = 400.0,
        min_speech_ms: float = 200.0,
        max_utterance_ms: float = 30_000.0,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError(f"sample_rate must be > 0 (got {sample_rate})")
        if not -100.0 <= threshold_db <= 0.0:
            raise ValueError(f"threshold_db must be between -100 and 0 dBFS (got {threshold_db})")
        for name, value in (
            ("leading_silence_ms", leading_silence_ms),
            ("trailing_silence_ms", trailing_silence_ms),
            ("min_speech_ms", min_speech_ms),
            ("max_utterance_ms", max_utterance_ms),
        ):
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite value >= 0 (got {value})")
        if trailing_silence_ms <= 0:
            raise ValueError("trailing_silence_ms must be > 0")
        if min_speech_ms <= 0:
            raise ValueError("min_speech_ms must be > 0")
        if max_utterance_ms < min_speech_ms:
            raise ValueError(
                "max_utterance_ms must be at least min_speech_ms "
                f"(got {max_utterance_ms} < {min_speech_ms})"
            )

        self.sample_rate = sample_rate
        self.threshold_db = threshold_db
        self._leading_samples = self._to_samples(leading_silence_ms)
        self._trailing_samples = max(1, self._to_samples(trailing_silence_ms))
        self._min_speech_samples = max(1, self._to_samples(min_speech_ms))
        self._max_samples = max(1, self._to_samples(max_utterance_ms))

        self._pre_roll: deque[bytes] = deque()
        self._pre_roll_samples = 0
        self._buffer = bytearray()
        self._speech_samples = 0
        self._silence_samples = 0
        self._total_samples = 0
        self._in_utterance = False

    def _to_samples(self, milliseconds: float) -> int:
        return round(self.sample_rate * milliseconds / 1_000)

    @property
    def in_utterance(self) -> bool:
        """Whether speech has started and the utterance has not ended yet."""

        return self._in_utterance

    def push(self, frame: bytes) -> bytes | None:
        """Add one frame and return a completed utterance, if any.

        Args:
            frame: 16-bit little-endian mono PCM. Must not be empty.

        Returns:
            The utterance PCM when an endpoint is reached, otherwise ``None``.
        """

        if not frame:
            raise ValueError("frame must not be empty")
        if len(frame) % 2:
            raise ValueError(f"PCM frame length must be a multiple of 2 bytes, got {len(frame)}")

        sample_count = len(frame) // 2
        speech = is_speech(frame, self.threshold_db)

        if not self._in_utterance:
            if not speech:
                self._add_pre_roll(frame)
                return None
            self._in_utterance = True
            self._buffer = bytearray()
            for held in self._pre_roll:
                self._buffer.extend(held)
            self._buffer.extend(frame)
            self._total_samples = len(self._buffer) // 2
            self._speech_samples = sample_count
            self._silence_samples = 0
            self._clear_pre_roll()
            if self._total_samples >= self._max_samples:
                return self._finish()
            return None

        self._buffer.extend(frame)
        self._total_samples += sample_count
        if speech:
            self._speech_samples += sample_count
            self._silence_samples = 0
        else:
            self._silence_samples += sample_count

        if self._silence_samples >= self._trailing_samples:
            return self._finish()
        if self._total_samples >= self._max_samples:
            return self._finish()
        return None

    def flush(self) -> bytes | None:
        """End the current utterance immediately.

        Use this when the call is ending or the capture stream stops. Returns
        ``None`` when there is no utterance in progress or when the current
        utterance is shorter than ``min_speech_ms``.
        """

        if not self._in_utterance:
            self.reset()
            return None
        return self._finish()

    def reset(self) -> None:
        """Discard all buffered audio and return to the idle state."""

        self._buffer = bytearray()
        self._speech_samples = 0
        self._silence_samples = 0
        self._total_samples = 0
        self._in_utterance = False
        self._clear_pre_roll()

    def _add_pre_roll(self, frame: bytes) -> None:
        if self._leading_samples <= 0:
            return
        self._pre_roll.append(frame)
        self._pre_roll_samples += len(frame) // 2
        while self._pre_roll and self._pre_roll_samples > self._leading_samples:
            dropped = self._pre_roll.popleft()
            self._pre_roll_samples -= len(dropped) // 2

    def _clear_pre_roll(self) -> None:
        self._pre_roll.clear()
        self._pre_roll_samples = 0

    def _finish(self) -> bytes | None:
        payload = bytes(self._buffer) if self._speech_samples >= self._min_speech_samples else None
        self.reset()
        return payload
