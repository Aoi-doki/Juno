"""The wire format between the brain and every client.

One JSON object per WebSocket frame, always carrying a ``type``. Both sides
import this module, so the schema cannot drift between them: the laptop client
depends on ``juno-brain`` purely for this file.

The vocabulary is deliberately small. Clients report *what happened* as events
and receive *what to do* as commands; nothing here knows about Kokoro, Whisper,
or Claude specifically.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


def _now() -> float:
    return time.time()


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


class DeviceKind(str, Enum):
    LAPTOP = "laptop"
    PHONE = "phone"
    SPEAKER = "speaker"


class Capability(str, Enum):
    """What a client can be asked to do. Sent at registration.

    The brain only issues a command to a device that declared the matching
    capability, so a client can implement a subset and be sent nothing it
    cannot handle.
    """

    SPEAK = "speak"            # play synthesised audio
    LISTEN = "listen"          # wake word + transcription
    NOTIFY = "notify"          # desktop or push notification
    ALARM = "alarm"            # full-screen, rings through Do Not Disturb
    SCREEN = "screen"          # report foreground window / OCR digest
    CAMERA = "camera"          # presence events, frame on request
    CONTROL = "control"        # launch apps, type, click
    USAGE = "usage"            # per-app foreground time (phone)


# --- client -> brain ---------------------------------------------------------

MsgHello = Literal["hello"]          # register: device_id, kind, capabilities
MsgUtterance = Literal["utterance"]  # the user said something
MsgEvent = Literal["event"]          # something was observed
MsgResult = Literal["result"]        # a command finished
MsgPong = Literal["pong"]

# --- brain -> client ---------------------------------------------------------

MsgSpeak = Literal["speak"]          # say this, now
MsgCommand = Literal["command"]      # run a capability with args
MsgPing = Literal["ping"]
MsgWelcome = Literal["welcome"]      # registration accepted


@dataclass(slots=True)
class Envelope:
    """Every frame on the wire.

    ``id`` correlates a command with its result. ``ts`` is brain-authoritative
    where the brain sent it, and best-effort client clock otherwise — clients
    are not trusted to have correct time, so the brain restamps events on
    arrival before they reach memory.
    """

    type: str
    id: str = field(default_factory=_new_id)
    ts: float = field(default_factory=_now)
    body: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {"type": self.type, "id": self.id, "ts": self.ts, **self.body}

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> Envelope:
        if not isinstance(raw, dict):
            raise ValueError("frame must be a JSON object")
        kind = raw.get("type")
        if not isinstance(kind, str) or not kind:
            raise ValueError("frame is missing a string 'type'")
        body = {k: v for k, v in raw.items() if k not in ("type", "id", "ts")}
        ts = raw.get("ts")
        return cls(
            type=kind,
            id=str(raw.get("id") or _new_id()),
            ts=float(ts) if isinstance(ts, (int, float)) else _now(),
            body=body,
        )


# --- helpers for the frames that get built most often ------------------------


def hello(device_id: str, kind: DeviceKind, capabilities: list[Capability]) -> Envelope:
    return Envelope(
        type="hello",
        body={
            "device_id": device_id,
            "kind": kind.value,
            "capabilities": [c.value for c in capabilities],
        },
    )


def utterance(text: str, *, final: bool = True) -> Envelope:
    """Something the user said. Partials let the brain show live transcription
    without committing them to memory."""
    return Envelope(type="utterance", body={"text": text, "final": final})


def event(kind: str, **fields: Any) -> Envelope:
    """An observation. ``kind`` is a dotted name such as ``screen.focus`` or
    ``usage.session`` — see ``memory.Timeline`` for how these are stored."""
    return Envelope(type="event", body={"kind": kind, **fields})


def speak(text: str, *, interruptible: bool = True, urgency: str = "normal") -> Envelope:
    """Say something. ``urgency`` picks the delivery: ``normal`` speaks at the
    current volume, ``alarm`` takes over the screen and overrides Do Not
    Disturb (phone only)."""
    return Envelope(
        type="speak",
        body={"text": text, "interruptible": interruptible, "urgency": urgency},
    )


def command(capability: Capability, **args: Any) -> Envelope:
    return Envelope(type="command", body={"capability": capability.value, "args": args})


def result(command_id: str, ok: bool, detail: Any = None) -> Envelope:
    return Envelope(type="result", id=command_id, body={"ok": ok, "detail": detail})
