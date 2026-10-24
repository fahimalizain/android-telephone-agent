"""Tests for the CLI-to-runtime wiring that does not need a real device."""

from __future__ import annotations

import argparse
import asyncio
import builtins
import json
import socket
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from telephone_agent.__main__ import (
    _audio_from_config,
    _build_session_factory,
    _check_pipewire_tools,
    _cmd_daemon,
    _cmd_doctor,
    _cmd_inbound,
    _cmd_run,
    _cmd_selftest,
    _cmd_serve,
    _dialer_from_config,
    _inbound_session_factory,
    _serve,
    _server_apps,
    build_parser,
    main,
)
from telephone_agent.audio import AudioError, AudioStats, PipeWireAudio
from telephone_agent.config import (
    AgentConfig,
    AudioConfig,
    Config,
    ConfigError,
    DialerConfig,
    InboundConfig,
    RealtimeConfig,
    ServerConfig,
    TranscriptConfig,
)
from telephone_agent.consult import ConsultationBroker
from telephone_agent.daemon import CallReply
from telephone_agent.pipeline import (
    AgentReply,
    CallEndReason,
    CallFinished,
    CallResult,
    CallState,
    Utterance,
)

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


def test_audio_from_config_builds_the_bluetooth_backend() -> None:
    config = Config(
        audio=AudioConfig(
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
bluetooth_address = "AA:BB:CC:DD:EE:FF"

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


def _serve_config(
    tmp_path: Path,
    *,
    token: str = "test-server-token",
    host: str = "127.0.0.1",
    public_url: str = "",
) -> Path:
    return _write_config(
        tmp_path,
        f"""
phone_number = "+15555550100"

[audio]
bluetooth_address = "AA:BB:CC:DD:EE:FF"

[dialer]
url = "http://127.0.0.1:8765"
token = "test-token"

[server]
host = "{host}"
token = "{token}"
public_url = "{public_url}"
""",
    )


def test_serve_subcommand_parses_and_dispatches(tmp_path: Path) -> None:
    parser = build_parser()

    args = parser.parse_args(["serve", "--config", str(tmp_path / "config.toml")])

    assert args.command == "serve"
    assert args.config == str(tmp_path / "config.toml")
    assert args.handler is _cmd_serve


def test_help_lists_serve_without_importing_server_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real_import = builtins.__import__

    def guarded_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "mcp" or name.startswith("mcp.") or name == "a2a" or name.startswith("a2a."):
            raise ImportError(f"{name} is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["--help"])

    assert excinfo.value.code == 0
    assert "serve" in capsys.readouterr().out


def test_server_apps_reports_missing_server_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    real_import = builtins.__import__

    def guarded_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "servers" or name.startswith("servers."):
            raise ImportError("the server extra is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    with pytest.raises(ConfigError, match="server dependencies are not installed"):
        _server_apps(Config(server=ServerConfig(token="test-server-token")), service=object())


def test_cmd_serve_requires_a_token(tmp_path: Path) -> None:
    path = _serve_config(tmp_path, token="")

    with pytest.raises(ConfigError, match="server.token is required"):
        _cmd_serve(argparse.Namespace(config=str(path)))


def test_cmd_serve_requires_public_url_off_loopback(tmp_path: Path) -> None:
    path = _serve_config(tmp_path, host="192.168.1.50")

    with pytest.raises(ConfigError, match="server.public_url is required"):
        _cmd_serve(argparse.Namespace(config=str(path)))


def test_cmd_serve_builds_and_runs_both_apps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _serve_config(tmp_path, token="super-secret-token")
    service = object()
    apps: list[tuple[object, str, int]] = [(object(), "127.0.0.1", 8765)]
    calls: dict[str, object] = {}

    def fake_build_service(config: Config) -> object:
        calls["config"] = config
        return service

    def fake_server_apps(config: Config, built_service: object) -> list[tuple[object, str, int]]:
        calls["apps_service"] = built_service
        return apps

    async def fake_serve(built_service: object, built_apps: object) -> None:
        calls["service"] = built_service
        calls["apps"] = built_apps

    monkeypatch.setattr("telephone_agent.__main__._build_service", fake_build_service)
    monkeypatch.setattr("telephone_agent.__main__._server_apps", fake_server_apps)
    monkeypatch.setattr("telephone_agent.__main__._serve", fake_serve)

    assert _cmd_serve(argparse.Namespace(config=str(path))) == 0

    assert isinstance(calls["config"], Config)
    assert calls["apps_service"] is service
    assert calls["service"] is service
    assert calls["apps"] is apps

    captured = capsys.readouterr()
    assert "MCP endpoint: http://127.0.0.1:8765/mcp" in captured.out
    assert "A2A RPC endpoint: http://127.0.0.1:8766/a2a" in captured.out
    assert "Agent Card: http://127.0.0.1:8766/.well-known/agent-card.json" in captured.out
    assert "auth: bearer token required" in captured.out
    assert "super-secret-token" not in captured.out
    assert "super-secret-token" not in captured.err


def test_cmd_serve_advertises_public_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _serve_config(tmp_path, public_url="https://agent.example.invalid/")
    monkeypatch.setattr("telephone_agent.__main__._build_service", lambda _config: object())
    monkeypatch.setattr("telephone_agent.__main__._server_apps", lambda _config, _service: [])

    async def fake_serve(_service: object, _apps: object) -> None:
        """Record nothing; the endpoint lines are the assertion."""

    monkeypatch.setattr("telephone_agent.__main__._serve", fake_serve)

    assert _cmd_serve(argparse.Namespace(config=str(path))) == 0

    output = capsys.readouterr().out
    assert "A2A RPC endpoint: https://agent.example.invalid/a2a" in output
    assert "Agent Card: https://agent.example.invalid/.well-known/agent-card.json" in output
    assert "MCP endpoint: http://127.0.0.1:8765/mcp" in output


def test_main_serve_returns_2_for_blank_token(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _serve_config(tmp_path, token="")

    assert main(["serve", "--config", str(path)]) == 2

    captured = capsys.readouterr()
    assert "server.token is required" in captured.err


def test_build_session_factory_wires_a_deliberative_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Config(
        audio=AudioConfig(bluetooth_address=ADDRESS),
        dialer=DialerConfig(url="http://127.0.0.1:8765", token="test-token"),
        agent=AgentConfig(consult_timeout_s=3.5, holding_phrase="Hold on."),
    )
    fake_audio = object()
    fake_dialer = object()
    fake_brain = object()
    captured: dict[str, object] = {}
    sentinel = object()

    def fake_call_session(**kwargs: object) -> object:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr("telephone_agent.__main__._audio_from_config", lambda _config: fake_audio)
    monkeypatch.setattr("telephone_agent.__main__._dialer_from_config", lambda _config: fake_dialer)
    monkeypatch.setattr(
        "telephone_agent.__main__.build_deliberative_brain",
        lambda _config: fake_brain,
    )
    monkeypatch.setattr("telephone_agent.__main__.CallSession", fake_call_session)

    def on_event(_event: object) -> None:
        """Ignore session events."""

    broker = ConsultationBroker()
    stop_event = threading.Event()
    task = SimpleNamespace(number="+15555550100", max_call_seconds=42.0)

    session = _build_session_factory(config)(
        task=task,
        broker=broker,
        stop_event=stop_event,
        on_event=on_event,
    )

    assert session is sentinel
    assert captured["audio"] is fake_audio
    assert captured["dialer"] is fake_dialer
    assert captured["deliberative"] is fake_brain
    assert "brain" not in captured
    assert captured["consultations"] is broker
    assert captured["number"] == "+15555550100"
    assert captured["max_call_seconds"] == 42.0
    assert captured["silence_timeout_s"] == config.agent.silence_timeout_s
    assert captured["vad_threshold"] == config.agent.vad_threshold
    assert captured["consult_timeout_s"] == 3.5
    assert captured["holding_phrase"] == "Hold on."
    assert captured["on_event"] is on_event
    assert captured["stop_event"] is stop_event


def test_build_session_factory_uses_the_configured_call_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Config(
        audio=AudioConfig(bluetooth_address=ADDRESS),
        dialer=DialerConfig(url="http://127.0.0.1:8765", token="test-token"),
        agent=AgentConfig(max_call_seconds=120),
    )
    captured: dict[str, object] = {}

    def fake_call_session(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("telephone_agent.__main__._audio_from_config", lambda _config: object())
    monkeypatch.setattr("telephone_agent.__main__._dialer_from_config", lambda _config: object())
    monkeypatch.setattr(
        "telephone_agent.__main__.build_deliberative_brain",
        lambda _config: object(),
    )
    monkeypatch.setattr("telephone_agent.__main__.CallSession", fake_call_session)

    _build_session_factory(config)(
        task=SimpleNamespace(number="+15555550100", max_call_seconds=None),
        broker=ConsultationBroker(),
        stop_event=threading.Event(),
        on_event=lambda _event: None,
    )

    assert captured["max_call_seconds"] == 120


def test_build_session_factory_wires_the_realtime_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = Config(
        audio=AudioConfig(bluetooth_address=ADDRESS),
        dialer=DialerConfig(url="http://127.0.0.1:8765", token="test-token"),
        realtime=RealtimeConfig(kind="grok"),
        agent=AgentConfig(consult_timeout_s=3.5, holding_phrase="Hold on."),
    )
    fake_audio = object()
    fake_dialer = object()
    captured: dict[str, object] = {}
    sentinel = object()

    def fake_realtime_session(**kwargs: object) -> object:
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr("telephone_agent.__main__._audio_from_config", lambda _config: fake_audio)
    monkeypatch.setattr("telephone_agent.__main__._dialer_from_config", lambda _config: fake_dialer)
    monkeypatch.setattr(
        "telephone_agent.__main__.GrokRealtimeSession",
        fake_realtime_session,
    )

    def on_event(_event: object) -> None:
        """Ignore session events."""

    broker = ConsultationBroker()
    stop_event = threading.Event()
    task = SimpleNamespace(
        number="+15555550100",
        max_call_seconds=42.0,
        objective="Confirm the 4 pm call.",
    )

    session = _build_session_factory(config)(
        task=task,
        broker=broker,
        stop_event=stop_event,
        on_event=on_event,
    )

    assert session is sentinel
    assert captured["audio"] is fake_audio
    assert captured["dialer"] is fake_dialer
    assert captured["config"] is config.realtime
    assert captured["objective"] == "Confirm the 4 pm call."
    assert captured["consultations"] is broker
    assert captured["number"] == "+15555550100"
    assert captured["max_call_seconds"] == 42.0
    assert captured["silence_timeout_s"] == config.agent.silence_timeout_s
    assert captured["consult_timeout_s"] == 3.5
    assert captured["holding_phrase"] == "Hold on."
    assert captured["on_event"] is on_event
    assert captured["stop_event"] is stop_event


def test_build_session_factory_records_transcripts_and_forwards_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "transcripts"
    config = Config(
        audio=AudioConfig(bluetooth_address=ADDRESS),
        dialer=DialerConfig(url="http://127.0.0.1:8765", token="test-token"),
        transcript=TranscriptConfig(enabled=True, directory=str(directory)),
    )
    captured: dict[str, object] = {}
    service_events: list[object] = []

    def fake_call_session(**kwargs: object) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr("telephone_agent.__main__._audio_from_config", lambda _config: object())
    monkeypatch.setattr("telephone_agent.__main__._dialer_from_config", lambda _config: object())
    monkeypatch.setattr(
        "telephone_agent.__main__.build_deliberative_brain",
        lambda _config: object(),
    )
    monkeypatch.setattr("telephone_agent.__main__.CallSession", fake_call_session)

    task = SimpleNamespace(
        id="call-1234567890",
        number="+15555550100",
        max_call_seconds=None,
        objective="Confirm the appointment.",
    )
    _build_session_factory(config)(
        task=task,
        broker=ConsultationBroker(),
        stop_event=threading.Event(),
        on_event=service_events.append,
    )

    callback = captured["on_event"]
    assert callable(callback)
    events = [
        Utterance("Hello?"),
        AgentReply("Hi, this is the agent."),
        CallFinished(
            CallResult(
                state=CallState.IDLE,
                reason=CallEndReason.COMPLETED,
                duration_s=9.5,
                turns=1,
            )
        ),
    ]
    for event in events:
        callback(event)

    # The service's own callback still receives every event unchanged.
    assert service_events == events

    paths = list(directory.glob("*.jsonl"))
    assert len(paths) == 1
    records = [json.loads(line) for line in paths[0].read_text(encoding="utf-8").splitlines()]
    assert [record["event"] for record in records] == [
        "call_start",
        "transcript",
        "reply",
        "call_end",
    ]
    assert records[0]["call_id"] == "call-1234567890"
    assert records[0]["number"] == "+15555550100"
    assert records[0]["objective"] == "Confirm the appointment."
    assert records[0]["audio_backend"] == "pipewire"
    assert records[0]["realtime_kind"] == "off"
    assert "realtime_model" not in records[0]
    assert records[1]["text"] == "Hello?"
    assert records[2]["text"] == "Hi, this is the agent."
    assert records[3]["reason"] == "completed"


def test_cmd_run_records_transcript_and_passes_the_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    directory = tmp_path / "transcripts"
    path = _write_config(
        tmp_path,
        f"""
phone_number = "+15555550100"

[audio]
bluetooth_address = "AA:BB:CC:DD:EE:FF"

[dialer]
url = "http://127.0.0.1:8765"
token = "test-token"

[realtime]
kind = "grok"
model = "grok-voice-3.0"
voice = "ara"

[transcript]
enabled = true
directory = "{directory}"
""",
    )
    captured: dict[str, object] = {}
    result = CallResult(
        state=CallState.IDLE,
        reason=CallEndReason.COMPLETED,
        duration_s=4.5,
        turns=1,
    )

    def fake_build_session(
        _config: Config,
        _number: str,
        *,
        on_event: object = None,
    ) -> object:
        captured["on_event"] = on_event

        class FakeSession:
            def run(self) -> CallResult:
                on_event(Utterance("hello"))
                on_event(AgentReply("hi"))
                on_event(CallFinished(result))
                return result

        return FakeSession()

    monkeypatch.setattr("telephone_agent.__main__._build_session", fake_build_session)

    assert _cmd_run(argparse.Namespace(config=str(path), number=None, direct=True)) == 0

    assert callable(captured["on_event"])
    paths = list(directory.glob("*.jsonl"))
    assert len(paths) == 1
    records = [json.loads(line) for line in paths[0].read_text(encoding="utf-8").splitlines()]
    assert [record["event"] for record in records] == [
        "call_start",
        "transcript",
        "reply",
        "call_end",
    ]
    assert records[0]["number"] == "+15555550100"
    assert records[0]["audio_backend"] == "pipewire"
    assert records[0]["realtime_kind"] == "grok"
    assert records[0]["realtime_model"] == "grok-voice-3.0"
    assert records[0]["realtime_voice"] == "ara"
    assert "objective" not in records[0]
    captured = capsys.readouterr()
    assert "call ended: state=idle reason=completed turns=1 duration=4.5s" in captured.out
    assert "running one call to *******0100" in captured.out
    assert "+15555550100" not in captured.out + captured.err


def test_inbound_subcommand_parses_and_dispatches(tmp_path: Path) -> None:
    parser = build_parser()

    args = parser.parse_args(["--config", str(tmp_path / "config.toml"), "inbound", "--once"])

    assert args.command == "inbound"
    assert args.config == str(tmp_path / "config.toml")
    assert args.once is True
    assert args.handler is _cmd_inbound


def test_inbound_requires_the_grok_realtime_adapter(tmp_path: Path) -> None:
    path = _write_config(
        tmp_path,
        """
[audio]
bluetooth_address = "AA:BB:CC:DD:EE:FF"

[dialer]
url = "http://127.0.0.1:8765"
token = "test-token"
""",
    )

    with pytest.raises(ConfigError, match="realtime.kind = 'grok'"):
        _cmd_inbound(argparse.Namespace(config=str(path), once=True))


def test_inbound_wires_the_watcher_with_config_and_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _write_config(
        tmp_path,
        """
[audio]
bluetooth_address = "AA:BB:CC:DD:EE:FF"

[dialer]
url = "http://127.0.0.1:8765"
token = "test-token"

[realtime]
kind = "grok"

[inbound]
poll_interval_s = 0.5
answer_delay_s = 1.5
""",
    )
    captured: dict[str, object] = {}

    class FakeWatcher:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

        def run(self) -> int:
            return 1

    monkeypatch.setattr("telephone_agent.__main__.InboundWatcher", FakeWatcher)
    monkeypatch.setattr("telephone_agent.__main__._install_stop_handlers", lambda _event: {})
    monkeypatch.setattr("telephone_agent.__main__._restore_signal_handlers", lambda _prev: None)

    assert _cmd_inbound(argparse.Namespace(config=str(path), once=True)) == 0

    assert isinstance(captured["config"], InboundConfig)
    assert captured["config"].poll_interval_s == 0.5
    assert captured["config"].answer_delay_s == 1.5
    assert captured["once"] is True
    assert callable(captured["session_factory"])
    assert isinstance(captured["stop_event"], threading.Event)
    assert "poll 0.5 s" in capsys.readouterr().out


def test_inbound_session_factory_uses_an_inbound_transcript_and_realtime_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / "transcripts"
    config = Config(
        audio=AudioConfig(bluetooth_address=ADDRESS),
        dialer=DialerConfig(url="http://127.0.0.1:8765", token="test-token"),
        realtime=RealtimeConfig(kind="grok"),
        transcript=TranscriptConfig(enabled=True, directory=str(directory)),
    )
    captured: dict[str, object] = {}
    ran: list[bool] = []

    class FakeSession:
        def run(self) -> None:
            ran.append(True)

    def fake_build(
        built_config: Config,
        audio: object,
        dialer: object,
        number: str,
        **kwargs: object,
    ) -> FakeSession:
        captured["config"] = built_config
        captured["audio"] = audio
        captured["dialer"] = dialer
        captured["number"] = number
        captured.update(kwargs)
        return FakeSession()

    monkeypatch.setattr("telephone_agent.__main__._audio_from_config", lambda _config: object())
    monkeypatch.setattr("telephone_agent.__main__._dialer_from_config", lambda _config: object())
    monkeypatch.setattr("telephone_agent.__main__._build_realtime_session", fake_build)

    stop_event = threading.Event()
    session = _inbound_session_factory(config)(stop_event)
    session.run()

    assert ran == [True]
    assert captured["number"] == ""
    assert captured["place_call"] is False
    assert captured["stop_event"] is stop_event
    assert captured["config"] is config

    paths = list(directory.glob("*.jsonl"))
    assert len(paths) == 1
    records = [json.loads(line) for line in paths[0].read_text(encoding="utf-8").splitlines()]
    assert records[0]["event"] == "call_start"
    assert records[0]["direction"] == "inbound"
    assert records[0]["number"] == ""
    assert records[0]["realtime_kind"] == "grok"
    assert "objective" not in records[0]


def test_doctor_realtime_reports_missing_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _write_config(
        tmp_path,
        """
[audio]
bluetooth_address = "AA:BB:CC:DD:EE:FF"

[dialer]
url = "http://127.0.0.1:8765"
token = "test-token"

[realtime]
kind = "grok"
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
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.delenv("TELEPHONE_AGENT_REALTIME__API_KEY", raising=False)

    assert _cmd_doctor(argparse.Namespace(config=str(path))) == 1

    captured = capsys.readouterr()
    assert "realtime: grok" in captured.out
    assert "api key: FAIL" in captured.err
    assert "doctor: FAIL" in captured.out


def test_doctor_realtime_reports_configured_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _write_config(
        tmp_path,
        """
[audio]
bluetooth_address = "AA:BB:CC:DD:EE:FF"

[dialer]
url = "http://127.0.0.1:8765"
token = "test-token"

[realtime]
kind = "grok"
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
    monkeypatch.setenv("XAI_API_KEY", "test-key")

    assert _cmd_doctor(argparse.Namespace(config=str(path))) == 0

    captured = capsys.readouterr()
    assert "realtime: grok" in captured.out
    assert "api key: configured" in captured.out
    assert "test-key" not in captured.out
    assert "doctor: ok" in captured.out


def test_serve_stops_every_listener_when_the_first_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uvicorn

    created: list[object] = []

    class FakeConfig:
        def __init__(self, app: object, *, host: str, port: int, log_level: str) -> None:
            self.app = app
            self.host = host
            self.port = port
            self.log_level = log_level

    class FakeServer:
        def __init__(self, config: FakeConfig) -> None:
            self.config = config
            self.should_exit = False
            self.started = asyncio.Event()
            self.stopped = asyncio.Event()
            self.finish = asyncio.Event()
            created.append(self)

        async def serve(self) -> None:
            self.started.set()
            while not self.should_exit and not self.finish.is_set():
                await asyncio.sleep(0.01)
            self.stopped.set()

    class FakeService:
        def __init__(self) -> None:
            self.start_calls = 0
            self.stop_calls = 0

        async def start(self) -> None:
            self.start_calls += 1

        async def stop(self) -> None:
            self.stop_calls += 1

    monkeypatch.setattr(uvicorn, "Config", FakeConfig)
    monkeypatch.setattr(uvicorn, "Server", FakeServer)

    async def scenario() -> None:
        service = FakeService()
        apps = [(object(), "127.0.0.1", 9101), (object(), "127.0.0.1", 9102)]
        runner = asyncio.create_task(_serve(service, apps))

        for _ in range(200):
            if len(created) == 2:
                break
            await asyncio.sleep(0.01)
        assert len(created) == 2

        first = created[0]
        second = created[1]
        assert isinstance(first, FakeServer)
        assert isinstance(second, FakeServer)
        assert [server.config.port for server in (first, second)] == [9101, 9102]
        assert await asyncio.wait_for(first.started.wait(), timeout=1.0)
        assert await asyncio.wait_for(second.started.wait(), timeout=1.0)

        first.finish.set()
        await asyncio.wait_for(runner, timeout=2.0)

        assert service.start_calls == 1
        assert service.stop_calls == 1
        assert first.should_exit is True
        assert second.should_exit is True
        assert first.stopped.is_set()
        assert second.stopped.is_set()

    asyncio.run(scenario())


def test_serve_stops_the_service_when_startup_fails() -> None:
    class FailingService:
        def __init__(self) -> None:
            self.stop_calls = 0

        async def start(self) -> None:
            raise RuntimeError("worker failed")

        async def stop(self) -> None:
            self.stop_calls += 1

    service = FailingService()

    with pytest.raises(RuntimeError, match="worker failed"):
        asyncio.run(_serve(service, []))  # type: ignore[arg-type]

    assert service.stop_calls == 1


class _FakeDaemon:
    """Accept one connection, record the request, and send one response."""

    def __init__(self, path: Path, response: dict[str, object]) -> None:
        self.path = path
        self.response = response
        self.requests: list[dict[str, object]] = []
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(path))
        self._listener.listen(1)
        self._listener.settimeout(5.0)
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self) -> _FakeDaemon:
        self._thread.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self._listener.close()
        self._thread.join(2.0)

    def _serve(self) -> None:
        try:
            connection, _address = self._listener.accept()
        except OSError:
            return
        with connection:
            buffer = bytearray()
            while b"\n" not in buffer:
                chunk = connection.recv(4096)
                if not chunk:
                    return
                buffer.extend(chunk)
            line, _, _rest = bytes(buffer).partition(b"\n")
            self.requests.append(json.loads(line.decode("utf-8")))
            connection.sendall(json.dumps(self.response).encode("utf-8") + b"\n")


def _daemon_config(tmp_path: Path, socket_path: Path) -> Path:
    return _write_config(
        tmp_path,
        f"""
phone_number = "+15555550100"

[audio]
bluetooth_address = "AA:BB:CC:DD:EE:FF"

[dialer]
url = "http://127.0.0.1:8765"
token = "test-token"

[daemon]
socket = "{socket_path}"
""",
    )


def test_run_client_sends_a_call_to_the_daemon(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    socket_path = tmp_path / "daemon.sock"
    path = _daemon_config(tmp_path, socket_path)
    response = {
        "ok": True,
        "result": {
            "state": "idle",
            "reason": "completed",
            "turns": 2,
            "duration_s": 3.25,
        },
    }

    with _FakeDaemon(socket_path, response) as fake:
        assert _cmd_run(argparse.Namespace(config=str(path), number=None, direct=False)) == 0

    assert fake.requests == [
        {
            "command": "call",
            "number": "+15555550100",
            "objective": "",
            "max_call_seconds": 300,
        }
    ]
    captured = capsys.readouterr()
    assert "call ended: state=idle reason=completed turns=2 duration=3.2s" in captured.out
    assert "+15555550100" not in captured.out + captured.err


def test_run_client_reports_an_unavailable_daemon(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _daemon_config(tmp_path, tmp_path / "missing.sock")

    assert _cmd_run(argparse.Namespace(config=str(path), number=None, direct=False)) == 1

    captured = capsys.readouterr()
    assert "telephone-agent-daemon.service" in captured.err
    assert "--direct" in captured.err
    assert "no daemon is listening" in captured.err


def test_run_client_returns_1_after_an_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _daemon_config(tmp_path, tmp_path / "daemon.sock")

    class FakeClient:
        def __init__(self, _path: object) -> None:
            pass

        def call(self, *_args: object, **_kwargs: object) -> CallReply:
            return CallReply(
                {
                    "ok": True,
                    "result": {
                        "state": "idle",
                        "reason": "interrupted",
                        "turns": 0,
                        "duration_s": 0.4,
                    },
                },
                interrupted=True,
            )

    monkeypatch.setattr("telephone_agent.__main__.DaemonClient", FakeClient)

    assert _cmd_run(argparse.Namespace(config=str(path), number=None, direct=False)) == 1
    assert "call ended: state=idle reason=interrupted" in capsys.readouterr().out


def test_run_direct_flag_is_parsed(tmp_path: Path) -> None:
    args = build_parser().parse_args(["--config", str(tmp_path / "config.toml"), "run", "--direct"])

    assert args.command == "run"
    assert args.direct is True
    assert args.handler is _cmd_run


def test_daemon_subcommand_parses_and_dispatches(tmp_path: Path) -> None:
    args = build_parser().parse_args(["--config", str(tmp_path / "config.toml"), "daemon"])

    assert args.command == "daemon"
    assert args.config == str(tmp_path / "config.toml")
    assert args.handler is _cmd_daemon


def test_daemon_requires_the_grok_realtime_adapter(tmp_path: Path) -> None:
    path = _daemon_config(tmp_path, tmp_path / "daemon.sock")

    with pytest.raises(ConfigError, match="daemon requires realtime.kind = 'grok'"):
        _cmd_daemon(argparse.Namespace(config=str(path)))


def test_daemon_starts_the_executor_server_and_watcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = _write_config(
        tmp_path,
        """
[audio]
bluetooth_address = "AA:BB:CC:DD:EE:FF"

[dialer]
url = "http://127.0.0.1:8765"
token = "test-token"

[realtime]
kind = "grok"

[daemon]
socket = "/tmp/test-telephone-agent.sock"
""",
    )
    calls: dict[str, object] = {}

    class FakeExecutor:
        def __init__(self, **kwargs: object) -> None:
            calls["executor_kwargs"] = kwargs
            self.busy = False

        def start(self) -> None:
            calls["executor_started"] = True

        def stop(self) -> None:
            calls["executor_stopped"] = True

        def hangup(self) -> bool:
            calls["hungup"] = True
            return True

        def enqueue(self, _request: object) -> object:
            raise AssertionError("the watcher factory must not run without a call")

    class FakeServer:
        def __init__(self, socket_path: object, _executor: object) -> None:
            self.path = socket_path

        def start(self) -> None:
            calls["server_started"] = True

        def stop(self) -> None:
            calls["server_stopped"] = True

    class FakeWatcher:
        def __init__(self, **kwargs: object) -> None:
            calls.update(kwargs)

        def run(self) -> int:
            calls["watcher_ran"] = True
            return 3

    monkeypatch.setattr("telephone_agent.__main__.CallExecutor", FakeExecutor)
    monkeypatch.setattr("telephone_agent.__main__.ControlServer", FakeServer)
    monkeypatch.setattr("telephone_agent.__main__.InboundWatcher", FakeWatcher)
    monkeypatch.setattr("telephone_agent.__main__._install_stop_handlers", lambda _event: {})
    monkeypatch.setattr(
        "telephone_agent.__main__._restore_signal_handlers",
        lambda _previous: calls.setdefault("handlers_restored", True),
    )

    assert _cmd_daemon(argparse.Namespace(config=str(path))) == 0

    assert calls["executor_started"] is True
    assert calls["server_started"] is True
    assert calls["watcher_ran"] is True
    assert calls["server_stopped"] is True
    assert calls["executor_stopped"] is True
    assert calls["hungup"] is True
    assert calls["handlers_restored"] is True
    assert callable(calls["can_accept"])
    assert callable(calls["session_factory"])
    assert isinstance(calls["stop_event"], threading.Event)
    output = capsys.readouterr().out
    assert "socket: /tmp/test-telephone-agent.sock" in output
    assert "serving inbound and outbound calls" in output
    assert "daemon stopped after 3 inbound call(s)" in output
