"""Tests for the local JSONL call transcript writer."""

from __future__ import annotations

import json
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from telephone_agent.pipeline import (
    AgentReply,
    CallEndReason,
    CallFinished,
    CallResult,
    CallState,
    Utterance,
)
from telephone_agent.transcript import TranscriptLogger

FIXED_NOW = datetime(
    2026,
    10,
    24,
    9,
    15,
    2,
    123_000,
    tzinfo=timezone(timedelta(hours=5, minutes=30)),
)
CALL_ID = "abcdef1234567890"


def _read(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_writes_start_transcript_reply_and_end(tmp_path: Path) -> None:
    logger = TranscriptLogger(
        tmp_path / "transcripts",
        call_id=CALL_ID,
        clock=lambda: FIXED_NOW,
    )

    logger.call_start(
        direction="outbound",
        number="+15555550100",
        objective="Confirm the appointment.",
        audio_backend="pipewire",
        realtime_kind="grok",
        realtime_model="grok-voice-3.0",
        realtime_voice="ara",
    )
    logger.transcript("Hello, who is this?")
    logger.reply("This is the telephone agent.")
    logger.call_end(
        state="idle",
        reason="completed",
        turns=1,
        duration_s=12.5,
    )
    logger.close()

    assert logger.path is not None
    assert logger.path.name == "20261024T091502-abcdef12.jsonl"
    records = _read(logger.path)
    assert records == [
        {
            "ts": "2026-10-24T09:15:02.123+05:30",
            "call_id": CALL_ID,
            "event": "call_start",
            "direction": "outbound",
            "number": "+15555550100",
            "audio_backend": "pipewire",
            "realtime_kind": "grok",
            "objective": "Confirm the appointment.",
            "realtime_model": "grok-voice-3.0",
            "realtime_voice": "ara",
        },
        {
            "ts": "2026-10-24T09:15:02.123+05:30",
            "call_id": CALL_ID,
            "event": "transcript",
            "role": "callee",
            "text": "Hello, who is this?",
        },
        {
            "ts": "2026-10-24T09:15:02.123+05:30",
            "call_id": CALL_ID,
            "event": "reply",
            "role": "agent",
            "text": "This is the telephone agent.",
        },
        {
            "ts": "2026-10-24T09:15:02.123+05:30",
            "call_id": CALL_ID,
            "event": "call_end",
            "state": "idle",
            "reason": "completed",
            "turns": 1,
            "duration_s": 12.5,
        },
    ]


def test_stores_the_full_number_without_masking(tmp_path: Path) -> None:
    logger = TranscriptLogger(tmp_path, call_id=CALL_ID, clock=lambda: FIXED_NOW)

    logger.call_start(direction="outbound", number="+15555550100")
    logger.close()

    assert logger.path is not None
    text = logger.path.read_text(encoding="utf-8")
    assert "+15555550100" in text
    assert "*******0100" not in text


def test_call_start_omits_empty_objective_and_non_realtime_metadata(tmp_path: Path) -> None:
    logger = TranscriptLogger(tmp_path, call_id=CALL_ID, clock=lambda: FIXED_NOW)

    logger.call_start(
        direction="outbound",
        number="+15555550100",
        audio_backend="alsa",
        realtime_kind="off",
    )
    logger.close()

    assert logger.path is not None
    record = _read(logger.path)[0]
    assert record["number"] == "+15555550100"
    assert record["audio_backend"] == "alsa"
    assert record["realtime_kind"] == "off"
    assert "objective" not in record
    assert "realtime_model" not in record
    assert "realtime_voice" not in record


def test_stores_conversation_text_verbatim(tmp_path: Path) -> None:
    logger = TranscriptLogger(tmp_path, call_id=CALL_ID, clock=lambda: FIXED_NOW)
    long_text = (
        "ഹലോ, ഇത് വളരെ നീളമുള്ള ഒരു വാക്യമാണ്; pronouns, punctuation, "
        'quotes like "this", and even an emoji 🙂 must survive exactly as spoken. '
        "This second clause makes the sentence deliberately longer so no truncation "
        "can hide the original content."
    )

    logger.transcript(long_text)
    logger.reply(long_text)
    logger.close()

    assert len(long_text) > 200
    assert logger.path is not None
    raw = logger.path.read_text(encoding="utf-8")
    assert "ഹലോ" in raw
    assert "🙂" in raw
    records = _read(logger.path)
    assert records[0]["text"] == long_text
    assert records[1]["text"] == long_text


def test_file_is_0600_and_directory_is_0700(tmp_path: Path) -> None:
    directory = tmp_path / "nested" / "transcripts"
    logger = TranscriptLogger(directory, call_id=CALL_ID, clock=lambda: FIXED_NOW)

    try:
        assert logger.path is not None
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(logger.path.stat().st_mode) == 0o600
    finally:
        logger.close()


def test_disabled_logger_creates_nothing(tmp_path: Path) -> None:
    directory = tmp_path / "transcripts"
    logger = TranscriptLogger(directory, call_id=CALL_ID, enabled=False)

    logger.call_start(direction="outbound", number="+15555550100")
    logger.transcript("hello")
    logger.reply("hi")
    logger.call_end(state="idle", reason="completed", turns=1, duration_s=1.0)
    logger.close()
    logger.close()

    assert logger.path is None
    assert not directory.exists()


def test_close_is_idempotent(tmp_path: Path) -> None:
    logger = TranscriptLogger(tmp_path, call_id=CALL_ID, clock=lambda: FIXED_NOW)

    logger.close()
    logger.close()

    # A write after close is ignored rather than raising.
    logger.transcript("late")
    assert logger.path is not None
    assert _read(logger.path) == []


def test_open_failure_disables_writes_without_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_open(*_args: object, **_kwargs: object) -> int:
        raise OSError("disk is full")

    monkeypatch.setattr("telephone_agent.transcript.os.open", fail_open)

    logger = TranscriptLogger(tmp_path / "transcripts", call_id=CALL_ID)
    logger.call_start(direction="outbound", number="+15555550100")
    logger.transcript("hello")
    logger.close()

    assert logger.path is None
    assert not list((tmp_path / "transcripts").glob("*.jsonl"))


def test_write_failure_disables_further_writes_without_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = TranscriptLogger(tmp_path, call_id=CALL_ID, clock=lambda: FIXED_NOW)

    def fail_write(_fd: int, _data: bytes) -> None:
        raise OSError("disk is full")

    monkeypatch.setattr("telephone_agent.transcript._write_all", fail_write)

    logger.call_start(direction="outbound", number="+15555550100")
    logger.transcript("hello")
    logger.close()

    assert logger.path is not None
    assert _read(logger.path) == []


def test_record_maps_session_events_and_closes_on_finish(tmp_path: Path) -> None:
    logger = TranscriptLogger(tmp_path, call_id=CALL_ID, clock=lambda: FIXED_NOW)

    logger.record(Utterance("hello"))
    logger.record(AgentReply("hi"))
    logger.record(
        CallFinished(
            CallResult(
                state=CallState.IDLE,
                reason=CallEndReason.COMPLETED,
                duration_s=3.25,
                turns=1,
            )
        )
    )

    assert logger.path is not None
    records = _read(logger.path)
    assert [record["event"] for record in records] == [
        "transcript",
        "reply",
        "call_end",
    ]
    assert records[0]["role"] == "callee"
    assert records[1]["role"] == "agent"
    assert records[2]["state"] == "idle"
    assert records[2]["reason"] == "completed"
    assert records[2]["turns"] == 1
    assert records[2]["duration_s"] == 3.25

    # CallFinished closed the file, so a second close is a no-op.
    logger.close()


def test_reserved_fields_cannot_be_overridden(tmp_path: Path) -> None:
    logger = TranscriptLogger(tmp_path, call_id=CALL_ID, clock=lambda: FIXED_NOW)

    logger.log_event("custom", ts="fake", call_id="other", event="shadow", detail="kept")
    logger.close()

    assert logger.path is not None
    record = _read(logger.path)[0]
    assert record["ts"] == "2026-10-24T09:15:02.123+05:30"
    assert record["call_id"] == CALL_ID
    assert record["event"] == "custom"
    assert record["detail"] == "kept"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_append_mode_keeps_existing_content(tmp_path: Path) -> None:
    first = TranscriptLogger(tmp_path, call_id=CALL_ID, clock=lambda: FIXED_NOW)
    first.transcript("first")
    first.close()

    second = TranscriptLogger(tmp_path, call_id=CALL_ID, clock=lambda: FIXED_NOW)
    second.transcript("second")
    second.close()

    assert first.path == second.path
    assert first.path is not None
    assert [record["text"] for record in _read(first.path)] == ["first", "second"]
