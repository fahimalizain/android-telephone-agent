"""Configuration loading and validation for the telephone agent.

Settings live in a TOML file. The dataclass defaults below are the lowest
precedence, TOML values override them, and ``TELEPHONE_AGENT_*`` environment
variables override both. Nested overrides use a double underscore::

    TELEPHONE_AGENT_DIALER__TOKEN=...
    TELEPHONE_AGENT_AUDIO__SAMPLE_RATE=48000

The loader is intentionally strict. Unknown keys and unknown environment
variables are errors rather than silent no-ops, because a typo in a token or a
card name is otherwise difficult to diagnose. Validation of sample rates, frame
alignment, URLs, and E.164 numbers happens here, before a call is placed.
"""

from __future__ import annotations

import math
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlsplit

__all__ = [
    "AgentConfig",
    "AudioConfig",
    "BrainConfig",
    "Config",
    "ConfigError",
    "DialerConfig",
    "RealtimeConfig",
    "ServerConfig",
    "TranscriptConfig",
    "ensure_e164",
    "load",
    "validate",
]

_ENV_PREFIX = "TELEPHONE_AGENT_"
_E164_PATTERN = re.compile(r"^\+[1-9]\d{6,14}$")
_CARD_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_BLUETOOTH_ADDRESS = re.compile(r"^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$")

_T = TypeVar("_T")


class ConfigError(ValueError):
    """Raised when a configuration file or environment override is invalid."""


@dataclass(frozen=True, slots=True)
class AudioConfig:
    """Audio transport and processing settings.

    ``backend = "alsa"`` selects the USB Audio Class gadget and requires
    ``card``/``device``. ``backend = "pipewire"`` selects Bluetooth HFP through
    PipeWire and requires ``bluetooth_address``; the SCO nodes are present only
    while a call is active, so ``node_timeout_s`` bounds the wait for them.

    For the USB gadget, ``card`` is the ALSA card id shown by ``arecord -l``
    (the token after ``card N:``), not the bracketed human-readable description.
    The gadget itself runs at 48 kHz stereo; the default here is 16 kHz mono
    because ALSA's ``plughw`` plugin resamples and the network codec cannot use
    the extra bandwidth. The Bluetooth HFP SCO link is 8 kHz (CVSD) or 16 kHz
    (mSBC); the default processing rate matches the wideband case.
    """

    card: str = ""
    device: int = 0
    sample_rate: int = 16_000
    channels: int = 1
    frame_ms: int = 20
    backend: str = "alsa"
    bluetooth_address: str = ""
    node_timeout_s: float = 30.0


@dataclass(frozen=True, slots=True)
class DialerConfig:
    """Settings for the HTTP helper that places and ends calls on Phone A."""

    url: str = ""
    token: str = field(default="", repr=False)
    timeout_s: float = 10.0
    retries: int = 2


@dataclass(frozen=True, slots=True)
class BrainConfig:
    """Settings for the conversation brain.

    ``kind = "echo"`` is a loopback aid and ignores the HTTP fields.
    ``kind = "http"`` targets any OpenAI-compatible ``/chat/completions`` API.
    ``api_key`` may be empty for a local OpenAI-compatible server.

    ``action_mode = "text"`` wraps a plain text-in, text-out brain in
    ``BrainActionAdapter``. ``action_mode = "json"`` asks an HTTP model for one
    JSON action per turn, which lets it consult the requester mid-call or hang
    up; it requires ``kind = "http"``.
    """

    kind: str = "echo"
    base_url: str = ""
    api_key: str = field(default="", repr=False)
    model: str = ""
    system_prompt: str = ""
    max_tokens: int = 200
    timeout_s: float = 30.0
    action_mode: str = "text"


