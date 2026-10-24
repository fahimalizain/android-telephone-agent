"""Command-line entry point for the telephone agent.

Exit codes follow the repository contract:

* ``0``: success
* ``1``: runtime failure (helper unreachable, audio device missing, call failed)
* ``2``: configuration or usage error
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from . import __version__
from .audio import AudioError, GadgetAudio
from .brain import BrainError, build_brain
from .config import Config, ConfigError, ensure_e164, load
from .dialer import Dialer, DialerError, mask_number
from .pipeline import CallEndReason, CallSession, CallState

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = "config/config.toml"


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""

    parser = argparse.ArgumentParser(
        prog="telephone-agent",
        description=(
            "Reference runtime for an outbound telephone voice agent. "
            "The agent runs on a Raspberry Pi and reaches Phone A through a "
            "LAN helper app."
        ),
        epilog=(
            "examples:\n"
            "  telephone-agent --config config/config.toml doctor\n"
            "  telephone-agent --config config/config.toml selftest\n"
            "  telephone-agent --config config/config.toml dial --number +15555550100\n"
            "  telephone-agent --config config/config.toml run"
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
        metavar="{doctor,dial,run,selftest}",
    )

    doctor = subparsers.add_parser(
        "doctor",
        help="check configuration, capture devices, and the dial helper",
        description=(
            "Load the configuration, list ALSA capture devices, verify that the "
            "configured card exists, and call the helper's health endpoint."
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
            "Place one call and run the v0 conversation loop. STT and TTS "
            "adapters are not bundled; see agent/README.md for the adapter seams."
        ),
    )
    run.add_argument(
        "--number",
        metavar="E164",
        help="destination in E.164 format; overrides phone_number in the config",
    )
    run.set_defaults(handler=_cmd_run)

    selftest = subparsers.add_parser(
        "selftest",
        help="record two seconds from the gadget and report RMS",
        description=(
            "Record two seconds from the configured gadget card and print frame "
            "count, duration, RMS, dBFS, and peak amplitude. This check does not "
            "place a call."
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
    except (AudioError, BrainError, DialerError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception:
        logger.exception("unexpected error")
        return 1


def _cmd_doctor(args: argparse.Namespace) -> int:
    config = load(Path(args.config))
    failed = False

    print(f"config: {Path(args.config)}")
    print(f"audio: {config.audio.card} ({_alsa_device(config)})")
    print(
        f"format: {config.audio.sample_rate} Hz, {config.audio.channels} channel(s), "
        f"{config.audio.frame_ms} ms frames"
    )
    if config.phone_number:
        print(f"phone: {mask_number(config.phone_number)}")
    else:
        print("phone: not set (pass --number to dial/run)")
    print(f"dialer: {config.dialer.url} (bearer token configured)")
    print(f"brain: {config.brain.kind}")
    if config.brain.kind == "http":
        print(f"  base_url: {config.brain.base_url}")
        print(f"  model: {config.brain.model}")

    print("capture devices:")
    try:
        devices = GadgetAudio.list_cards()
    except AudioError as exc:
        print(f"  FAIL: {exc}")
        failed = True
    else:
        if not devices:
            print("  none reported by `arecord -l`")
            failed = True
        else:
            for device in devices:
                print(f"  {device}")
            if not any(device.card_id == config.audio.card for device in devices):
                print(
                    f"  FAIL: configured card {config.audio.card!r} is not in `arecord -l`",
                    file=sys.stderr,
                )
                failed = True

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
    print(f"recording 2.0 s from {audio.alsa_device} ...")
    stats = audio.selftest()
    print(f"frames: {stats.frames}")
    print(f"duration: {stats.duration_s:.2f} s")
    print(f"rms: {stats.rms:.1f} ({stats.dbfs:.1f} dBFS)")
    print(f"peak: {stats.peak}")
    if stats.rms == 0:
        print(
            "warning: captured digital silence; check the USB gadget and the "
            "phone's USB audio routing",
            file=sys.stderr,
        )
        return 1
    return 0


def _build_session(config: Config, number: str) -> CallSession:
    """Create a call session.

    This is the integration point for real speech adapters. v0 does not bundle
    STT or TTS. Once you have implementations of
    :class:`telephone_agent.pipeline.Transcriber` and
    :class:`telephone_agent.pipeline.Synthesizer`, construct them here and pass
    ``transcriber=`` and ``synthesizer=`` to :class:`CallSession`. See
    ``agent/README.md`` for the expected PCM format and streaming notes.
    """

    audio = _audio_from_config(config)
    dialer = _dialer_from_config(config.dialer)
    brain = build_brain(config.brain)
    return CallSession(
        audio=audio,
        dialer=dialer,
        brain=brain,
        number=number,
        max_call_seconds=config.agent.max_call_seconds,
        silence_timeout_s=config.agent.silence_timeout_s,
        vad_threshold=config.agent.vad_threshold,
        # transcriber=MyTranscriber(),  # v0: not bundled
        # synthesizer=MySynthesizer(),  # v0: not bundled
    )


def _audio_from_config(config: Config) -> GadgetAudio:
    """Build the audio device from configuration."""

    return GadgetAudio(
        card=config.audio.card,
        device=config.audio.device,
        sample_rate=config.audio.sample_rate,
        channels=config.audio.channels,
        frame_ms=config.audio.frame_ms,
    )


def _dialer_from_config(config: Config) -> Dialer:
    """Build the dial helper client from configuration."""

    return Dialer(
        url=config.dialer.url,
        token=config.dialer.token,
        timeout_s=config.dialer.timeout_s,
        retries=config.dialer.retries,
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
