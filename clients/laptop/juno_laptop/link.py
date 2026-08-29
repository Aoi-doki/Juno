"""The connection to the brain, with reconnection that survives a suspend.

A laptop lid closes mid-sentence and reopens hours later on a different
network. The client has to treat that as normal rather than as an error, so
this reconnects forever with backoff and re-registers on every attempt.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

import websockets

from juno_laptop.config import ClientConfig

log = logging.getLogger(__name__)

INITIAL_BACKOFF = 1.0
MAX_BACKOFF = 60.0


def backoff_delays(
    initial: float = INITIAL_BACKOFF, maximum: float = MAX_BACKOFF, factor: float = 2.0
):
    """Doubling backoff, capped.

    A generator so the schedule is testable without waiting for it, and so the
    cap is enforced in one place rather than at every call site.
    """
    delay = initial
    while True:
        yield delay
        delay = min(delay * factor, maximum)


class Link:
    def __init__(
        self,
        config: ClientConfig,
        capabilities: list[str],
        on_frame: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self.config = config
        self.capabilities = capabilities
        self._on_frame = on_frame
        self._socket: websockets.WebSocketClientProtocol | None = None
        self._connected = asyncio.Event()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    async def send(self, frame: dict[str, Any]) -> None:
        """Drop rather than queue when offline.

        Voice is only meaningful live: replaying an utterance from twenty
        minutes ago when the link comes back would be worse than losing it.
        """
        socket = self._socket
        if socket is None:
            log.warning("not connected; dropping %s frame", frame.get("type"))
            return
        try:
            await socket.send(json.dumps(frame))
        except Exception as exc:  # noqa: BLE001
            log.warning("send failed: %s", exc)

    async def run(self) -> None:
        delays = backoff_delays()
        while True:
            try:
                await self._session()
                # A clean close still means reconnect, but immediately — the
                # brain restarting shouldn't cost a minute of deafness.
                delays = backoff_delays()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("connection lost (%s)", exc)
            finally:
                self._socket = None
                self._connected.clear()

            delay = next(delays)
            log.info("reconnecting in %.0fs", delay)
            await asyncio.sleep(delay)

    async def _session(self) -> None:
        log.info("connecting to %s", self.config.brain_url)
        async with websockets.connect(
            self.config.brain_url, ping_interval=30, ping_timeout=30, max_size=2**22
        ) as socket:
            await socket.send(
                json.dumps(
                    {
                        "type": "hello",
                        "token": self.config.token,
                        "device_id": self.config.device_id,
                        "kind": self.config.kind,
                        "capabilities": self.capabilities,
                    }
                )
            )
            self._socket = socket
            self._connected.set()
            log.info("connected as %s", self.config.device_id)

            async for raw in socket:
                try:
                    frame = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning("unparseable frame from brain")
                    continue
                if frame.get("type") == "ping":
                    await socket.send(json.dumps({"type": "pong", "id": frame.get("id")}))
                    continue
                await self._on_frame(frame)
