"""Tests for the CLI-to-runtime wiring that does not need a real device."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
from telephone_agent.__main__ import (
    _audio_from_config,
    _check_pipewire_tools,
    _cmd_doctor,
    _cmd_selftest,
    _dialer_from_config,
)
from telephone_agent.audio import (
    AudioError,
    AudioStats,
    CaptureDevice,
    GadgetAudio,
    PipeWireAudio,
)
from telephone_agent.config import AudioConfig, Config, DialerConfig

ADDRESS = "AA:BB:CC:DD:EE:FF"


class FakeDialer:
    def health(self) -> dict[str, object]:
        return {"status": "ok", "version": "test"}


def _write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_dialer_from_config_uses_dialer_section() -> None:
    dialer = _dialer_from_config(
        DialerConfig(
            url="http://127.0.0.1:8765",
            token="test-token",
            timeout_s=2.5,
            retries=1,
        )
    )

    assert dialer.url == "http://127.0.0.1:8765"
    assert dialer.token == "test-token"
    assert dialer.timeout_s == 2.5
    assert dialer.retries == 1


def test_audio_from_config_builds_the_usb_gadget_backend() -> None:
    config = Config(audio=AudioConfig(backend="alsa", card="UAC1Gadget", device=1))

    audio = _audio_from_config(config)

    assert isinstance(audio, GadgetAudio)
    assert audio.card == "UAC1Gadget"
    assert audio.device == 1


def test_audio_from_config_builds_the_bluetooth_backend() -> None:
    config = Config(
        audio=AudioConfig(
            backend="pipewire",
            bluetooth_address="AA:BB:CC:DD:EE:FF",
            node_timeout_s=12.0,
        )
    )

    audio = _audio_from_config(config)

    assert isinstance(audio, PipeWireAudio)
    assert audio.bluetooth_address == "AA:BB:CC:DD:EE:FF"
    assert audio.description == "Bluetooth HFP AA:BB:CC:DD:EE:FF (PipeWire SCO)"


def test_doctor_bluetooth_reports_active_sco_nodes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _write_config(
        tmp_path,
        f"""
[audio]
backend = "pipewire"
bluetooth_address = "{ADDRESS}"

[dialer]
url = "http://127.0.0.1:8765"
token = "test-token"
""",
    )
    monkeypatch.setattr("telephone_agent.__main__._check_pipewire_tools", lambda: True)
    monkeypatch.setattr(
        "telephone_agent.__main__.PipeWireAudio.find_sco_nodes",
        lambda *_args, **_kwargs: (
            f"bluez_input.{ADDRESS}.0",
            f"bluez_output.{ADDRESS}.1",
        ),
    )
    monkeypatch.setattr(
        "telephone_agent.__main__._dialer_from_config",
        lambda _config: FakeDialer(),
    )

    assert _cmd_doctor(argparse.Namespace(config=str(path))) == 0

    output = capsys.readouterr().out
    assert "audio backend: pipewire" in output
    assert f"downlink: bluez_input.{ADDRESS}.0" in output
    assert f"uplink:   bluez_output.{ADDRESS}.1" in output
    assert "dial helper: ok" in output
    assert "doctor: ok" in output


def test_doctor_bluetooth_idle_is_not_a_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _write_config(
        tmp_path,
        f"""
[audio]
backend = "pipewire"
bluetooth_address = "{ADDRESS}"

[dialer]
url = "http://127.0.0.1:8765"
token = "test-token"
""",
    )
    monkeypatch.setattr("telephone_agent.__main__._check_pipewire_tools", lambda: True)
    monkeypatch.setattr(
        "telephone_agent.__main__.PipeWireAudio.find_sco_nodes",
        lambda *_args, **_kwargs: (None, None),
    )
    monkeypatch.setattr(
        "telephone_agent.__main__._dialer_from_config",
        lambda _config: FakeDialer(),
    )

    assert _cmd_doctor(argparse.Namespace(config=str(path))) == 0

    output = capsys.readouterr().out
    assert "SCO nodes: not present" in output
    assert "doctor: ok" in output


def test_doctor_bluetooth_incomplete_sco_pair_is_a_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _write_config(
        tmp_path,
        f"""
