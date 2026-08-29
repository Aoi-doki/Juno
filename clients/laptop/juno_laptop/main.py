"""Wires the microphone, the speaker and the brain together.

Everything is one asyncio process: the link reconnects on its own, the listener
owns the input device, the speaker owns the output device. They coordinate
through two small signals — ``speaking()`` and the interrupt event — rather
than sharing state.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from typing import Any

from juno_laptop.config import ClientConfig
from juno_laptop.link import Link
from juno_laptop.listen import Listener
from juno_laptop.speak import Speaker

log = logging.getLogger(__name__)

CAPABILITIES = ["speak", "listen"]


class Client:
    def __init__(self, config: ClientConfig) -> None:
        self.config = config
        self.speaker = Speaker(config)
        self.link = Link(config, CAPABILITIES, self._on_frame)
        self.listener = Listener(
            config,
            on_utterance=self._on_utterance,
            speaking=lambda: self.speaker.speaking,
            on_barge_in=self._on_barge_in,
        )
        # One reply at a time. Without this, a proactive nudge arriving while
        # she is answering a question would play both at once.
        self._speech_lock = asyncio.Lock()

    def _on_utterance(self, text: str) -> None:
        asyncio.create_task(
            self.link.send({"type": "utterance", "text": text, "final": True})
        )

    def _on_barge_in(self) -> None:
        log.info("user interrupted")
        self.speaker.stop()

    async def _on_frame(self, frame: dict[str, Any]) -> None:
        kind = frame.get("type")
        if kind == "welcome":
            log.info("registered with the brain")
            return
        if kind == "speak":
            text = str(frame.get("text", "")).strip()
            if text:
                asyncio.create_task(self._say(text))
            return
        if kind == "command":
            # Phase 2 adds screen and control handling here. Reply honestly
            # rather than silently, so the brain knows it went nowhere.
            await self.link.send(
                {
                    "type": "result",
                    "id": frame.get("id"),
                    "ok": False,
                    "detail": f"capability {frame.get('capability')!r} not implemented yet",
                }
            )
            return
        log.debug("ignoring frame %r", kind)

    async def _say(self, text: str) -> None:
        async with self._speech_lock:
            print(f"\n  Juno: {text}\n", flush=True)
            try:
                await self.speaker.say(text)
            except FileNotFoundError as exc:
                log.error("%s", exc)
            except Exception as exc:  # noqa: BLE001
                log.exception("playback failed: %s", exc)

    async def run(self) -> None:
        tasks = [asyncio.create_task(self.link.run()), asyncio.create_task(self.listener.run())]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(*tasks, return_exceptions=True)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        config = ClientConfig.load()
    except ValueError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    try:
        asyncio.run(Client(config).run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
