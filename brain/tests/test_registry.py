from __future__ import annotations

import time

import pytest

from juno.orchestrator import STALE_AFTER_SECONDS, Device, DeviceRegistry
from juno.protocol import Capability


class FakeSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


def make(device_id: str, *caps: str, interacted: float | None = None) -> Device:
    d = Device(device_id=device_id, kind="laptop", capabilities=set(caps), socket=FakeSocket())
    if interacted is not None:
        d.last_interaction = interacted
    return d


def test_nearest_picks_the_most_recently_interacted_device():
    reg = DeviceRegistry()
    now = time.time()
    reg.add(make("laptop", "speak", interacted=now - 600))
    reg.add(make("phone", "speak", interacted=now - 5))

    assert reg.nearest().device_id == "phone"


def test_nearest_ignores_devices_that_cannot_speak():
    """A phone touched seconds ago still must not be chosen if it has no
    speaker capability — otherwise the reply goes nowhere."""
    reg = DeviceRegistry()
    now = time.time()
    reg.add(make("laptop", "speak", interacted=now - 600))
    reg.add(make("sensor", "camera", interacted=now))

    assert reg.nearest().device_id == "laptop"


def test_nearest_is_none_when_nothing_can_speak():
    reg = DeviceRegistry()
    reg.add(make("sensor", "camera"))
    assert reg.nearest() is None


def test_stale_devices_drop_out_of_listings():
    reg = DeviceRegistry()
    dead = make("laptop", "speak")
    dead.last_seen = time.time() - STALE_AFTER_SECONDS - 1
    reg.add(dead)

    assert reg.all() == []
    assert reg.nearest() is None
    # Still addressable by id, so a late result frame can be matched up.
    assert reg.get("laptop") is not None


def test_reconnecting_replaces_rather_than_duplicates():
    reg = DeviceRegistry()
    reg.add(make("laptop", "speak"))
    fresh = make("laptop", "speak", "listen")
    reg.add(fresh)

    assert len(reg.all()) == 1
    assert reg.get("laptop") is fresh


def test_with_capability_accepts_enum_or_string():
    reg = DeviceRegistry()
    reg.add(make("phone", "alarm"))

    assert len(reg.with_capability(Capability.ALARM)) == 1
    assert len(reg.with_capability("alarm")) == 1
    assert reg.with_capability("speak") == []


@pytest.mark.asyncio
async def test_send_writes_the_envelope_to_the_socket():
    from juno.protocol import speak as speak_frame

    device = make("laptop", "speak")
    await device.send(speak_frame("hello"))

    assert device.socket.sent[0]["type"] == "speak"
    assert device.socket.sent[0]["text"] == "hello"