@dataclass(frozen=True, slots=True)
class RealtimeConfig:
    """Settings for a full-duplex speech-to-speech adapter.

    ``kind = "off"`` leaves the existing text pipeline in charge.
    ``kind = "grok"`` selects xAI's Grok Voice realtime API, which owns speech
    recognition, the conversation model, turn detection, and speech synthesis in
    one WebSocket session. The adapter streams the call downlink as PCM and plays
    the returned PCM back into the uplink; it does not use
    :class:`~telephone_agent.pipeline.Transcriber` or
    :class:`~telephone_agent.pipeline.Synthesizer`.

    ``api_key`` may be empty. The adapter first uses this value, then the
    ``XAI_API_KEY`` environment variable, so the key never has to live in the
    repository or the TOML file. ``base_url`` is the WebSocket endpoint without
    the model query parameter.

    ``greeting`` is an optional opening line. The adapter requests it once,
    after the call audio backend is ready, so a scheduled outbound call does not
    wait for the callee to speak first. An empty value preserves the
    listen-first behavior.
    """

    kind: str = "off"
    base_url: str = "wss://api.x.ai/v1/realtime"
    api_key: str = field(default="", repr=False)
    model: str = "grok-voice-latest"
    voice: str = "eve"
    instructions: str = ""
    greeting: str = ""
    language: str = ""
    reasoning_effort: str = "none"
    vad_threshold: float = 0.5
    prefix_padding_ms: int = 300
    silence_duration_ms: int = 500
    idle_timeout_ms: int = 0


@dataclass(frozen=True, slots=True)
class AgentConfig:
    """Call-level policy: duration limits, endpointing, language, and consultations.

    ``consult_timeout_s`` bounds how long a deliberative brain may pause the call
    for the requester's answer. ``holding_phrase`` is spoken before that pause so
    the callee is not left in silence.
    """

    schedule: str = ""
    max_call_seconds: int = 300
    silence_timeout_s: float = 20.0
    vad_threshold: float = -40.0
    language: str = "en"
    consult_timeout_s: float = 8.0
    holding_phrase: str = "One moment please."


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """Settings for the MCP and A2A control-plane listeners.

    Both listeners share one bearer token and one :class:`CallService`. The
    default host is loopback, so a server is not reachable from the LAN unless
    the operator opts in. ``token`` is required by the ``serve`` command but is
    allowed to be empty here so ``doctor`` and ``selftest`` still work.
    """

    host: str = "127.0.0.1"
    port: int = 8765
    a2a_port: int = 8766
    token: str = field(default="", repr=False)
    public_url: str = ""
    audit_log: str = ""


@dataclass(frozen=True, slots=True)
class TranscriptConfig:
    """Settings for local call transcripts.

    ``enabled = false`` keeps the default deployment quiet: no directory or file
    is created. When enabled, each call writes one append-only JSONL file under
    ``directory`` with mode 0600; the directory itself is created with mode 0700.
    A relative path is resolved against the process working directory.
    """

    enabled: bool = False
    directory: str = "runtime/transcripts"


