"""Turning window focus into timeline events.

The brain needs to know what you were doing, not every sample of it. So this
polls often (cheap, one IPC call) but only *reports* when the answer changes,
plus a heartbeat so a long unbroken session still shows continued presence.

That distinction is what keeps the whole system affordable: an hour in one app
becomes a handful of events instead of 360, and `Memory.digest` then collapses
even those into a single line.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from juno_laptop.windows import FocusWatcher, Window

log = logging.getLogger(__name__)

POLL_SECONDS = 5.0
# Re-report an unchanged window this often, so the brain can tell "still in
# Firefox" from "the client died an hour ago".
HEARTBEAT_SECONDS = 120.0
# A window focused for less than this is alt-tab noise, not activity. Without
# it, flicking through windows fills the timeline with entries nobody meant.
MIN_DWELL_SECONDS = 8.0


class FocusReporter:
    """Decides which focus changes are worth sending.

    Pure logic and clock-driven, so the debounce rules can be tested by feeding
    it timestamps rather than by waiting in real time.
    """

    def __init__(
        self,
        min_dwell: float = MIN_DWELL_SECONDS,
        heartbeat: float = HEARTBEAT_SECONDS,
    ) -> None:
        self.min_dwell = min_dwell
        self.heartbeat = heartbeat
        self._current: Window | None = None
        self._since = 0.0
        self._reported: Window | None = None
        self._last_report = 0.0

    def observe(self, window: Window | None, now: float) -> Window | None:
        """Feed one sample. Returns a window when it should be reported.

        ``None`` means the screen is locked, idle, or showing something
        redacted — all of which end the current run without starting a new one.
        """
        if window != self._current:
            self._current = window
            self._since = now
            return None

        if window is None:
            # The run is over — screen locked, idle, or something redacted.
            # Forgetting what was last reported means returning to the same
            # window reports it afresh, so the brain sees a resumption rather
            # than one unbroken session spanning the gap.
            self._reported = None
            return None

        dwelled = now - self._since
        if window != self._reported:
            # New window: report once it has held focus long enough to mean
            # something.
            if dwelled >= self.min_dwell:
                self._reported = window
                self._last_report = now
                return window
            return None

        if now - self._last_report >= self.heartbeat:
            self._last_report = now
            return window
        return None


class ScreenWatcher:
    def __init__(
        self,
        send: Callable[[dict], Awaitable[None]],
        watcher: FocusWatcher | None = None,
        poll_seconds: float = POLL_SECONDS,
    ) -> None:
        self.send = send
        self.watcher = watcher or FocusWatcher()
        self.poll_seconds = poll_seconds
        self.reporter = FocusReporter()

    @property
    def available(self) -> bool:
        return self.watcher.available

    async def run(self) -> None:
        if not self.available:
            return
        while True:
            try:
                window = await asyncio.to_thread(self.watcher.focused)
                report = self.reporter.observe(window, time.monotonic())
                if report is not None:
                    await self.send(
                        {
                            "type": "event",
                            "kind": "screen.focus",
                            "summary": report.summary(),
                            "app": report.app,
                        }
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a bad sample must not stop the watcher
                log.debug("screen poll failed: %s", exc)
            await asyncio.sleep(self.poll_seconds)
