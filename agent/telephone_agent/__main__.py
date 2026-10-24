"""Command-line entry point for the telephone agent.

Exit codes follow the repository contract:

* ``0``: success
* ``1``: runtime failure (helper unreachable, audio device missing, call failed)
* ``2``: configuration or usage error
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import logging
import shutil
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from . import __version__
from .audio import AudioDevice, AudioError, GadgetAudio, PipeWireAudio
from .brain import BrainError, build_brain, build_deliberative_brain
from .config import Config, ConfigError, DialerConfig, ensure_e164, load
from .consult import ConsultationBroker
from .dialer import Dialer, DialerError, mask_number
from .grok_realtime import (
    GrokRealtimeError,
    GrokRealtimeSession,
    resolve_api_key,
)
from .pipeline import CallEndReason, CallSession, CallState, SessionEvent
from .service import CallService, CallTask, SessionFactory

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "config/config.toml"


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""

    parser = argparse.ArgumentParser(
        prog="telephone-agent",
        description=(
            "Reference runtime for an outbound telephone voice agent. "
            "The agent runs on a small Linux host and reaches Phone A through a "
            "USB audio gadget or a Bluetooth HFP headset link, plus a LAN helper app."
        ),
        epilog=(
            "examples:\n"
            "  telephone-agent --config config/config.toml doctor\n"
            "  telephone-agent --config config/config.toml selftest\n"
            "  telephone-agent --config config/config.toml dial --number +15555550100\n"
            "  telephone-agent --config config/config.toml run\n"
            "  telephone-agent --config config/config.toml serve"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_CONFIG_PATH,
        metavar="PATH",
        help=f"TOML configuration file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
        metavar="{doctor,dial,run,serve,selftest}",
    )

    doctor = subparsers.add_parser(
        "doctor",
        help="check configuration, the audio path, and the dial helper",
        description=(
            "Load the configuration, check the selected audio transport (ALSA "
            "gadget or PipeWire Bluetooth HFP), and call the helper's health endpoint."
        ),
    )
    doctor.set_defaults(handler=_cmd_doctor)

    dial = subparsers.add_parser(
        "dial",
        help="ask the helper to place one call and exit",
        description=(
            "Place a call without starting the conversation loop. The helper "
            "owns the call afterwards."
        ),
    )
    dial.add_argument(
        "--number",
        metavar="E164",
        help="destination in E.164 format; overrides phone_number in the config",
    )
    dial.set_defaults(handler=_cmd_dial)

    run = subparsers.add_parser(
        "run",
        help="place a call and run the conversation loop until hangup",
        description=(
            "Place one call and run the conversation loop. The default path uses "
            "the text pipeline; set realtime.kind = 'grok' for the full-duplex "
            "Grok Voice speech-to-speech adapter."
        ),
    )
    run.add_argument(
        "--number",
        metavar="E164",
        help="destination in E.164 format; overrides phone_number in the config",
    )
    run.set_defaults(handler=_cmd_run)

    serve = subparsers.add_parser(
        "serve",
        help="serve MCP and A2A endpoints for other agents (one call at a time)",
        description=(
            "Run one long-lived CallService and expose it to other agents over "
            "MCP (Streamable HTTP) and A2A (JSON-RPC). Both listeners share one "
            "queue and place at most one call at a time. Requires the optional "
            "server extra and [server].token."
        ),
    )
    serve.add_argument(
        "--config",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help="TOML configuration file; overrides the global --config",
    )
    serve.set_defaults(handler=_cmd_serve)

    selftest = subparsers.add_parser(
        "selftest",
        help="record two seconds from the call downlink and report RMS",
        description=(
            "Record two seconds from the configured call downlink and print frame "
            "count, duration, RMS, dBFS, and peak amplitude. This check does not "
            "place a call. With the Bluetooth backend, the SCO link exists only "
            "during a call, so place a call first."
        ),
    )
    selftest.set_defaults(handler=_cmd_selftest)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, dispatch a subcommand, and map errors to exit codes."""

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args.handler

    try:
        return handler(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 1
    except (AudioError, BrainError, DialerError, GrokRealtimeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception:
        logger.exception("unexpected error")
        return 1


def _cmd_doctor(args: argparse.Namespace) -> int:
    config = load(Path(args.config))
    failed = False

    print(f"config: {Path(args.config)}")
    print(f"audio backend: {config.audio.backend}")
    print(
        f"format: {config.audio.sample_rate} Hz, {config.audio.channels} channel(s), "
        f"{config.audio.frame_ms} ms frames"
    )
    if config.audio.backend == "alsa":
        print(f"audio: {config.audio.card} ({_alsa_device(config)})")
        if not _check_alsa_capture(config.audio.card):
            failed = True
    else:
        print(
            f"audio: Bluetooth HFP {config.audio.bluetooth_address} (PipeWire SCO), "
            f"node timeout {config.audio.node_timeout_s:g} s"
        )
        if not _check_pipewire_tools():
            failed = True
        try:
            capture, playback = PipeWireAudio.find_sco_nodes(
                config.audio.bluetooth_address,
                pw_dump="pw-dump",
            )
        except AudioError as exc:
            print(f"audio: FAIL: {exc}", file=sys.stderr)
            failed = True
        else:
            if capture is not None and playback is not None:
                print(f"  downlink: {capture}")
                print(f"  uplink:   {playback}")
            elif capture is None and playback is None:
                print(
                    "  SCO nodes: not present (idle; place a call to test the Bluetooth audio path)"
                )
            else:
                downlink = "present" if capture is not None else "missing"
                uplink = "present" if playback is not None else "missing"
                print(
                    f"  SCO nodes: incomplete (downlink={downlink}, uplink={uplink}); "
                    "a call may be starting or the HFP link may be unhealthy"
                )
                failed = True

    if config.phone_number:
        print(f"phone: {mask_number(config.phone_number)}")
    else:
        print("phone: not set (pass --number to dial/run)")
    print(f"dialer: {config.dialer.url} (bearer token configured)")
    if config.realtime.kind == "grok":
        print("realtime: grok (speech-to-speech; brain and STT/TTS adapters are unused)")
        print(f"  model: {config.realtime.model}")
        print(f"  voice: {config.realtime.voice}")
        print(f"  url: {config.realtime.base_url}")
        if resolve_api_key(config.realtime):
            print("  api key: configured")
        else:
            print(
                "  api key: FAIL: set XAI_API_KEY or TELEPHONE_AGENT_REALTIME__API_KEY",
                file=sys.stderr,
            )
            failed = True
        if importlib.util.find_spec("websockets") is None:
            print(
                "  dependency: FAIL: websockets is not installed; run "
                "`pip install -e '.[realtime]'` in the agent directory",
                file=sys.stderr,
            )
            failed = True
        else:
            print("  dependency: ok (websockets)")
    else:
        print("realtime: off")
    print(f"brain: {config.brain.kind}")
    if config.brain.kind == "http":
        print(f"  base_url: {config.brain.base_url}")
        print(f"  model: {config.brain.model}")

    try:
        health = _dialer_from_config(config.dialer).health()
    except (DialerError, ValueError) as exc:
        print(f"dial helper: FAIL: {exc}", file=sys.stderr)
        failed = True
    else:
        print("dial helper: ok")
        status = health.get("status")
        if isinstance(status, str):
            print(f"  status: {status}")
        version = health.get("version")
        if isinstance(version, str):
            print(f"  version: {version}")

    print(f"doctor: {'FAIL' if failed else 'ok'}")
    return 1 if failed else 0


def _check_alsa_capture(card: str) -> bool:
    """List ALSA capture devices and verify that *card* is present."""

    print("capture devices:")
    try:
        devices = GadgetAudio.list_cards()
    except AudioError as exc:
        print(f"  FAIL: {exc}")
        return False
    if not devices:
        print("  none reported by `arecord -l`")
        return False
    for device in devices:
        print(f"  {device}")
    if not any(device.card_id == card for device in devices):
        print(
            f"  FAIL: configured card {card!r} is not in `arecord -l`",
            file=sys.stderr,
        )
        return False
    return True


def _check_pipewire_tools() -> bool:
    """Verify that the PipeWire command-line tools used by the backend exist."""

    missing = [tool for tool in ("pw-record", "pw-play", "pw-dump") if shutil.which(tool) is None]
    if missing:
        print(
            f"audio tools: FAIL: missing {', '.join(missing)}; install the pipewire-bin package",
            file=sys.stderr,
        )
        return False
    print("audio tools: ok (pw-record, pw-play, pw-dump)")
    return True


def _cmd_dial(args: argparse.Namespace) -> int:
    config = load(Path(args.config))
    number = _resolve_number(config, args.number)
    dialer = _dialer_from_config(config.dialer)
    result = dialer.place_call(number)
    print(f"call placed to {mask_number(number)}")
    for key in ("call_id", "status"):
        value = result.get(key)
        if isinstance(value, str):
            print(f"{key}: {value}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    config = load(Path(args.config))
    number = _resolve_number(config, args.number)
    session = _build_session(config, number)
    print(f"running one call to {mask_number(number)}")
    result = session.run()
    print(
        f"call ended: state={result.state.value} reason={result.reason.value} "
        f"turns={result.turns} duration={result.duration_s:.1f}s"
    )
    if result.state is CallState.FAILED or result.reason is CallEndReason.INTERRUPTED:
        return 1
    return 0


def _cmd_selftest(args: argparse.Namespace) -> int:
    config = load(Path(args.config))
    audio = _audio_from_config(config)
    print(f"recording 2.0 s from {audio.description} ...")
    if config.audio.backend == "pipewire":
        print(
            "note: Bluetooth HFP SCO nodes exist only during a call; "
            f"waiting up to {config.audio.node_timeout_s:g} s for the link"
        )
    stats = audio.selftest()
    print(f"frames: {stats.frames}")
    print(f"duration: {stats.duration_s:.2f} s")
    print(f"rms: {stats.rms:.1f} ({stats.dbfs:.1f} dBFS)")
    print(f"peak: {stats.peak}")
    if stats.rms == 0:
        if config.audio.backend == "pipewire":
            print(
                "warning: captured digital silence; check that a call is active, "
                "the phone is routing call audio to Bluetooth, and the SCO link "
                "is up (`scripts/bluetooth-check.sh <mac>`)",
                file=sys.stderr,
            )
        else:
            print(
                "warning: captured digital silence; check the USB gadget and the "
                "phone's USB audio routing",
                file=sys.stderr,
            )
        return 1
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    config = load(Path(args.config))
    if not config.server.token.strip():
        raise ConfigError(
            "server.token is required to run serve; set [server].token or "
            "TELEPHONE_AGENT_SERVER__TOKEN"
        )
    if config.server.host not in {"127.0.0.1", "localhost", "::1"} and not config.server.public_url:
        raise ConfigError(
            "server.public_url is required when server.host is not loopback, because "
            "the A2A Agent Card must advertise a reachable URL"
        )

    service = _build_service(config)
    apps = _server_apps(config, service)

    mcp_base = f"http://{config.server.host}:{config.server.port}"
    a2a_base = (
        config.server.public_url or f"http://{config.server.host}:{config.server.a2a_port}"
    ).rstrip("/")
    print(f"MCP endpoint: {mcp_base}/mcp")
    print(f"A2A RPC endpoint: {a2a_base}/a2a")
    print(f"Agent Card: {a2a_base}/.well-known/agent-card.json")
    print("auth: bearer token required")

    asyncio.run(_serve(service, apps))
    return 0


def _build_service(config: Config) -> CallService:
    """Build the long-lived service shared by both protocol listeners."""

    return CallService(
        session_factory=_build_session_factory(config),
        default_number=config.phone_number,
        audit_path=config.server.audit_log or None,
    )


def _build_session_factory(config: Config) -> SessionFactory:
    """Return a factory that builds one deliberative session per delegated call.

    The service calls the factory from a worker thread with keyword arguments.
    Each call gets a fresh audio device, dialer, and brain so one call cannot
    leak state into the next.
    """

    def build(
        *,
        task: CallTask,
        broker: ConsultationBroker,
        stop_event: threading.Event,
        on_event: Callable[[SessionEvent], None],
    ) -> CallSession | GrokRealtimeSession:
        audio = _audio_from_config(config)
        dialer = _dialer_from_config(config.dialer)
        limit = (
            task.max_call_seconds
            if task.max_call_seconds is not None
            else config.agent.max_call_seconds
        )
        if config.realtime.kind == "grok":
            return _build_realtime_session(
                config,
                audio,
                dialer,
                task.number,
                objective=task.objective,
                broker=broker,
                stop_event=stop_event,
                on_event=on_event,
                max_call_seconds=limit,
            )
        brain = build_deliberative_brain(config.brain)
        return CallSession(
            audio=audio,
            dialer=dialer,
            deliberative=brain,
            consultations=broker,
            number=task.number,
            max_call_seconds=limit,
            silence_timeout_s=config.agent.silence_timeout_s,
            vad_threshold=config.agent.vad_threshold,
            consult_timeout_s=config.agent.consult_timeout_s,
            holding_phrase=config.agent.holding_phrase,
            on_event=on_event,
            stop_event=stop_event,
        )

    return build


def _server_apps(config: Config, service: CallService) -> list[tuple[object, str, int]]:
    """Build the MCP and A2A applications and their bind addresses.

    The imports are local so ``--help``, ``doctor``, ``dial``, ``run``, and
    ``selftest`` keep working when the optional ``server`` extra is absent.
    """

    try:
        from .servers.a2a import create_a2a_app
        from .servers.mcp import create_mcp_app
    except ImportError as exc:
        raise ConfigError(
            "server dependencies are not installed; run "
            "`pip install -e '.[server]'` in the agent directory"
        ) from exc

    mcp_app = create_mcp_app(
        service,
        host=config.server.host,
        token=config.server.token,
        public_url=config.server.public_url,
    )
    a2a_app = create_a2a_app(
        service,
        host=config.server.host,
        port=config.server.a2a_port,
        token=config.server.token,
        public_url=config.server.public_url,
    )
    return [
        (mcp_app, config.server.host, config.server.port),
        (a2a_app, config.server.host, config.server.a2a_port),
    ]


async def _serve(service: CallService, apps: Sequence[tuple[object, str, int]]) -> None:
    """Run the service and every protocol app until the first listener exits.

    Uvicorn owns signal handling. When one listener stops, for any reason, the
    others are told to exit so the process shuts down cleanly on both ports.
    The service is stopped even if a listener fails before it starts.
    """

    import uvicorn

    servers: list[Any] = []
    tasks: list[asyncio.Task[None]] = []
    try:
        await service.start()
        for app, host, port in apps:
            server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="info"))
            servers.append(server)
            tasks.append(asyncio.create_task(server.serve()))
        if tasks:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for server in servers:
            server.should_exit = True
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await service.stop()


def _build_session(config: Config, number: str) -> CallSession | GrokRealtimeSession:
    """Create the call session selected by the configuration.

    ``realtime.kind = "grok"`` returns the independent speech-to-speech
    adapter. The default path keeps the text pipeline and its injected speech
    adapter seams.
    """

    audio = _audio_from_config(config)
    dialer = _dialer_from_config(config.dialer)
    if config.realtime.kind == "grok":
        return _build_realtime_session(config, audio, dialer, number)
    brain = build_brain(config.brain)
    return CallSession(
        audio=audio,
        dialer=dialer,
        brain=brain,
        number=number,
        max_call_seconds=config.agent.max_call_seconds,
        silence_timeout_s=config.agent.silence_timeout_s,
        vad_threshold=config.agent.vad_threshold,
        # transcriber=MyTranscriber(),  # not bundled; see agent/README.md
        # synthesizer=MySynthesizer(),  # not bundled; see agent/README.md
    )


def _build_realtime_session(
    config: Config,
    audio: AudioDevice,
    dialer: Dialer,
    number: str,
    *,
    objective: str = "",
    broker: ConsultationBroker | None = None,
    stop_event: threading.Event | None = None,
    on_event: Callable[[SessionEvent], None] | None = None,
    max_call_seconds: float | None = None,
) -> GrokRealtimeSession:
    """Build the Grok Voice adapter without touching the text pipeline."""

    return GrokRealtimeSession(
        audio=audio,
        dialer=dialer,
        number=number,
        config=config.realtime,
        objective=objective,
        consultations=broker,
        max_call_seconds=(
            max_call_seconds if max_call_seconds is not None else config.agent.max_call_seconds
        ),
        silence_timeout_s=config.agent.silence_timeout_s,
        consult_timeout_s=config.agent.consult_timeout_s,
        holding_phrase=config.agent.holding_phrase,
        on_event=on_event,
        stop_event=stop_event,
    )


def _audio_from_config(config: Config) -> AudioDevice:
    """Build the audio device from configuration."""

    if config.audio.backend == "pipewire":
        return PipeWireAudio(
            bluetooth_address=config.audio.bluetooth_address,
            sample_rate=config.audio.sample_rate,
            channels=config.audio.channels,
            frame_ms=config.audio.frame_ms,
            node_timeout_s=config.audio.node_timeout_s,
        )
    return GadgetAudio(
        card=config.audio.card,
        device=config.audio.device,
        sample_rate=config.audio.sample_rate,
        channels=config.audio.channels,
        frame_ms=config.audio.frame_ms,
    )


def _dialer_from_config(config: DialerConfig) -> Dialer:
    """Build the dial helper client from the ``[dialer]`` section."""

    return Dialer(
        url=config.url,
        token=config.token,
        timeout_s=config.timeout_s,
        retries=config.retries,
    )


def _resolve_number(config: Config, override: str | None) -> str:
    number = override or config.phone_number
    if not number:
        raise ConfigError(
            "destination number is required; pass --number +E164 or set phone_number in the config"
        )
    return ensure_e164(number)


def _alsa_device(config: Config) -> str:
    return f"plughw:CARD={config.audio.card},DEV={config.audio.device}"


if __name__ == "__main__":
    raise SystemExit(main())