[audio]
backend = "pipewire"
bluetooth_address = "{ADDRESS}"

[dialer]
url = "http://127.0.0.1:8765"
token = "test-token"
""",
    )
    monkeypatch.setattr("telephone_agent.__main__._check_pipewire_tools", lambda: True)
    monkeypatch.setattr(
        "telephone_agent.__main__.PipeWireAudio.find_sco_nodes",
        lambda *_args, **_kwargs: (f"bluez_input.{ADDRESS}.0", None),
    )
    monkeypatch.setattr(
        "telephone_agent.__main__._dialer_from_config",
        lambda _config: FakeDialer(),
    )

    assert _cmd_doctor(argparse.Namespace(config=str(path))) == 1

    output = capsys.readouterr().out
    assert "SCO nodes: incomplete" in output
    assert "downlink=present" in output
    assert "uplink=missing" in output
    assert "doctor: FAIL" in output


def test_doctor_bluetooth_pw_dump_failure_is_a_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _write_config(
        tmp_path,
        f"""
[audio]
backend = "pipewire"
bluetooth_address = "{ADDRESS}"

[dialer]
url = "http://127.0.0.1:8765"
token = "test-token"
""",
    )
    monkeypatch.setattr("telephone_agent.__main__._check_pipewire_tools", lambda: True)

    def fail(*_args: object, **_kwargs: object) -> tuple[None, None]:
        raise AudioError("pw-dump is not running")

    monkeypatch.setattr("telephone_agent.__main__.PipeWireAudio.find_sco_nodes", fail)
    monkeypatch.setattr(
        "telephone_agent.__main__._dialer_from_config",
        lambda _config: FakeDialer(),
    )

    assert _cmd_doctor(argparse.Namespace(config=str(path))) == 1

    captured = capsys.readouterr()
    assert "audio: FAIL: pw-dump is not running" in captured.err
    assert "doctor: FAIL" in captured.out


def test_doctor_alsa_reports_capture_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _write_config(
        tmp_path,
        """
[audio]
card = "UAC1Gadget"

[dialer]
url = "http://127.0.0.1:8765"
token = "test-token"
""",
    )
    monkeypatch.setattr(
        "telephone_agent.__main__.GadgetAudio.list_cards",
        lambda: [CaptureDevice(0, "UAC1Gadget", "USB Audio", 0, "USB Audio")],
    )
    monkeypatch.setattr(
        "telephone_agent.__main__._dialer_from_config",
        lambda _config: FakeDialer(),
    )

    assert _cmd_doctor(argparse.Namespace(config=str(path))) == 0

    output = capsys.readouterr().out
    assert "audio backend: alsa" in output
    assert "capture devices:" in output
    assert "doctor: ok" in output


def test_check_pipewire_tools_reports_missing_binaries(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("telephone_agent.__main__.shutil.which", lambda _tool: None)

    assert _check_pipewire_tools() is False

    captured = capsys.readouterr()
    assert "pw-record" in captured.err
    assert "pipewire-bin" in captured.err


class FakeSelftestAudio:
    description = "fake audio device"

    def __init__(self, *, rms: float) -> None:
        self._rms = rms

    def selftest(self) -> AudioStats:
        return AudioStats(
            frames=1,
            duration_s=2.0,
            rms=self._rms,
            dbfs=-999.0 if self._rms == 0 else -20.0,
            peak=0 if self._rms == 0 else 100,
        )


@pytest.mark.parametrize(("rms", "expected"), [(0.0, 1), (100.0, 0)])
def test_selftest_exit_code_tracks_digital_silence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    rms: float,
    expected: int,
) -> None:
    path = _write_config(
        tmp_path,
        """
[audio]
card = "UAC1Gadget"

[dialer]
url = "http://127.0.0.1:8765"
token = "test-token"
""",
    )
    monkeypatch.setattr(
        "telephone_agent.__main__._audio_from_config",
        lambda _config: FakeSelftestAudio(rms=rms),
    )

    assert _cmd_selftest(argparse.Namespace(config=str(path))) == expected

    captured = capsys.readouterr()
    assert "fake audio device" in captured.out
    if expected:
        assert "digital silence" in captured.err
