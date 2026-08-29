"""The loop that gives Juno initiative.

Ticks once a minute and asks three questions: is anything on the calendar
imminent, has any rule fired, and is it time for a discretionary check-in.
Anything it decides to say goes through ``Gate`` first, which is what stops
this becoming a nuisance.

Escalation is driven from here rather than from the model. A tier is earned by
elapsed time and by being ignored — not by the model deciding it feels
strongly, which would make loudness a function of phrasing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from juno.agent import Agent
from juno.calendar import Calendar
from juno.config import Config
from juno.memory import Memory
from juno.proactive import (
    CHECK_IN_PROMPT,
    Gate,
    Nag,
    Tier,
    build_prompt,
    check_in_delay,
    is_silence,
    scroll_tier,
    thresholds_for,
)
from juno.protocol import Capability, speak

log = logging.getLogger(__name__)

TICK_SECONDS = 60.0
# Warn this long before a calendar event. Enough to actually get somewhere.
CALENDAR_WARN_MINUTES = 10
# A commitment you are already late for escalates to the loud tier.
CALENDAR_ALARM_MINUTES = 0


class Scheduler:
    def __init__(
        self,
        config: Config,
        memory: Memory,
        devices,
        agent: Agent,
        calendar: Calendar | None = None,
    ) -> None:
        self.config = config
        self.memory = memory
        self.devices = devices
        self.agent = agent
        self.calendar = calendar or Calendar(None)
        self.gate = Gate(quiet_hours=config.proactivity.quiet_hours)
        self._next_check_in = time.monotonic() + check_in_delay(
            config.proactivity.check_in_minutes
        )
        self._announced: set[str] = set()

    # --- state read off the timeline ----------------------------------------

    def _present(self) -> bool | None:
        """Whether they are at the desk. ``None`` when the camera isn't
        reporting, which must not be read as absence."""
        rows = self.memory.events_since(time.time() - 900, kinds=["camera.presence"])
        if not rows:
            return None
        return rows[-1]["summary"].startswith("at the desk")

    def _current_scroll(self) -> tuple[str, float] | None:
        """The app being scrolled and for how many minutes, if any.

        Reads phone usage events rather than tracking state here, so a restart
        of the brain doesn't reset someone's ongoing session to zero.
        """
        rows = self.memory.events_since(time.time() - 4 * 3600, kinds=["usage.session"])
        if not rows:
            return None
        latest = rows[-1]
        # The phone reports a running total for the current session and stops
        # reporting when the session ends, so a stale row means it's over.
        if time.time() - latest["ts"] > 180:
            return None
        try:
            import json

            data = json.loads(latest["summary"]) if latest["summary"].startswith("{") else None
        except ValueError:
            data = None
        if data:
            return str(data.get("app", "")), float(data.get("minutes", 0))
        # Fall back to the summary text the phone sent, e.g.
        # "Instagram — 42m".
        parts = latest["summary"].rsplit("—", 1)
        if len(parts) != 2:
            return None
        try:
            return parts[0].strip(), float(parts[1].strip().rstrip("m"))
        except ValueError:
            return None

    def _stated_plan(self) -> str | None:
        row = next(
            (f for f in self.memory.all_facts(limit=50) if f["subject"] == "current task"), None
        )
        return row["body"] if row else None

    # --- rules ---------------------------------------------------------------

    async def _calendar_nags(self, now: datetime) -> list[Nag]:
        nags: list[Nag] = []
        for event in await self.calendar.upcoming(within_hours=2, now=now):
            if event.all_day:
                continue
            minutes = event.minutes_until(now)
            key = f"calendar:{event.summary}:{event.start.isoformat()}"

            if CALENDAR_ALARM_MINUTES >= minutes > -15:
                tier = Tier.ALARM
            elif 0 < minutes <= CALENDAR_WARN_MINUTES:
                tier = Tier.FIRM
            else:
                continue

            # Each event announces once per tier, not once per tick.
            marker = f"{key}:{tier}"
            if marker in self._announced:
                continue
            self._announced.add(marker)
            nags.append(
                Nag(
                    subject=key,
                    tier=tier,
                    prompt=f"They have a commitment: {event.describe(now)}.",
                )
            )
        return nags

    def _scroll_nag(self) -> Nag | None:
        current = self._current_scroll()
        if current is None:
            return None
        app, minutes = current
        thresholds = thresholds_for(app, self.config.proactivity.scroll_apps)
        tier = scroll_tier(minutes, thresholds)
        if tier is None:
            return None
        return Nag(
            subject=f"scroll:{app}",
            tier=tier,
            prompt=(
                f"They have been on {app} for {int(minutes)} minutes without a break."
            ),
        )

    # --- delivery ------------------------------------------------------------

    async def _deliver(self, nag: Nag, now: float, clock: datetime) -> None:
        allowed, why = self.gate.allows(
            nag, now=now, clock=clock, present=self._present()
        )
        if not allowed:
            log.info("holding %s (%s)", nag.subject, why)
            return

        digest = self.memory.digest(since=time.time() - 2 * 3600)
        reply = await self.agent.respond(
            build_prompt(nag, digest, self._stated_plan()), history=6
        )
        if not reply.text or is_silence(reply.text):
            return

        urgency = "alarm" if nag.tier is Tier.ALARM else "normal"
        capability = Capability.ALARM if nag.tier is Tier.ALARM else Capability.SPEAK
        target = self.devices.nearest(capability) or self.devices.nearest()
        if target is None:
            log.info("nothing to say %s on — no device connected", nag.subject)
            return

        await target.send(speak(reply.text, urgency=urgency))
        self.memory.add_turn("juno", reply.text, device_id=target.device_id)
        self.gate.record_spoken(nag.subject, now)
        log.info("tier %d on %s: %s", nag.tier, target.device_id, reply.text)

    async def _check_in(self, now: float, clock: datetime) -> None:
        nag = Nag(subject="check-in", tier=Tier.GENTLE, prompt="")
        allowed, why = self.gate.allows(nag, now=now, clock=clock, present=self._present())
        if not allowed:
            log.debug("skipping check-in (%s)", why)
            return

        digest = self.memory.digest(since=time.time() - 90 * 60)
        reply = await self.agent.respond(CHECK_IN_PROMPT.format(digest=digest), history=8)
        if is_silence(reply.text):
            log.debug("check-in: nothing to say")
            return

        target = self.devices.nearest()
        if target is None:
            return
        await target.send(speak(reply.text))
        self.memory.add_turn("juno", reply.text, device_id=target.device_id)
        self.gate.record_spoken("check-in", now)
        log.info("check-in on %s: %s", target.device_id, reply.text)

    # --- the loop ------------------------------------------------------------

    async def run(self) -> None:
        log.info(
            "proactivity level %d, quiet hours %02d:00-%02d:00",
            self.config.proactivity.level,
            *self.config.proactivity.quiet_hours,
        )
        while True:
            await asyncio.sleep(TICK_SECONDS)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - one bad tick must not end the loop
                log.exception("scheduler tick failed: %s", exc)

    async def tick(self) -> None:
        if self.config.proactivity.level == 0:
            return

        now = time.monotonic()
        clock = datetime.now(timezone.utc).astimezone()

        for nag in await self._calendar_nags(clock):
            await self._deliver(nag, now, clock)

        scroll = self._scroll_nag()
        if scroll is not None:
            await self._deliver(scroll, now, clock)
        else:
            # The session ended; forget it so the next one starts fresh rather
            # than being suppressed by the last one's history.
            for subject in [s for s in self.gate._last_spoken if s.startswith("scroll:")]:
                self.gate.clear(subject)

        if now >= self._next_check_in:
            self._next_check_in = now + check_in_delay(self.config.proactivity.check_in_minutes)
            await self._check_in(now, clock)
