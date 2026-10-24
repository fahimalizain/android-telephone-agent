"""Tests for RMS calculation, dBFS conversion, and endpointing."""

from __future__ import annotations

import math
from array import array

import pytest
from telephone_agent.vad import (
    Endpointer,
    dbfs_from_rms,
    is_speech,
    pcm_to_samples,
    rms,
)

SAMPLE_RATE = 16_000


def _frame(amplitude: int, duration_ms: float = 20.0, rate: int = SAMPLE_RATE) -> bytes:
    count = round(rate * duration_ms / 1_000)
    return array("h", [amplitude] * count).tobytes()


def _silence(duration_ms: float = 20.0, rate: int = SAMPLE_RATE) -> bytes:
    return _frame(0, duration_ms, rate)


def test_pcm_to_samples_round_trip() -> None:
    payload = array("h", [1, -2, 3, -32768, 32767]).tobytes()

    assert list(pcm_to_samples(payload)) == [1, -2, 3, -32768, 32767]


def test_pcm_to_samples_rejects_odd_length() -> None:
    with pytest.raises(ValueError, match="multiple of 2"):
        pcm_to_samples(b"\x00\x01\x02")


def test_rms_of_silence_is_zero() -> None:
    assert rms(_silence()) == 0.0
    assert rms(b"") == 0.0


def test_rms_of_constant_amplitude() -> None:
    assert rms(_frame(1_000)) == pytest.approx(1_000.0)
    assert rms(_frame(-1_000)) == pytest.approx(1_000.0)


def test_rms_of_mixed_amplitude() -> None:
    payload = array("h", [0, 16_384]).tobytes()
    assert rms(payload) == pytest.approx(math.sqrt((0 + 16_384**2) / 2))


def test_dbfs_conversion() -> None:
    assert dbfs_from_rms(32_768.0) == pytest.approx(0.0)
    assert dbfs_from_rms(3_276.8) == pytest.approx(-20.0)


def test_dbfs_of_silence_is_negative_infinity() -> None:
    value = dbfs_from_rms(0.0)
    assert math.isinf(value)
    assert value < 0


def test_is_speech_uses_dbfs_threshold() -> None:
    speech = _frame(1_000)

    assert is_speech(speech, -40.0)
    assert is_speech(speech, -31.0)
    assert not is_speech(speech, -30.0)
    assert not is_speech(_silence(), -100.0)


def test_endpointer_returns_utterance_after_trailing_silence() -> None:
    endpointer = Endpointer(
        sample_rate=SAMPLE_RATE,
        threshold_db=-40.0,
        leading_silence_ms=200.0,
        trailing_silence_ms=400.0,
        min_speech_ms=200.0,
        max_utterance_ms=5_000.0,
    )

    for _ in range(10):
        assert endpointer.push(_silence()) is None
    for _ in range(15):
        assert endpointer.push(_frame(1_000)) is None
    assert endpointer.in_utterance

    for _ in range(19):
        assert endpointer.push(_silence()) is None
    assert endpointer.in_utterance

    utterance = endpointer.push(_silence())

    assert utterance is not None
    assert not endpointer.in_utterance
    # 200 ms of retained leading silence + 300 ms speech + 400 ms trailing silence.
    assert len(utterance) == int(0.9 * SAMPLE_RATE) * 2


def test_endpointer_limits_leading_silence() -> None:
    endpointer = Endpointer(
        sample_rate=SAMPLE_RATE,
        leading_silence_ms=100.0,
        trailing_silence_ms=400.0,
        min_speech_ms=200.0,
    )

    for _ in range(50):
        endpointer.push(_silence())
    for _ in range(15):
        endpointer.push(_frame(1_000))
    utterance = None
    for _ in range(20):
        utterance = endpointer.push(_silence())

    assert utterance is not None
    assert len(utterance) == int(0.8 * SAMPLE_RATE) * 2


def test_endpointer_can_disable_leading_preroll() -> None:
    endpointer = Endpointer(
        sample_rate=SAMPLE_RATE,
        leading_silence_ms=0.0,
        trailing_silence_ms=400.0,
        min_speech_ms=200.0,
    )

    for _ in range(20):
        endpointer.push(_silence())
    for _ in range(15):
        endpointer.push(_frame(1_000))
    utterance = None
    for _ in range(20):
        utterance = endpointer.push(_silence())

    assert utterance is not None
    assert len(utterance) == int(0.7 * SAMPLE_RATE) * 2


