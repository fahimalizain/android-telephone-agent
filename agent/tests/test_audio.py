"""Tests for the PipeWire Bluetooth HFP audio backend and SCO discovery."""

from __future__ import annotations

import json
import subprocess
import sys
import time

import pytest
from telephone_agent.audio import (
    AudioError,
    PipeWireAudio,
    normalize_bluetooth_address,
    sco_nodes_from_dump,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"

SCO_DUMP = [
    {
        "id": 81,
        "info": {
            "props": {
                "api.bluez5.address": ADDRESS,
                "api.bluez5.codec": "msbc",
                "api.bluez5.profile": "headset-audio-gateway",
                "factory.name": "api.bluez5.sco.source",
                "media.class": "Stream/Output/Audio",
                "media.role": "Communication",
                "node.name": f"bluez_input.{ADDRESS}.0",
            }
        },
    },
    {
        "id": 89,
        "info": {
            "props": {
                "api.bluez5.address": ADDRESS,
                "api.bluez5.codec": "msbc",
                "api.bluez5.profile": "headset-audio-gateway",
                "factory.name": "api.bluez5.sco.sink",
                "media.class": "Stream/Input/Audio",
                "media.role": "Communication",
                "node.name": f"bluez_output.{ADDRESS}.1",
            }
        },
    },
    {
        "id": 50,
        "info": {
            "props": {
                "api.bluez5.address": ADDRESS,
                "factory.name": "api.bluez5.a2dp.source",
                "node.name": f"bluez_output.{ADDRESS}.5",
            }
        },
    },
    {
        "id": 60,
        "info": {
            "props": {
                "api.bluez5.address": "11:22:33:44:55:66",
                "factory.name": "api.bluez5.sco.source",
                "node.name": "bluez_input.11:22:33:44:55:66.0",
            }
        },
    },
    {"id": 99, "info": {"props": {}}},
    {"id": 100},
]


def test_sco_nodes_from_dump_matches_address_and_factory() -> None:
    capture, playback = sco_nodes_from_dump(SCO_DUMP, ADDRESS.lower())

    assert capture == f"bluez_input.{ADDRESS}.0"
    assert playback == f"bluez_output.{ADDRESS}.1"


def test_sco_nodes_from_dump_ignores_other_devices_and_profiles() -> None:
    capture, playback = sco_nodes_from_dump(SCO_DUMP, "11:22:33:44:55:66")

    assert capture == "bluez_input.11:22:33:44:55:66.0"
    assert playback is None


def test_sco_nodes_from_dump_requires_a_list() -> None:
    with pytest.raises(AudioError, match="not a list"):
        sco_nodes_from_dump({"not": "a list"}, ADDRESS)


def test_normalize_bluetooth_address() -> None:
    assert normalize_bluetooth_address("aa:bb:cc:dd:ee:ff") == ADDRESS
    assert normalize_bluetooth_address(f"  {ADDRESS}  ") == ADDRESS


@pytest.mark.parametrize(
    "address",
    ["", "AA:BB:CC:DD:EE", "AA-BB-CC-DD-EE-FF", "zz:bb:cc:dd:ee:ff", "AA:BB:CC:DD:EE:FF:00"],
)
def test_normalize_bluetooth_address_rejects_invalid_values(address: str) -> None:
    with pytest.raises(ValueError, match="AA:BB:CC:DD:EE:FF"):
        normalize_bluetooth_address(address)


def test_find_sco_nodes_queries_pw_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["pw-dump"],
            returncode=0,
            stdout=json.dumps(SCO_DUMP),
            stderr="",
        )

    monkeypatch.setattr("telephone_agent.audio.subprocess.run", fake_run)

    capture, playback = PipeWireAudio.find_sco_nodes(ADDRESS)

    assert capture == f"bluez_input.{ADDRESS}.0"
    assert playback == f"bluez_output.{ADDRESS}.1"


def test_find_sco_nodes_rejects_invalid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["pw-dump"],
            returncode=0,
            stdout="not json",
            stderr="",
        )

    monkeypatch.setattr("telephone_agent.audio.subprocess.run", fake_run)

    with pytest.raises(AudioError, match="invalid JSON"):
        PipeWireAudio.find_sco_nodes(ADDRESS)


def test_find_sco_nodes_reports_empty_pw_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["pw-dump"],
            returncode=0,
            stdout="",
            stderr="",
        )

    monkeypatch.setattr("telephone_agent.audio.subprocess.run", fake_run)

    with pytest.raises(AudioError, match="no data"):
        PipeWireAudio.find_sco_nodes(ADDRESS)


def test_pipewire_start_times_out_when_sco_nodes_are_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio = PipeWireAudio(ADDRESS, node_timeout_s=0.01, poll_interval_s=0.001)
    monkeypatch.setattr("telephone_agent.audio._find_sco_nodes", lambda *_a, **_k: (None, None))

    with pytest.raises(AudioError, match="did not appear"):
        audio.start()


def test_pipewire_start_rediscovers_call_scoped_nodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio = PipeWireAudio(ADDRESS)
    audio._capture_node = "stale-capture"
    audio._playback_node = "stale-playback"
    discovered: list[bool] = []

    def fake_discover() -> None:
        discovered.append(True)
        audio._capture_node = "fresh-capture"
        audio._playback_node = "fresh-playback"

    monkeypatch.setattr(audio, "_discover_nodes", fake_discover)
    monkeypatch.setattr("telephone_agent.audio._ProcessAudio.start", lambda _self: None)

    audio.start()

    assert discovered == [True]
    assert audio.capture_node == "fresh-capture"
    assert audio.playback_node == "fresh-playback"


