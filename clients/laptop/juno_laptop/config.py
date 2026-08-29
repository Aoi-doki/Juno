"""Laptop client configuration.

Deliberately separate from the brain's config: this machine only needs to know
where the brain is and how to talk, not the persona or the budget.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_PATH = Path(os.environ.get("JUNO_CLIENT_CONFIG", "client.yaml"))

# Audio constants. 16 kHz mono is what openWakeWord, webrtcvad and Whisper all
# want, so resampling never enters the picture.
SAMPLE_RATE = 16_000
# openWakeWord consumes exactly 80 ms at a time; webrtcvad accepts 10/20/30 ms.
# 80 ms is a whole number of 20 ms frames, so one capture size feeds both.
FRAME_SAMPLES = 1280
VAD_FRAME_MS = 20


@dataclass(slots=True)
class ClientConfig:
    brain_url: str = "ws://juno-brain:8765/ws"
    token: str = ""
    device_id: str = ""
    kind: str = "laptop"

    # Voice
    wake_word: str = "hey_jarvis"
    wake_threshold: float = 0.6
    kokoro_voice: str = "af_heart"
    kokoro_speed: float = 1.0
    whisper_model: str = "base.en"
    whisper_compute: str = "int8"
    endpoint_silence_ms: int = 700
    # Stop listening for an utterance after this long even without silence,
    # so a noisy room cannot pin the microphone open forever.
    max_utterance_seconds: float = 20.0

    # Where kokoro-onnx model files live. Downloaded once; see the README.
    model_dir: Path = Path("models")

    # Barge-in: how many consecutive voiced frames during playback count as the
    # user interrupting. Three 20 ms frames is enough to beat a cough.
    barge_in_frames: int = 3
    enable_barge_in: bool = True

    @classmethod
    def load(cls, path: Path | None = None) -> ClientConfig:
        path = path or DEFAULT_PATH
        raw = {}
        if path.exists():
            raw = yaml.safe_load(path.read_text()) or {}

        token = os.environ.get("JUNO_AUTH_TOKEN") or raw.get("token", "")
        if not token:
            raise ValueError(
                "No auth token. Set JUNO_AUTH_TOKEN or token in client.yaml — it must "
                "match the brain's."
            )

        raw.pop("token", None)
        model_dir = Path(raw.pop("model_dir", "models"))
        device_id = raw.pop("device_id", "") or socket.gethostname()

        return cls(token=token, device_id=device_id, model_dir=model_dir, **raw)
