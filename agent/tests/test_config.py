"""Tests for configuration loading, environment overrides, and validation."""

from __future__ import annotations

from pathlib import Path

import pytest
from telephone_agent.config import (
    AgentConfig,
    AudioConfig,
    BrainConfig,
    Config,
    ConfigError,
    DialerConfig,
    ensure_e164,
    load,
    validate,
)

BASE_CONFIG = """
phone_number = "+15555550100"

[audio]
card = "UAC1Gadget"

[dialer]
url = "http://192.168.1.10:8080"
token = "test-token"
"""


def _write(tmp_path: Path, text: str = BASE_CONFIG) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_applies_defaults(tmp_path: Path) -> None:
    config = load(_write(tmp_path), env={})

    assert config.phone_number == "+15555550100"
    assert config.audio == AudioConfig(card="UAC1Gadget")
    assert config.dialer == DialerConfig(
        url="http://192.168.1.10:8080",
        token="test-token",
    )
    assert config.brain.kind == "echo"
    assert config.agent == AgentConfig()
    assert config.brain.max_tokens == 200
    assert config.agent.vad_threshold == -40.0


def test_load_reads_all_sections(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
phone_number = "+15555550199"

[audio]
card = "Gadget"
device = 1
sample_rate = 48000
channels = 1
frame_ms = 10

[dialer]
url = "https://helper.example.invalid:8443"
token = "secret-token"
timeout_s = 2.5
retries = 0

[brain]
kind = "http"
base_url = "https://llm.example.invalid/v1"
api_key = "test-key"
model = "test-model"
system_prompt = "Be brief."
max_tokens = 64
timeout_s = 12.0

[agent]
schedule = "0 9 * * *"
max_call_seconds = 120
silence_timeout_s = 12.5
vad_threshold = -35.0
language = "ml-IN"
""",
    )

    config = load(path, env={})

    assert config.audio.card == "Gadget"
    assert config.audio.device == 1
    assert config.audio.sample_rate == 48000
    assert config.audio.frame_ms == 10
    assert config.dialer.retries == 0
    assert config.dialer.timeout_s == 2.5
    assert config.brain.kind == "http"
    assert config.brain.model == "test-model"
    assert config.brain.api_key == "test-key"
    assert config.agent.schedule == "0 9 * * *"
    assert config.agent.language == "ml-IN"
    assert config.agent.vad_threshold == -35.0
    assert "secret-token" not in repr(config)
    assert "test-key" not in repr(config)
    assert "+15555550199" not in repr(config)


def test_environment_overrides_toml(tmp_path: Path) -> None:
    env = {
        "TELEPHONE_AGENT_PHONE_NUMBER": "+15555550111",
        "TELEPHONE_AGENT_AUDIO__CARD": "EnvCard",
        "TELEPHONE_AGENT_AUDIO__SAMPLE_RATE": "48000",
        "TELEPHONE_AGENT_AUDIO__FRAME_MS": "10",
        "TELEPHONE_AGENT_DIALER__TOKEN": "env-token",
        "TELEPHONE_AGENT_DIALER__RETRIES": "1",
        "TELEPHONE_AGENT_BRAIN__KIND": "http",
        "TELEPHONE_AGENT_BRAIN__BASE_URL": "http://127.0.0.1:11434/v1",
        "TELEPHONE_AGENT_BRAIN__MODEL": "local",
        "TELEPHONE_AGENT_AGENT__SILENCE_TIMEOUT_S": "7.5",
        "TELEPHONE_AGENT_AGENT__VAD_THRESHOLD": "-30",
    }

    config = load(_write(tmp_path), env=env)

    assert config.phone_number == "+15555550111"
    assert config.audio.card == "EnvCard"
    assert config.audio.sample_rate == 48000
    assert config.audio.frame_ms == 10
    assert config.dialer.token == "env-token"
    assert config.dialer.retries == 1
    assert config.brain.kind == "http"
    assert config.brain.base_url == "http://127.0.0.1:11434/v1"
    assert config.brain.api_key == ""
    assert config.agent.silence_timeout_s == 7.5
    assert config.agent.vad_threshold == -30.0


def test_unrelated_environment_is_ignored(tmp_path: Path) -> None:
    config = load(_write(tmp_path), env={"PATH": "/usr/bin", "TELEPHONE_HOME": "x"})
    assert config.audio.card == "UAC1Gadget"


def test_phone_number_is_optional(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
[audio]
card = "Gadget"

[dialer]
url = "http://127.0.0.1:8080"
token = "test-token"
""",
    )

    config = load(path, env={})

    assert config.phone_number == ""


@pytest.mark.parametrize(
    "phone",
    ["9876543210", "+0123456789", "+1 555 555 0100", "+123", "+1234567890123456", ""],
)
def test_invalid_phone_numbers_are_rejected(tmp_path: Path, phone: str) -> None:
    path = _write(
        tmp_path,
        f"""
phone_number = "{phone}"

[audio]
card = "Gadget"

[dialer]
url = "http://127.0.0.1:8080"
token = "test-token"
""",
    )

    if phone == "":
        # An empty top-level number means "not configured", not invalid.
        assert load(path, env={}).phone_number == ""
        return

    with pytest.raises(ConfigError, match="E.164"):
        load(path, env={})


