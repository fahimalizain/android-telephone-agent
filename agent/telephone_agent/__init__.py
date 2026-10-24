"""Reference runtime for the android-telephone-agent project.

The package exposes one small API surface for the call pipeline and the
supporting configuration, audio, dialing, and brain layers. Runtime code uses
only the Python standard library; pytest and ruff are development dependencies.
"""

from __future__ import annotations

from .audio import AudioError, AudioStats, CaptureDevice, GadgetAudio
from .brain import (
    DEFAULT_SYSTEM_PROMPT,
    Brain,
    BrainError,
    EchoBrain,
    HttpBrain,
    build_brain,
)
from .config import (
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
from .dialer import Dialer, DialerError, mask_number
from .pipeline import (
    CallEndReason,
    CallResult,
    CallSession,
    CallState,
    Synthesizer,
    Transcriber,
)
from .vad import Endpointer, dbfs_from_rms, is_speech, pcm_to_samples, rms

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "AgentConfig",
    "AudioConfig",
    "AudioError",
    "AudioStats",
    "Brain",
    "BrainConfig",
    "BrainError",
    "CallEndReason",
    "CallResult",
    "CallSession",
    "CallState",
    "CaptureDevice",
    "Config",
    "ConfigError",
    "Dialer",
    "DialerConfig",
    "DialerError",
    "EchoBrain",
    "Endpointer",
    "GadgetAudio",
    "HttpBrain",
    "Synthesizer",
    "Transcriber",
    "__version__",
    "build_brain",
    "dbfs_from_rms",
    "ensure_e164",
    "is_speech",
    "load",
    "mask_number",
    "pcm_to_samples",
    "rms",
    "validate",
]