def test_endpointer_discards_short_speech() -> None:
    endpointer = Endpointer(
        sample_rate=SAMPLE_RATE,
        leading_silence_ms=200.0,
        trailing_silence_ms=400.0,
        min_speech_ms=200.0,
    )

    for _ in range(5):
        endpointer.push(_frame(1_000))
    for _ in range(20):
        assert endpointer.push(_silence()) is None
    assert not endpointer.in_utterance

    for _ in range(15):
        endpointer.push(_frame(1_000))
    utterance = None
    for _ in range(20):
        utterance = endpointer.push(_silence())

    assert utterance is not None


def test_endpointer_cuts_at_max_utterance() -> None:
    endpointer = Endpointer(
        sample_rate=SAMPLE_RATE,
        leading_silence_ms=0.0,
        trailing_silence_ms=400.0,
        min_speech_ms=200.0,
        max_utterance_ms=500.0,
    )

    result = None
    for _ in range(25):
        result = endpointer.push(_frame(1_000))

    assert result is not None
    assert len(result) == int(0.5 * SAMPLE_RATE) * 2
    assert not endpointer.in_utterance

    assert endpointer.push(_frame(1_000)) is None
    assert endpointer.in_utterance


def test_endpointer_flush_ends_utterance() -> None:
    endpointer = Endpointer(
        sample_rate=SAMPLE_RATE,
        leading_silence_ms=200.0,
        trailing_silence_ms=400.0,
        min_speech_ms=200.0,
    )

    assert endpointer.flush() is None
    for _ in range(15):
        endpointer.push(_frame(1_000))

    utterance = endpointer.flush()

    assert utterance is not None
    assert len(utterance) == int(0.3 * SAMPLE_RATE) * 2
    assert not endpointer.in_utterance
    assert endpointer.flush() is None


def test_endpointer_flush_discards_short_speech() -> None:
    endpointer = Endpointer(sample_rate=SAMPLE_RATE, min_speech_ms=200.0)

    for _ in range(5):
        endpointer.push(_frame(1_000))

    assert endpointer.flush() is None
    assert not endpointer.in_utterance


def test_endpointer_reset_discards_state() -> None:
    endpointer = Endpointer(sample_rate=SAMPLE_RATE, min_speech_ms=200.0)

    for _ in range(15):
        endpointer.push(_frame(1_000))
    assert endpointer.in_utterance

    endpointer.reset()

    assert not endpointer.in_utterance
    for _ in range(15):
        endpointer.push(_frame(1_000))
    assert endpointer.flush() is not None


def test_endpointer_accepts_arbitrary_frame_lengths() -> None:
    endpointer = Endpointer(
        sample_rate=SAMPLE_RATE,
        leading_silence_ms=0.0,
        trailing_silence_ms=400.0,
        min_speech_ms=200.0,
    )

    assert endpointer.push(_frame(1_000, 300.0)) is None
    utterance = endpointer.push(_silence(400.0))

    assert utterance is not None
    assert len(utterance) == int(0.7 * SAMPLE_RATE) * 2


def test_endpointer_rejects_empty_and_odd_frames() -> None:
    endpointer = Endpointer(sample_rate=SAMPLE_RATE)

    with pytest.raises(ValueError, match="must not be empty"):
        endpointer.push(b"")
    with pytest.raises(ValueError, match="multiple of 2"):
        endpointer.push(b"\x00\x01\x02")


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"sample_rate": 0}, "sample_rate"),
        ({"threshold_db": 1.0}, "threshold_db"),
        ({"threshold_db": -101.0}, "threshold_db"),
        ({"leading_silence_ms": -1.0}, "leading_silence_ms"),
        ({"trailing_silence_ms": 0.0}, "trailing_silence_ms"),
        ({"min_speech_ms": 0.0}, "min_speech_ms"),
        ({"min_speech_ms": 500.0, "max_utterance_ms": 400.0}, "max_utterance_ms"),
    ],
)
def test_endpointer_validation(kwargs: dict[str, float], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Endpointer(**kwargs)