def test_pipewire_start_retries_transient_discovery_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audio = PipeWireAudio(ADDRESS, node_timeout_s=0.2, poll_interval_s=0.001)
    calls: list[int] = []

    def flaky(*_args: object, **_kwargs: object) -> tuple[str, str]:
        calls.append(1)
        if len(calls) == 1:
            raise AudioError("pw-dump failed during call setup")
        return f"bluez_input.{ADDRESS}.0", f"bluez_output.{ADDRESS}.1"

    monkeypatch.setattr("telephone_agent.audio._find_sco_nodes", flaky)
    monkeypatch.setattr("telephone_agent.audio._ProcessAudio.start", lambda _self: None)

    audio.start()

    assert len(calls) == 2
    assert audio.capture_node == f"bluez_input.{ADDRESS}.0"
    assert audio.playback_node == f"bluez_output.{ADDRESS}.1"


def test_pipewire_commands_target_discovered_nodes() -> None:
    audio = PipeWireAudio(ADDRESS)
    audio._capture_node = f"bluez_input.{ADDRESS}.0"
    audio._playback_node = f"bluez_output.{ADDRESS}.1"

    assert audio._capture_command() == [
        "pw-record",
        "--raw",
        "--format",
        "s16",
        "--rate",
        "16000",
        "--channels",
        "1",
        "--properties",
        '{"node.dont-fallback": true, "node.dont-reconnect": true}',
        "--target",
        f"bluez_input.{ADDRESS}.0",
        "-",
    ]
    assert audio._playback_command() == [
        "pw-play",
        "--raw",
        "--format",
        "s16",
        "--rate",
        "16000",
        "--channels",
        "1",
        "--properties",
        '{"node.dont-fallback": true, "node.dont-reconnect": true}',
        "--target",
        f"bluez_output.{ADDRESS}.1",
        "-",
    ]


def test_pipewire_rejects_invalid_timeouts() -> None:
    with pytest.raises(ValueError, match="node_timeout_s"):
        PipeWireAudio(ADDRESS, node_timeout_s=0)
    with pytest.raises(ValueError, match="poll_interval_s"):
        PipeWireAudio(ADDRESS, poll_interval_s=0)


def test_frames_streams_pcm_and_stops_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    audio = PipeWireAudio(ADDRESS)
    monkeypatch.setattr(audio, "_discover_nodes", lambda: None)
    # Two 20 ms mono frames at 16 kHz: 640 bytes each.
    audio._capture_command = lambda: [  # type: ignore[method-assign]
        sys.executable,
        "-c",
        "import sys; sys.stdout.buffer.write(b'\\x01\\x02' * 640)",
    ]

    frames = list(audio.frames())

    assert len(frames) == 2
    assert all(len(frame) == audio.frame_bytes for frame in frames)
    assert audio._capture is None


def test_frames_reports_capture_stderr_and_status(monkeypatch: pytest.MonkeyPatch) -> None:
    audio = PipeWireAudio(ADDRESS)
    monkeypatch.setattr(audio, "_discover_nodes", lambda: None)
    audio._capture_command = lambda: [  # type: ignore[method-assign]
        sys.executable,
        "-c",
        "import sys; sys.stderr.write('capture boom'); sys.exit(3)",
    ]

    with pytest.raises(AudioError, match="capture boom") as excinfo:
        list(audio.frames())

    assert "status 3" in str(excinfo.value)


def test_play_writes_pcm_to_the_player() -> None:
    audio = PipeWireAudio(ADDRESS)
    audio._playback_command = lambda: [  # type: ignore[method-assign]
        sys.executable,
        "-c",
        "import sys; data = sys.stdin.buffer.read(); sys.exit(0 if data else 4)",
    ]

    audio.play(b"\x00\x00" * audio.frame_bytes)


def test_play_reports_player_stderr_and_status() -> None:
    audio = PipeWireAudio(ADDRESS)
    audio._playback_command = lambda: [  # type: ignore[method-assign]
        sys.executable,
        "-c",
        "import sys; sys.stdin.buffer.read(); sys.stderr.write('player boom'); sys.exit(2)",
    ]

    with pytest.raises(AudioError, match="player boom") as excinfo:
        audio.play(b"\x00\x00" * audio.frame_bytes)

    assert "status 2" in str(excinfo.value)


def test_streaming_playback_writes_chunks_and_closes() -> None:
    audio = PipeWireAudio(ADDRESS)
    audio._playback_command = lambda: [  # type: ignore[method-assign]
        sys.executable,
        "-c",
        ("import sys; data = sys.stdin.buffer.read(); sys.exit(0 if len(data) == 1280 else 3)"),
    ]

    with audio.start_playback() as stream:
        stream.write(b"\x00\x00" * 320)
        stream.write(b"\x00\x00" * 320)


def test_streaming_playback_stop_terminates_the_player() -> None:
    audio = PipeWireAudio(ADDRESS)
    audio._playback_command = lambda: [  # type: ignore[method-assign]
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
    ]
    stream = audio.start_playback()
    stream.write(b"\x00\x00" * 320)

    started = time.monotonic()
    stream.stop()
    elapsed = time.monotonic() - started
    stream.close()

    assert elapsed < 5.0


def test_streaming_playback_can_restart_after_stop() -> None:
    audio = PipeWireAudio(ADDRESS)
    audio._playback_command = lambda: [  # type: ignore[method-assign]
        sys.executable,
        "-c",
        "import sys; sys.stdin.buffer.read(); sys.exit(0)",
    ]
    stream = audio.start_playback()
    stream.write(b"\x00\x00" * 320)
    stream.stop()
    stream.write(b"\x00\x00" * 320)
    stream.close()
