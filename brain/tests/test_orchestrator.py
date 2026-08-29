"""End-to-end over a real WebSocket, using FastAPI's test transport.

These cover the handshake and the event path — the parts that talk to actual
devices and so cannot be checked by unit tests alone.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from juno.config import Config, ModelConfig
from juno.orchestrator import create_app
from juno.protocol import Capability, DeviceKind, event, hello

TOKEN = "test-token"


@pytest.fixture()
def client(tmp_path):
    config = Config(
        db_path=tmp_path / "juno.db",
        auth_token=TOKEN,
        user_name="Aoi",
        models=ModelConfig(),
    )
    app = create_app(config)
    with TestClient(app) as c:
        yield c


def connect(client, device_id="laptop-1", caps=(Capability.SPEAK, Capability.LISTEN)):
    ws = client.websocket_connect("/ws")
    socket = ws.__enter__()
    frame = hello(device_id, DeviceKind.LAPTOP, list(caps)).to_json()
    frame["token"] = TOKEN
    socket.send_json(frame)
    return ws, socket


def test_valid_hello_is_welcomed_and_listed(client):
    ws, socket = connect(client)
    try:
        assert socket.receive_json()["type"] == "welcome"

        health = client.get("/health").json()
        assert health["ok"] is True
        assert health["devices"][0]["id"] == "laptop-1"
        assert health["devices"][0]["capabilities"] == ["listen", "speak"]
    finally:
        ws.__exit__(None, None, None)


def test_a_wrong_token_is_refused(client):
    with client.websocket_connect("/ws") as socket:
        frame = hello("intruder", DeviceKind.LAPTOP, []).to_json()
        frame["token"] = "not-the-token"
        socket.send_json(frame)

        with pytest.raises(Exception):
            socket.receive_json()

    assert client.get("/health").json()["devices"] == []


def test_a_first_frame_that_is_not_hello_is_refused(client):
    with client.websocket_connect("/ws") as socket:
        socket.send_json(event("screen.focus", summary="sneaky").to_json())
        with pytest.raises(Exception):
            socket.receive_json()

    assert client.get("/health").json()["devices"] == []


def test_events_reach_the_timeline(client):
    ws, socket = connect(client)
    try:
        socket.receive_json()  # welcome
        socket.send_json(event("screen.focus", summary="Ghostty — nvim").to_json())

        # The handler runs on the server task; give it a moment to commit.
        deadline = time.time() + 2
        rows: list = []
        while time.time() < deadline and not rows:
            rows = client.app.state.memory.events_since(time.time() - 60)
        assert [r["summary"] for r in rows] == ["Ghostty — nvim"]
        assert rows[0]["device_id"] == "laptop-1"
    finally:
        ws.__exit__(None, None, None)


def test_disconnecting_removes_the_device(client):
    ws, socket = connect(client)
    socket.receive_json()
    assert len(client.get("/health").json()["devices"]) == 1

    ws.__exit__(None, None, None)

    deadline = time.time() + 2
    while time.time() < deadline and client.get("/health").json()["devices"]:
        pass
    assert client.get("/health").json()["devices"] == []


def test_malformed_frames_do_not_kill_the_connection(client):
    """A client bug must not take the socket down — the device would then be
    unreachable until it noticed and reconnected."""
    ws, socket = connect(client)
    try:
        socket.receive_json()
        socket.send_json({"no_type_field": True})
        socket.send_json(event("screen.focus", summary="still here").to_json())

        deadline = time.time() + 2
        rows: list = []
        while time.time() < deadline and not rows:
            rows = client.app.state.memory.events_since(time.time() - 60)
        assert [r["summary"] for r in rows] == ["still here"]
    finally:
        ws.__exit__(None, None, None)