@dataclass(frozen=True, slots=True)
class Config:
    """Top-level runtime configuration.

    ``phone_number`` is optional in the file because ``doctor`` and ``selftest``
    do not call anyone. ``dial`` and ``run`` require either this value or an
    explicit ``--number`` argument.
    """

    audio: AudioConfig = field(default_factory=AudioConfig)
    dialer: DialerConfig = field(default_factory=DialerConfig)
    brain: BrainConfig = field(default_factory=BrainConfig)
    realtime: RealtimeConfig = field(default_factory=RealtimeConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    phone_number: str = field(default="", repr=False)
    transcript: TranscriptConfig = field(default_factory=TranscriptConfig)


def ensure_e164(number: str, label: str = "phone number") -> str:
    """Validate *number* as E.164 and return it unchanged.

    E.164 numbers begin with ``+``, start with a non-zero digit, and contain at
    most 15 digits. No spaces, hyphens, or leading zeros are accepted.
    """

    if not _E164_PATTERN.fullmatch(number):
        raise ConfigError(
            f"{label} must be in E.164 format, for example +15555550100 "
            f"(got {_mask_sensitive(number)!r})"
        )
    return number


def load(path: str | os.PathLike[str], *, env: Mapping[str, str] | None = None) -> Config:
    """Load, apply environment overrides to, and validate a TOML configuration.

    Args:
        path: Path to the TOML file.
        env: Environment mapping to read overrides from. Defaults to
            ``os.environ``. Tests pass an explicit mapping to stay isolated.

    Raises:
        ConfigError: If the file is missing or malformed, a key is unknown, a
            value has the wrong type, or validation fails.
    """

    config_path = Path(path)
    try:
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(
            f"config file not found: {config_path}. Copy config/config.example.toml "
            "to config/config.toml and replace the placeholders."
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {config_path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read config file {config_path}: {exc}") from exc

    allowed = {
        "phone_number",
        "audio",
        "dialer",
        "brain",
        "realtime",
        "agent",
        "server",
        "transcript",
    }
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigError(
            "unknown top-level configuration key(s): "
            f"{', '.join(unknown)}; expected phone_number, audio, dialer, brain, "
            "realtime, agent, server, transcript"
        )

    config = Config(
        phone_number=_coerce_str(data.get("phone_number", ""), "phone_number"),
        audio=_build_section(AudioConfig, data.get("audio"), "audio"),
        dialer=_build_section(DialerConfig, data.get("dialer"), "dialer"),
        brain=_build_section(BrainConfig, data.get("brain"), "brain"),
        realtime=_build_section(RealtimeConfig, data.get("realtime"), "realtime"),
        agent=_build_section(AgentConfig, data.get("agent"), "agent"),
        server=_build_section(ServerConfig, data.get("server"), "server"),
        transcript=_build_section(TranscriptConfig, data.get("transcript"), "transcript"),
    )
    config = _apply_env(config, os.environ if env is None else env)
    validate(config)
    return config


def validate(config: Config) -> None:
    """Validate a fully constructed :class:`Config`.

    This function is separate from :func:`load` so callers that build a
    ``Config`` programmatically can apply the same checks.
    """

    _validate_audio(config.audio)
    _validate_dialer(config.dialer)
    _validate_brain(config.brain)
    _validate_realtime(config.realtime, config.audio)
    _validate_agent(config.agent)
    _validate_server(config.server)
    _validate_transcript(config.transcript)
    if config.phone_number:
        ensure_e164(config.phone_number)


def _mask_sensitive(value: str) -> str:
    """Keep the last four characters when echoing an invalid phone number."""

    digits = value.removeprefix("+")
    if len(digits) <= 4:
        return "*" * len(digits)
    return "*" * (len(digits) - 4) + digits[-4:]


def _validate_audio(audio: AudioConfig) -> None:
    if audio.backend not in {"alsa", "pipewire"}:
        raise ConfigError(
            f"audio.backend must be 'alsa' or 'pipewire' (got {audio.backend!r}); "
            "'alsa' is the USB gadget and 'pipewire' is Bluetooth HFP"
        )
    if audio.backend == "alsa":
        if not audio.card:
            raise ConfigError(
                "audio.card is required when audio.backend = 'alsa'; run `arecord -l` "
                "and set [audio].card to the gadget card id (the token after 'card N:')"
            )
        if not _CARD_PATTERN.fullmatch(audio.card):
            raise ConfigError(
                f"audio.card must contain only letters, digits, '_', '-', or '.' "
                f"(got {audio.card!r})"
            )
    else:
        if not audio.bluetooth_address:
            raise ConfigError(
                "audio.bluetooth_address is required when audio.backend = 'pipewire'; "
                "set it to Phone A's Bluetooth address, for example "
                "'AA:BB:CC:DD:EE:FF'"
            )
        if not _BLUETOOTH_ADDRESS.fullmatch(audio.bluetooth_address):
            raise ConfigError(
                "audio.bluetooth_address must be six colon-separated hex octets, "
                f"for example 'AA:BB:CC:DD:EE:FF' (got {audio.bluetooth_address!r})"
            )
    if audio.device < 0:
        raise ConfigError(f"audio.device must be >= 0 (got {audio.device})")
    if not math.isfinite(audio.node_timeout_s) or audio.node_timeout_s <= 0:
        raise ConfigError(
            f"audio.node_timeout_s must be a positive number (got {audio.node_timeout_s})"
        )
    if not 8_000 <= audio.sample_rate <= 48_000:
        raise ConfigError(
            f"audio.sample_rate must be between 8000 and 48000 Hz (got {audio.sample_rate})"
        )
    if audio.channels != 1:
        raise ConfigError(
            "audio.channels must be 1 in v0; the pipeline processes mono and the "
            "ALSA/PipeWire layer downmixes the gadget or SCO stream"
        )
    if not 1 <= audio.frame_ms <= 1_000:
        raise ConfigError(f"audio.frame_ms must be between 1 and 1000 (got {audio.frame_ms})")
    if (audio.sample_rate * audio.frame_ms) % 1_000 != 0:
        raise ConfigError(
            f"audio.frame_ms={audio.frame_ms} is not a whole number of samples at "
            f"{audio.sample_rate} Hz; choose a frame length that is a multiple of "
            "the sample period"
        )


def _validate_dialer(dialer: DialerConfig) -> None:
    if not dialer.url:
        raise ConfigError(
            "dialer.url is required; set [dialer].url to the LAN base URL of the "
            "helper app, for example http://192.168.1.20:8080"
        )
    _validate_url(dialer.url, "dialer.url")
    if not dialer.token.strip():
        raise ConfigError(
            "dialer.token is required; set [dialer].token or "
            "TELEPHONE_AGENT_DIALER__TOKEN to the helper's bearer token"
        )
    if not math.isfinite(dialer.timeout_s) or dialer.timeout_s <= 0:
        raise ConfigError(f"dialer.timeout_s must be a positive number (got {dialer.timeout_s})")
    if not 0 <= dialer.retries <= 5:
        raise ConfigError(
            f"dialer.retries must be between 0 and 5 (got {dialer.retries}); "
            "retries delay the call and should stay bounded"
        )


def _validate_brain(brain: BrainConfig) -> None:
    if brain.kind not in {"echo", "http"}:
        raise ConfigError(f"brain.kind must be 'echo' or 'http' (got {brain.kind!r})")
    if brain.max_tokens <= 0:
        raise ConfigError(f"brain.max_tokens must be > 0 (got {brain.max_tokens})")
    if not math.isfinite(brain.timeout_s) or brain.timeout_s <= 0:
        raise ConfigError(f"brain.timeout_s must be a positive number (got {brain.timeout_s})")
    if brain.action_mode not in {"text", "json"}:
        raise ConfigError(
            f"brain.action_mode must be 'text' or 'json' (got {brain.action_mode!r}); "
            "'text' wraps a plain reply brain and 'json' asks an HTTP model for actions"
        )
    if brain.action_mode == "json" and brain.kind != "http":
        raise ConfigError(
            "brain.action_mode = 'json' requires brain.kind = 'http'; the echo brain "
            "cannot produce actions"
        )
    if brain.kind == "http":
        if not brain.base_url:
            raise ConfigError(
                "brain.base_url is required when brain.kind = 'http'; point it at "
                "the API root, for example https://api.example.invalid/v1"
            )
        _validate_url(brain.base_url, "brain.base_url")
        if not brain.model.strip():
            raise ConfigError(
                "brain.model is required when brain.kind = 'http'; set it to the "
                "provider's model identifier"
            )


_REALTIME_RATES = {8_000, 16_000, 22_050, 24_000, 32_000, 44_100, 48_000}
_GREETING_MAX_CHARS = 300
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


def _validate_realtime(realtime: RealtimeConfig, audio: AudioConfig) -> None:
    if realtime.kind not in {"off", "grok"}:
        raise ConfigError(
            f"realtime.kind must be 'off' or 'grok' (got {realtime.kind!r}); "
            "'grok' selects xAI's speech-to-speech WebSocket API"
        )
    if realtime.kind == "off":
        return

    greeting = realtime.greeting.strip()
    if _CONTROL_CHARACTERS.search(realtime.greeting):
        raise ConfigError("realtime.greeting must be a single line without control characters")
    if len(greeting) > _GREETING_MAX_CHARS:
        raise ConfigError(
            f"realtime.greeting must be at most {_GREETING_MAX_CHARS} characters "
            f"after trimming whitespace (got {len(greeting)})"
        )

    _validate_ws_url(realtime.base_url, "realtime.base_url")
    if not realtime.model.strip():
        raise ConfigError(
            "realtime.model is required when realtime.kind = 'grok'; use "
            "'grok-voice-latest' or a pinned version such as 'grok-voice-3.0'"
        )
    if not realtime.voice.strip():
        raise ConfigError(
            "realtime.voice is required when realtime.kind = 'grok'; use a "
            "built-in voice id such as 'eve' or a custom voice id"
        )
    if realtime.reasoning_effort not in {"high", "none"}:
        raise ConfigError(
            "realtime.reasoning_effort must be 'high' or 'none' "
            f"(got {realtime.reasoning_effort!r})"
        )
    if not math.isfinite(realtime.vad_threshold) or not 0.0 <= realtime.vad_threshold <= 1.0:
        raise ConfigError(
            f"realtime.vad_threshold must be between 0 and 1 (got {realtime.vad_threshold})"
        )
    if realtime.prefix_padding_ms < 0:
        raise ConfigError(
            f"realtime.prefix_padding_ms must be >= 0 (got {realtime.prefix_padding_ms})"
        )
    if realtime.silence_duration_ms < 0:
        raise ConfigError(
            f"realtime.silence_duration_ms must be >= 0 (got {realtime.silence_duration_ms})"
        )
    if realtime.idle_timeout_ms < 0:
        raise ConfigError(f"realtime.idle_timeout_ms must be >= 0 (got {realtime.idle_timeout_ms})")
    if audio.sample_rate not in _REALTIME_RATES:
        supported = ", ".join(str(rate) for rate in sorted(_REALTIME_RATES))
        raise ConfigError(
            "audio.sample_rate must be one of the Grok Voice PCM rates when "
            f"realtime.kind = 'grok' (got {audio.sample_rate}; supported: {supported})"
        )


def _validate_agent(agent: AgentConfig) -> None:
    if agent.max_call_seconds <= 0:
        raise ConfigError(f"agent.max_call_seconds must be > 0 (got {agent.max_call_seconds})")
    if not math.isfinite(agent.silence_timeout_s) or agent.silence_timeout_s <= 0:
        raise ConfigError(
            f"agent.silence_timeout_s must be a positive number (got {agent.silence_timeout_s})"
        )
    if not -100.0 <= agent.vad_threshold <= 0.0:
        raise ConfigError(
            "agent.vad_threshold must be a dBFS value between -100 and 0 "
            f"(got {agent.vad_threshold})"
        )
    if not agent.language.strip():
        raise ConfigError("agent.language is required; use a BCP-47 tag such as en-IN")
    if not math.isfinite(agent.consult_timeout_s) or agent.consult_timeout_s <= 0:
        raise ConfigError(
            f"agent.consult_timeout_s must be a positive number (got {agent.consult_timeout_s})"
        )
    if not agent.holding_phrase.strip():
        raise ConfigError(
            "agent.holding_phrase is required; it is spoken before the call pauses "
            "for the requester's answer"
        )


def _validate_server(server: ServerConfig) -> None:
    if not isinstance(server.host, str) or not server.host.strip():
        raise ConfigError(
            "server.host is required; use 127.0.0.1 for loopback or a LAN address "
            "when another host must connect"
        )
    if "://" in server.host or "/" in server.host:
        raise ConfigError(
            "server.host must be a hostname or IP address without a scheme or path "
            f"(got {server.host!r})"
        )
    _validate_port(server.port, "server.port")
    _validate_port(server.a2a_port, "server.a2a_port")
    if server.port == server.a2a_port:
        raise ConfigError(
            f"server.port (MCP) and server.a2a_port (A2A) must differ (both are {server.port})"
        )
    if server.token and len(server.token) < 8:
        raise ConfigError(
            "server.token must be at least 8 characters when set; prefer "
            "TELEPHONE_AGENT_SERVER__TOKEN over storing it in the config file"
        )
    if server.public_url:
        _validate_url(server.public_url, "server.public_url")
    if server.audit_log and (not isinstance(server.audit_log, str) or not server.audit_log.strip()):
        raise ConfigError(
            "server.audit_log must be a non-empty path when set; omit it to disable the audit log"
        )


def _validate_transcript(transcript: TranscriptConfig) -> None:
    if not isinstance(transcript.enabled, bool):
        raise ConfigError(f"transcript.enabled must be a boolean (got {transcript.enabled!r})")
    if not isinstance(transcript.directory, str) or not transcript.directory.strip():
        raise ConfigError(
            "transcript.directory must be a non-empty path when transcripts are "
            "configured; use a writable local directory such as runtime/transcripts"
        )


def _validate_port(port: object, label: str) -> None:
    if isinstance(port, bool) or not isinstance(port, int):
        raise ConfigError(f"{label} must be an integer (got {port!r})")
    if not 1 <= port <= 65535:
        raise ConfigError(f"{label} must be between 1 and 65535 (got {port})")


def _validate_url(value: str, label: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(f"{label} must be an absolute http:// or https:// URL (got {value!r})")


def _validate_ws_url(value: str, label: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
        raise ConfigError(f"{label} must be an absolute ws:// or wss:// URL (got {value!r})")


def _build_section(cls: type[_T], raw: object, section: str) -> _T:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"[{section}] must be a TOML table")

    allowed = {item.name for item in fields(cls)}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(
            f"unknown key(s) in [{section}]: {', '.join(unknown)}; "
            f"expected {', '.join(sorted(allowed))}"
        )

    values: dict[str, object] = {}
    for item in fields(cls):
        if item.name in raw:
            values[item.name] = _coerce_toml(raw[item.name], item.default, f"{section}.{item.name}")
    return cls(**values)  # type: ignore[arg-type]


def _coerce_toml(value: object, default: object, label: str) -> object:
    if isinstance(default, str):
        return _coerce_str(value, label)
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ConfigError(f"{label} must be a boolean (got {value!r})")
        return value
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{label} must be an integer (got {value!r})")
        return value
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{label} must be a number (got {value!r})")
        return float(value)
    raise ConfigError(f"internal error: unsupported default type for {label}")


def _coerce_str(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{label} must be a string (got {value!r})")
    return value


def _apply_env(config: Config, env: Mapping[str, str]) -> Config:
    section_types: dict[str, type[object]] = {
        "audio": AudioConfig,
        "dialer": DialerConfig,
        "brain": BrainConfig,
        "realtime": RealtimeConfig,
        "agent": AgentConfig,
        "server": ServerConfig,
        "transcript": TranscriptConfig,
    }

    for name, raw in sorted(env.items()):
        if not name.startswith(_ENV_PREFIX):
            continue
        suffix = name[len(_ENV_PREFIX) :]
        parts = [part.lower() for part in suffix.split("__")]

        if len(parts) == 1:
            if parts[0] != "phone_number":
                raise ConfigError(
                    f"{name} is not a valid override; use "
                    "TELEPHONE_AGENT_PHONE_NUMBER or "
                    "TELEPHONE_AGENT_<SECTION>__<FIELD>"
                )
            config = replace(config, phone_number=raw)
            continue

        if len(parts) != 2:
            raise ConfigError(
                f"{name} is not a valid override; use "
                "TELEPHONE_AGENT_<SECTION>__<FIELD> with one nested level"
            )

        section, field_name = parts
        if section not in section_types:
            raise ConfigError(
                f"{name} refers to unknown section {section!r}; "
                f"expected one of {', '.join(sorted(section_types))}"
            )
        section_value = getattr(config, section)
        section_fields = {item.name: item for item in fields(section_value)}
        if field_name not in section_fields:
            raise ConfigError(
                f"{name} refers to unknown key {field_name!r} in [{section}]; "
                f"expected one of {', '.join(sorted(section_fields))}"
            )
        coerced = _coerce_env(raw, section_fields[field_name].default, f"{section}.{field_name}")
        updated = replace(section_value, **{field_name: coerced})
        config = replace(config, **{section: updated})

    return config


def _coerce_env(raw: str, default: object, label: str) -> object:
    if isinstance(default, str):
        return raw
    if isinstance(default, bool):
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
        raise ConfigError(f"{label} must be a boolean (got {raw!r})")
    if isinstance(default, int):
        try:
            return int(raw, 10)
        except ValueError as exc:
            raise ConfigError(f"{label} must be an integer (got {raw!r})") from exc
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError as exc:
            raise ConfigError(f"{label} must be a number (got {raw!r})") from exc
    raise ConfigError(f"internal error: unsupported default type for {label}")
