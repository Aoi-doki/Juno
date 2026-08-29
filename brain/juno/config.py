"""Configuration, loaded once from YAML with environment overrides.

Secrets — API keys, the shared device token — are read from the environment and
never from the file, so ``config.yaml`` stays safe to commit if you ever want to
track your own tuning.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path(os.environ.get("JUNO_CONFIG", "config.yaml"))


@dataclass(slots=True)
class EngineSpec:
    """One backend Juno can think with.

    ``kind`` is ``openai`` for anything speaking OpenAI's chat-completions API —
    Gemini, Ollama, Cerebras, OpenRouter — or ``anthropic`` for Claude.

    The key is read from the environment, never the file, so ``config.yaml``
    stays safe to commit.
    """

    kind: str = "openai"
    base_url: str = ""
    model: str = ""
    api_key_env: str = ""

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "") if self.api_key_env else ""


DEFAULT_ENGINES = {
    # Free tier, no card. Fast and good at tool calls.
    "gemini": EngineSpec(
        kind="openai",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        model="gemini-2.5-flash",
        api_key_env="GEMINI_API_KEY",
    ),
    # On the always-on box. Slow on ARM without a GPU — fine for check-ins that
    # nobody is waiting on, painful for conversation.
    "local": EngineSpec(
        kind="openai",
        base_url="http://127.0.0.1:11434/v1",
        model="qwen3:4b",
    ),
    "claude": EngineSpec(
        kind="anthropic",
        model="claude-haiku-4-5",
        api_key_env="ANTHROPIC_API_KEY",
    ),
}


@dataclass(slots=True)
class ModelConfig:
    """Which engine handles what.

    Roles rather than a single model, because Juno's paths have genuinely
    different needs. Check-ins are high-volume and carry your screen contents,
    so they are the natural candidate for staying local. Conversation is
    low-volume and latency-sensitive, so it wants the fastest engine you have.
    """

    engines: dict[str, EngineSpec] = field(default_factory=lambda: dict(DEFAULT_ENGINES))
    # Ordinary back-and-forth. Latency matters; you are waiting for it.
    conversation: str = "gemini"
    # Periodic "should I say anything?". High volume, and the prompt carries
    # your activity timeline.
    checkin: str = "local"
    # Used when the role engine is unavailable or errors.
    fallback: str = "local"
    max_tokens: int = 1024

    def spec(self, role_engine: str) -> EngineSpec | None:
        return self.engines.get(role_engine)


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
class HomeAssistantConfig:
    """Home Assistant is the device layer rather than per-vendor APIs: one
    integration gets every device HA supports, now and later."""

    url: str = ""
    # Long-lived access token, from your HA profile page. Env var wins so it
    # need not be written to disk.
    token: str = ""
    # Entities Juno may never touch, by entity_id or a `domain.*` glob. Locks
    # and garage doors are the obvious candidates.
    forbidden: list[str] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)


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
    # Private subscription URL for your calendar. Google, Radicale, Nextcloud
    # and Fastmail all expose one; full CalDAV is not needed to read it.
    calendar_ics_url: str = ""
    models: ModelConfig = field(default_factory=ModelConfig)
    voice: VoiceConfig = field(default_factory=VoiceConfig)
    proactivity: ProactivityConfig = field(default_factory=ProactivityConfig)
    home_assistant: HomeAssistantConfig = field(default_factory=HomeAssistantConfig)

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

        home = dict(raw.get("home_assistant") or {})
        home["token"] = os.environ.get("JUNO_HA_TOKEN") or home.get("token", "")

        proactivity = dict(raw.get("proactivity") or {})
        if "quiet_hours" in proactivity:
            # YAML gives a list; the dataclass wants a pair, and comparing a
            # list against a tuple later would silently misbehave.
            proactivity["quiet_hours"] = tuple(proactivity["quiet_hours"])
        if "check_in_minutes" in proactivity:
            proactivity["check_in_minutes"] = tuple(proactivity["check_in_minutes"])

        models_raw = dict(raw.get("models") or {})
        engines = dict(DEFAULT_ENGINES)
        for name, spec in (models_raw.pop("engines", None) or {}).items():
            # Merge over the default so a config only has to name what differs
            # — usually just the model, keeping the verified base URL.
            base = engines.get(name)
            merged = {**(base.__dict__ if base else {}), **spec}
            engines[name] = EngineSpec(**merged)
        models_raw["engines"] = engines

        return cls(
            host=raw.get("host", "0.0.0.0"),
            port=int(raw.get("port", 8765)),
            db_path=Path(raw.get("db_path", "juno.db")),
            auth_token=token,
            timezone=raw.get("timezone", "UTC"),
            user_name=raw.get("user_name", "you"),
            calendar_ics_url=os.environ.get("JUNO_CALENDAR_URL")
            or raw.get("calendar_ics_url", ""),
            models=ModelConfig(**models_raw),
            voice=VoiceConfig(**(raw.get("voice") or {})),
            proactivity=ProactivityConfig(**proactivity),
            home_assistant=HomeAssistantConfig(**home),
        )