def test_ensure_e164_accepts_valid_numbers() -> None:
    assert ensure_e164("+15555550100") == "+15555550100"
    assert ensure_e164("+919876543210", "destination") == "+919876543210"


def test_missing_config_file_is_actionable(tmp_path: Path) -> None:
    missing = tmp_path / "nope.toml"

    with pytest.raises(ConfigError, match="config file not found"):
        load(missing, env={})


def test_audio_config_keeps_positional_field_order_compatible() -> None:
    audio = AudioConfig("Card", 0, 16_000, 1, 20)

    assert audio.card == "Card"
    assert audio.device == 0
    assert audio.sample_rate == 16_000
    assert audio.channels == 1
    assert audio.frame_ms == 20
    assert audio.backend == "alsa"
    assert audio.bluetooth_address == ""
    assert audio.node_timeout_s == 30.0


def test_bluetooth_audio_config_is_accepted(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
[audio]
backend = "pipewire"
bluetooth_address = "AA:BB:CC:DD:EE:FF"
node_timeout_s = 12.5

[dialer]
url = "http://127.0.0.1:8080"
token = "test-token"
""",
    )

    config = load(path, env={})

    assert config.audio.backend == "pipewire"
    assert config.audio.bluetooth_address == "AA:BB:CC:DD:EE:FF"
    assert config.audio.node_timeout_s == 12.5
    assert config.audio.card == ""


def test_bluetooth_backend_requires_an_address(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
[audio]
backend = "pipewire"

[dialer]
url = "http://127.0.0.1:8080"
token = "test-token"
""",
    )

    with pytest.raises(ConfigError, match="bluetooth_address is required"):
        load(path, env={})


def test_bluetooth_address_is_validated(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
[audio]
backend = "pipewire"
bluetooth_address = "not-a-mac"

[dialer]
url = "http://127.0.0.1:8080"
token = "test-token"
""",
    )

    with pytest.raises(ConfigError, match="six colon-separated hex octets"):
        load(path, env={})


def test_unknown_audio_backend_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
[audio]
backend = "jack"
card = "Gadget"

[dialer]
url = "http://127.0.0.1:8080"
token = "test-token"
""",
    )

    with pytest.raises(ConfigError, match="audio.backend"):
        load(path, env={})


def test_node_timeout_must_be_positive(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
[audio]
card = "Gadget"
node_timeout_s = 0

[dialer]
url = "http://127.0.0.1:8080"
token = "test-token"
""",
    )

    with pytest.raises(ConfigError, match="node_timeout_s"):
        load(path, env={})


def test_environment_can_select_bluetooth(tmp_path: Path) -> None:
    env = {
        "TELEPHONE_AGENT_AUDIO__BACKEND": "pipewire",
        "TELEPHONE_AGENT_AUDIO__BLUETOOTH_ADDRESS": "AA:BB:CC:DD:EE:FF",
        "TELEPHONE_AGENT_AUDIO__NODE_TIMEOUT_S": "5",
    }

    config = load(_write(tmp_path), env=env)

    assert config.audio.backend == "pipewire"
    assert config.audio.bluetooth_address == "AA:BB:CC:DD:EE:FF"
    assert config.audio.node_timeout_s == 5.0


def test_invalid_toml_is_reported(tmp_path: Path) -> None:
    path = _write(tmp_path, "[audio\ncard = 'x'\n")

    with pytest.raises(ConfigError, match="invalid TOML"):
        load(path, env={})


def test_unknown_top_level_key_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, f"unknown = true\n{BASE_CONFIG}")

    with pytest.raises(ConfigError, match="unknown top-level"):
        load(path, env={})


def test_unknown_section_key_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
[audio]
card = "Gadget"
gain = 3

[dialer]
url = "http://127.0.0.1:8080"
token = "test-token"
""",
    )

    with pytest.raises(ConfigError, match="unknown key.*gain"):
        load(path, env={})


@pytest.mark.parametrize(
    "audio_block, message",
    [
        ('[audio]\ncard = ""', "audio.card is required"),
        ('[audio]\ncard = "bad card"', "audio.card must contain"),
        ('[audio]\ncard = "Gadget"\nchannels = 2', "audio.channels must be 1"),
        ('[audio]\ncard = "Gadget"\nsample_rate = 7999', "audio.sample_rate"),
        ('[audio]\ncard = "Gadget"\nsample_rate = 11025\nframe_ms = 20', "whole number"),
        ('[audio]\ncard = "Gadget"\nframe_ms = 0', "audio.frame_ms"),
        ('[audio]\ncard = "Gadget"\ndevice = -1', "audio.device"),
    ],
)
def test_audio_validation(tmp_path: Path, audio_block: str, message: str) -> None:
    path = _write(
        tmp_path,
        f"""
{audio_block}

[dialer]
url = "http://127.0.0.1:8080"
token = "test-token"
""",
    )

    with pytest.raises(ConfigError, match=message):
        load(path, env={})


