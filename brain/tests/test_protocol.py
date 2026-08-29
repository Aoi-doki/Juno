from __future__ import annotations

import pytest

from juno.protocol import Capability, DeviceKind, Envelope, event, hello, result, speak, utterance


def test_round_trip_preserves_body_and_id():
    original = speak("time to stop scrolling", urgency="alarm")
    restored = Envelope.from_json(original.to_json())

    assert restored.type == "speak"
    assert restored.id == original.id
    assert restored.body["text"] == "time to stop scrolling"
    assert restored.body["urgency"] == "alarm"


def test_type_and_metadata_do_not_leak_into_body():
    restored = Envelope.from_json(utterance("hello").to_json())
    assert set(restored.body) == {"text", "final"}


@pytest.mark.parametrize("bad", [{}, {"type": ""}, {"type": 3}, [], "nope"])
def test_frames_without_a_usable_type_are_rejected(bad):
    with pytest.raises(ValueError):
        Envelope.from_json(bad)


def test_missing_id_and_ts_are_generated():
    restored = Envelope.from_json({"type": "event", "kind": "screen.focus"})
    assert restored.id
    assert restored.ts > 0


def test_a_client_clock_of_the_wrong_type_does_not_crash_the_hub():
    restored = Envelope.from_json({"type": "event", "ts": "not-a-number"})
    assert isinstance(restored.ts, float)


def test_hello_serialises_enums_as_plain_strings():
    frame = hello("laptop-1", DeviceKind.LAPTOP, [Capability.SPEAK, Capability.LISTEN])
    payload = frame.to_json()

    assert payload["kind"] == "laptop"
    assert payload["capabilities"] == ["speak", "listen"]
    assert all(isinstance(c, str) for c in payload["capabilities"])


def test_result_correlates_with_the_command_id():
    assert result("abc123", ok=False, detail="no mic").id == "abc123"


def test_event_kind_travels_in_the_body():
    frame = event("usage.session", app="com.instagram.android", minutes=42)
    assert frame.body == {"kind": "usage.session", "app": "com.instagram.android", "minutes": 42}
