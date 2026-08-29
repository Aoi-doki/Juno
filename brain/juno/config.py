"""Configuration, loaded once from YAML with environment overrides.

The only secret is the Anthropic key, which is read from the environment and
never from the file — so ``config.yaml`` stays safe to commit if you ever want
to track your own tuning.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path(os.environ.get("JUNO_CONFIG", "config.yaml"))


@dataclass(slots=True)
class ModelConfig:
    """Which model handles what.

    ``routine`` carries the constant background load — check-in decisions,
    short replies — and is why the monthly bill is single-digit dollars.
    ``escalation`` is reserved for turns that actually need the reasoning.
    """

    routine: str = "claude-haiku-4-5-20251001"
    escalation: str = "claude-sonnet-5"
    max_tokens: int = 1024
    # Above this many characters of assembled context, a turn is promoted to
    # the escalation model. Cheap heuristic, easy to tune once you see traffic.
    escalate_over_chars: int = 6000
    # Hard stop. When the month's spend crosses this, the brain drops to the
    # local Ollama tier rather than silently costing more than you agreed to.
    monthly_budget_usd: float = 5.0
    local_fallback_url: str = "http://127.0.0.1:11434"
    local_fallback_model: str = "qwen3:4b"


@dataclass(slots=True)
class VoiceConfig:
    wake_word: str = "hey_juno"
    # Shipped as a placeholder until the custom "hey juno" model finishes
    # training; see clients/laptop/README.md.
    wake_word_fallback: str = "hey_jarvis"
    wake_threshold: float = 0.6
    kokoro_voice: str = "af_heart"
    kokoro_speed: float = 1.0
    whisper_model: str = "base.en"
    whisper_compute: str = "int8"
    # Silence that ends an utterance.
    endpoint_silence_ms: int = 700


@dataclass(slots=True)
class ProactivityConfig:
    """How pushy she is. 0 = only speaks when spoken to, 10 = drill sergeant."""

    level: int = 7
    quiet_hours: tuple[int, int] = (23, 8)
    check_in_minutes: tuple[int, int] = (20, 40)
    # Apps that count as doomscrolling, with the minutes of continuous use
    # before the first nudge. Overrides the default ladder per app.
    scroll_apps: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0 <= self.level <= 10:
            raise ValueError(f"proactivity level must be 0-10, got {self.level}")


@dataclass(slots=True)
class Config:
    host: str = "0.0.0.0"
    port: int = 8765
    db_path: Path = Path("juno.db")
    # Shared secret every client presents. Tailscale already restricts who can
    # reach the port; this is the second lock, so a wrong turn in the ACL
    # doesn't hand a stranger your microphone.
    auth_token: str = ""
    timezone: str = "UTC"
    user_name: str = "you"
    models: ModelConfig = field(default_factory=ModelConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    proactivity: ProactivityConfig = field(default_factory=ProactivityConfig)

    @property
    def anthropic_key(self) -> str:
        return os.environ.get("ANTHROPIC_API_KEY", "")

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        path = path or DEFAULT_PATH
        raw: dict[str, Any] = {}
        if path.exists():
            raw = yaml.safe_load(path.read_text()) or {}

        token = os.environ.get("JUNO_AUTH_TOKEN") or raw.get("auth_token", "")
        if not token:
            raise ValueError(
                "No auth token. Set JUNO_AUTH_TOKEN or auth_token in config.yaml "
                "(generate one with: openssl rand -hex 32)"
            )

        return cls(
            host=raw.get("host", "0.0.0.0"),
            port=int(raw.get("port", 8765)),
            db_path=Path(raw.get("db_path", "juno.db")),
            auth_token=token,
            timezone=raw.get("timezone", "UTC"),
            user_name=raw.get("user_name", "you"),
            models=ModelConfig(**(raw.get("models") or {})),
            voice=VoiceConfig(**(raw.get("voice") or {})),
            proactivity=ProactivityConfig(**(raw.get("proactivity") or {})),
        )