@pytest.mark.parametrize(
    "dialer_block, message",
    [
        ('[dialer]\ntoken = "test-token"', "dialer.url is required"),
        ('[dialer]\nurl = "http://127.0.0.1:8080"', "dialer.token is required"),
        ('[dialer]\nurl = "ftp://127.0.0.1"\ntoken = "x"', "absolute http"),
        ('[dialer]\nurl = "http://127.0.0.1:8080"\ntoken = "x"\nretries = -1', "retries"),
        ('[dialer]\nurl = "http://127.0.0.1:8080"\ntoken = "x"\nretries = 6', "retries"),
        ('[dialer]\nurl = "http://127.0.0.1:8080"\ntoken = "x"\ntimeout_s = 0', "timeout_s"),
    ],
)
def test_dialer_validation(tmp_path: Path, dialer_block: str, message: str) -> None:
    path = _write(
        tmp_path,
        f"""
[audio]
card = "Gadget"

{dialer_block}
""",
    )

    with pytest.raises(ConfigError, match=message):
        load(path, env={})


@pytest.mark.parametrize(
    "brain_block, message",
    [
        ('[brain]\nkind = "other"', "brain.kind"),
        ('[brain]\nkind = "http"', "brain.base_url is required"),
        (
            '[brain]\nkind = "http"\nbase_url = "https://llm.example.invalid/v1"',
            "brain.model is required",
        ),
        (
            '[brain]\nkind = "http"\nbase_url = "not-a-url"\nmodel = "m"',
            "absolute http",
        ),
        ("[brain]\nmax_tokens = 0", "brain.max_tokens"),
        ("[brain]\ntimeout_s = 0", "brain.timeout_s"),
    ],
)
def test_brain_validation(tmp_path: Path, brain_block: str, message: str) -> None:
    path = _write(
        tmp_path,
        f"""
[audio]
card = "Gadget"

[dialer]
url = "http://127.0.0.1:8080"
token = "test-token"

{brain_block}
""",
    )

    with pytest.raises(ConfigError, match=message):
        load(path, env={})


def test_http_brain_allows_no_api_key(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
[audio]
card = "Gadget"

[dialer]
url = "http://127.0.0.1:8080"
token = "test-token"

[brain]
kind = "http"
base_url = "http://127.0.0.1:11434/v1"
model = "local"
""",
    )

    config = load(path, env={})

    assert config.brain.kind == "http"
    assert config.brain.api_key == ""


@pytest.mark.parametrize(
    "agent_block, message",
    [
        ("[agent]\nmax_call_seconds = 0", "max_call_seconds"),
        ("[agent]\nsilence_timeout_s = 0", "silence_timeout_s"),
        ("[agent]\nvad_threshold = 3", "vad_threshold"),
        ("[agent]\nvad_threshold = -101", "vad_threshold"),
        ('[agent]\nlanguage = "  "', "language"),
    ],
)
def test_agent_validation(tmp_path: Path, agent_block: str, message: str) -> None:
    path = _write(
        tmp_path,
        f"""
[audio]
card = "Gadget"

[dialer]
url = "http://127.0.0.1:8080"
token = "test-token"

{agent_block}
""",
    )

    with pytest.raises(ConfigError, match=message):
        load(path, env={})


def test_wrong_toml_type_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
[audio]
card = "Gadget"
sample_rate = "16000"

[dialer]
url = "http://127.0.0.1:8080"
token = "test-token"
""",
    )

    with pytest.raises(ConfigError, match="audio.sample_rate must be an integer"):
        load(path, env={})


def test_invalid_environment_value_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="audio.sample_rate must be an integer"):
        load(_write(tmp_path), env={"TELEPHONE_AGENT_AUDIO__SAMPLE_RATE": "fast"})


@pytest.mark.parametrize(
    "name",
    [
        "TELEPHONE_AGENT_AUDIO__GAIN",
        "TELEPHONE_AGENT_SPEECH__MODEL",
        "TELEPHONE_AGENT_NOPE",
        "TELEPHONE_AGENT_AUDIO__CARD__EXTRA",
    ],
)
def test_invalid_environment_names_are_rejected(tmp_path: Path, name: str) -> None:
    with pytest.raises(ConfigError, match="not a valid override|unknown"):
        load(_write(tmp_path), env={name: "x"})


def test_validate_rechecks_programmatic_config() -> None:
    with pytest.raises(ConfigError, match="E.164"):
        validate(
            config=Config(
                audio=AudioConfig(card="Gadget"),
                dialer=DialerConfig(url="http://127.0.0.1:8080", token="test-token"),
                brain=BrainConfig(),
                agent=AgentConfig(),
                phone_number="9876543210",
            )
        )


def test_example_config_is_complete_and_valid() -> None:
    example = Path(__file__).resolve().parents[2] / "config" / "config.example.toml"
    if not example.is_file():
        pytest.skip("the example config is only present in the repository checkout")

    config = load(example, env={})

    assert config.audio.card
    assert config.dialer.url
    assert config.dialer.token
    assert config.audio.sample_rate == 16000
    assert config.brain.kind in {"echo", "http"}
