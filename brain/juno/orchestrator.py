"""The hub. Devices connect here, report what they see, and get told what to do.

Runs on the always-on box. Every client holds one long-lived WebSocket; the
brain pushes to it whenever it has something to say, which is what makes
Juno able to speak first rather than only answering.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from juno.agent import Agent
from juno.config import Config
from juno.memory import Memory, TimelineRow
from juno.protocol import Capability, Envelope, speak as speak_frame

log = logging.getLogger(__name__)

# A device that has not spoken in this long is considered gone even if the
# socket never closed — laptops suspend without a clean disconnect.
STALE_AFTER_SECONDS = 180
PING_INTERVAL_SECONDS = 45


@dataclass
class Device:
    device_id: str
    kind: str
    capabilities: set[str]
    socket: WebSocket
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    # Distinct from last_seen: pings keep a device alive but do not make it the
    # one the user is actually sitting at.
    last_interaction: float = field(default_factory=time.time)

    async def send(self, envelope: Envelope) -> None:
        await self.socket.send_json(envelope.to_json())

    @property
    def stale(self) -> bool:
        return time.time() - self.last_seen > STALE_AFTER_SECONDS


class DeviceRegistry:
    """Who is connected, and which of them the user is most likely near."""

    def __init__(self) -> None:
        self._devices: dict[str, Device] = {}

    def add(self, device: Device) -> None:
        existing = self._devices.get(device.device_id)
        if existing is not None:
            # Reconnect after a suspend: drop the old socket rather than
            # keeping two and speaking to a dead one.
            log.info("device %s reconnected, replacing old session", device.device_id)
        self._devices[device.device_id] = device

    def remove(self, device_id: str) -> None:
        self._devices.pop(device_id, None)

    def get(self, device_id: str) -> Device | None:
        return self._devices.get(device_id)

    def all(self) -> list[Device]:
        return [d for d in self._devices.values() if not d.stale]

    def with_capability(self, cap: Capability | str) -> list[Device]:
        want = cap.value if isinstance(cap, Capability) else cap
        return [d for d in self.all() if want in d.capabilities]

    def nearest(self, cap: Capability | str = Capability.SPEAK) -> Device | None:
        """The device to speak on: whichever capable one the user touched last.

        Falling back to *any* capable device matters — after a night's sleep
        nothing has been interacted with recently, and saying the morning
        reminder somewhere is better than saying it nowhere.
        """
        candidates = self.with_capability(cap)
        if not candidates:
            return None
        return max(candidates, key=lambda d: d.last_interaction)

    def nearest_id(self) -> str | None:
        device = self.nearest()
        return device.device_id if device else None


def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="Juno")
    memory = Memory(config.db_path)
    registry = DeviceRegistry()
    agent = Agent(config, memory, registry)

    app.state.config = config
    app.state.memory = memory
    app.state.devices = registry
    app.state.agent = agent

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "devices": [
                {"id": d.device_id, "kind": d.kind, "capabilities": sorted(d.capabilities)}
                for d in registry.all()
            ],
            "spend_30d_usd": round(memory.spend_since(time.time() - 30 * 86400), 4),
        }

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        device: Device | None = None
        pinger: asyncio.Task[None] | None = None

        try:
            # The first frame must be a valid hello with the right token. Until
            # then the connection gets nothing and can do nothing.
            raw = await asyncio.wait_for(socket.receive_json(), timeout=10)
            envelope = Envelope.from_json(raw)
            if envelope.type != "hello":
                await socket.close(code=4400, reason="expected hello")
                return
            if envelope.body.get("token") != config.auth_token:
                log.warning("rejected connection with bad token")
                await socket.close(code=4401, reason="bad token")
                return

            device = Device(
                device_id=str(envelope.body.get("device_id", "unknown")),
                kind=str(envelope.body.get("kind", "laptop")),
                capabilities=set(envelope.body.get("capabilities", [])),
                socket=socket,
            )
            registry.add(device)
            log.info("device %s (%s) connected", device.device_id, device.kind)
            await device.send(Envelope(type="welcome", body={"name": "Juno"}))

            pinger = asyncio.create_task(_ping_loop(device))

            while True:
                raw = await socket.receive_json()
                device.last_seen = time.time()
                try:
                    frame = Envelope.from_json(raw)
                except ValueError as exc:
                    log.warning("bad frame from %s: %s", device.device_id, exc)
                    continue
                await _handle(frame, device, agent, memory, registry)

        except WebSocketDisconnect:
            pass
        except asyncio.TimeoutError:
            with contextlib.suppress(Exception):
                await socket.close(code=4408, reason="no hello")
        except Exception as exc:  # noqa: BLE001
            log.exception("websocket error: %s", exc)
        finally:
            if pinger is not None:
                pinger.cancel()
            if device is not None:
                registry.remove(device.device_id)
                log.info("device %s disconnected", device.device_id)

    return app


async def _ping_loop(device: Device) -> None:
    while True:
        await asyncio.sleep(PING_INTERVAL_SECONDS)
        try:
            await device.send(Envelope(type="ping"))
        except Exception:  # noqa: BLE001 - socket died; the reader will clean up
            return


async def _handle(
    frame: Envelope,
    device: Device,
    agent: Agent,
    memory: Memory,
    registry: DeviceRegistry,
) -> None:
    if frame.type == "pong":
        return

    if frame.type == "event":
        kind = str(frame.body.get("kind", "unknown"))
        summary = str(frame.body.get("summary") or json.dumps(frame.body))
        memory.add_event(
            # Restamped with brain time: clients' clocks are not trusted, and a
            # timeline that isn't monotonic breaks the run-collapsing in digest().
            TimelineRow(
                ts=time.time(),
                kind=kind,
                device_id=device.device_id,
                summary=summary,
                data=frame.body,
            )
        )
        return

    if frame.type == "utterance":
        if not frame.body.get("final", True):
            return  # partial transcript, shown live on the client only
        text = str(frame.body.get("text", "")).strip()
        if not text:
            return

        device.last_interaction = time.time()
        memory.add_turn("user", text, device_id=device.device_id)
        log.info("%s said: %s", device.device_id, text)

        reply = await agent.respond(text)
        if not reply.text:
            return
        memory.add_turn("juno", reply.text, device_id=device.device_id)

        # Reply where they spoke, unless that device cannot speak — in which
        # case anywhere is better than nowhere.
        target = device if "speak" in device.capabilities else registry.nearest()
        if target is not None:
            await target.send(speak_frame(reply.text))
        return

    if frame.type == "result":
        if not frame.body.get("ok", True):
            log.warning("command %s failed on %s: %s", frame.id, device.device_id,
                        frame.body.get("detail"))
        return

    log.debug("ignoring frame of type %r from %s", frame.type, device.device_id)
