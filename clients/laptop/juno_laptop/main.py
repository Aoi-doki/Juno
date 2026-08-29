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

from juno_laptop.camera import CameraWatcher
from juno_laptop.config import ClientConfig
from juno_laptop.control import Controller, Outcome
from juno_laptop.link import Link
from juno_laptop.listen import Listener
from juno_laptop.screen import ScreenWatcher
from juno_laptop.speak import Speaker

log = logging.getLogger(__name__)


class Client:
    def __init__(self, config: ClientConfig) -> None:
        self.config = config
        self.speaker = Speaker(config)
        self.screen = ScreenWatcher(send=lambda frame: self.link.send(frame))
        self.control = Controller(allow_input_synthesis=config.allow_input_synthesis)
        self.camera = CameraWatcher(
            send=lambda frame: self.link.send(frame),
            index=config.camera_index,
            poll_seconds=config.camera_poll_seconds,
        ) if config.enable_camera else None

        capabilities = ["speak", "listen"]
        # Only claim a capability we can actually serve — the brain routes on
        # what devices declare, and claiming one we cannot serve means it waits
        # on results that never come.
        if self.screen.available:
            capabilities.append("screen")
        capabilities.extend(self.control.capabilities)
        if self.camera is not None and self.camera.available:
            capabilities.append("camera")

        self.link = Link(config, capabilities, self._on_frame)
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
            outcome = await self._run_command(frame)
            await self.link.send(
                {
                    "type": "result",
                    "id": frame.get("id"),
                    "ok": outcome.ok,
                    "detail": outcome.detail,
                }
            )
            return
        log.debug("ignoring frame %r", kind)

    async def _run_command(self, frame: dict[str, Any]) -> Outcome:
        """Dispatch one command frame to the controller.

        Unknown capabilities and actions return an honest failure rather than
        being ignored, so the brain learns what this device cannot do instead
        of waiting on a result that never comes.
        """
        capability = frame.get("capability")
        args = frame.get("args") or {}
        if capability != "control":
            return Outcome(False, f"capability {capability!r} is not supported here")

        action = str(args.get("action", ""))
        try:
            if action == "notify":
                return await self.control.notify(
                    str(args.get("summary", "Juno")),
                    str(args.get("body", "")),
                    bool(args.get("urgent", False)),
                )
            if action == "launch":
                return await self.control.launch(str(args["application"]))
            if action == "read_clipboard":
                return await self.control.read_clipboard()
            if action == "write_clipboard":
                return await self.control.write_clipboard(str(args["text"]))
            if action == "type":
                return await self.control.type_text(str(args["text"]))
            if action == "press":
                return await self.control.press(str(args["keys"]))
        except KeyError as exc:
            return Outcome(False, f"missing argument {exc}")
        return Outcome(False, f"unknown action {action!r}")

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
        if self.screen.available:
            tasks.append(asyncio.create_task(self.screen.run()))
        if self.camera is not None and self.camera.available:
            tasks.append(asyncio.create_task(self.camera.run()))
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
