"""Reading your calendar.

Deliberately an **ICS URL** rather than full CalDAV. Every calendar worth
syncing — Google, Radicale, Nextcloud, Fastmail — exposes a private
subscription URL, and reading one is a GET plus a parse. CalDAV would mean
authentication flows and PROPFIND for the same answer.

Recurrence is handled by ``recurring-ical-events`` rather than by interpreting
RRULE here. Getting recurrence subtly wrong means silently missing a weekly
meeting, which is the exact failure this feature exists to prevent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

log = logging.getLogger(__name__)

FETCH_TIMEOUT = 20.0


@dataclass(frozen=True, slots=True)
class Event:
    summary: str
    start: datetime
    end: datetime
    location: str = ""

    @property
    def all_day(self) -> bool:
        return (self.end - self.start) >= timedelta(days=1) and self.start.hour == 0

    def minutes_until(self, now: datetime) -> float:
        return (self.start - now).total_seconds() / 60

    def describe(self, now: datetime) -> str:
        if self.all_day:
            return f"{self.summary} (all day)"
        minutes = self.minutes_until(now)
        when = (
            f"in {int(minutes)} min"
            if 0 <= minutes < 90
            else self.start.strftime("%H:%M")
        )
        where = f" at {self.location}" if self.location else ""
        return f"{self.summary} — {when}{where}"


def _as_datetime(value, fallback_tz: timezone) -> datetime:
    """Normalise icalendar's date-or-datetime into an aware datetime.

    All-day events come back as ``date``, and comparing those to an aware
    datetime raises — which would take down the whole scheduler tick.
    """
    from datetime import date

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=fallback_tz)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=fallback_tz)
    raise TypeError(f"unexpected calendar time {value!r}")


class Calendar:
    """Fetches and caches upcoming events.

    Cached because the scheduler asks constantly and the calendar changes
    rarely; a stale-by-ten-minutes view is fine for "what's coming up", and it
    keeps a flaky network from turning into a flaky assistant.
    """

    def __init__(self, ics_url: str | None, cache_seconds: float = 600.0) -> None:
        self.ics_url = ics_url
        self.cache_seconds = cache_seconds
        self._cache: list[Event] = []
        self._fetched_at: datetime | None = None

    @property
    def configured(self) -> bool:
        return bool(self.ics_url)

    async def upcoming(self, within_hours: int = 24, now: datetime | None = None) -> list[Event]:
        now = now or datetime.now(timezone.utc).astimezone()
        if not self.configured:
            return []

        stale = (
            self._fetched_at is None
            or (now - self._fetched_at).total_seconds() > self.cache_seconds
        )
        if stale:
            fetched = await self._fetch(now, within_hours)
            if fetched is not None:
                self._cache = fetched
                self._fetched_at = now

        horizon = now + timedelta(hours=within_hours)
        return [e for e in self._cache if now <= e.start <= horizon or e.start <= now <= e.end]

    async def _fetch(self, now: datetime, within_hours: int) -> list[Event] | None:
        """Returns None on failure, so the caller keeps the previous cache
        rather than concluding the calendar is empty."""
        try:
            import icalendar
            import recurring_ical_events
        except ImportError:
            log.warning("calendar extras not installed; calendar features off")
            return None

        try:
            async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True) as http:
                response = await http.get(self.ics_url)
                response.raise_for_status()
                raw = response.content
        except Exception as exc:  # noqa: BLE001
            log.warning("calendar fetch failed: %s", exc)
            return None

        try:
            calendar = icalendar.Calendar.from_ical(raw)
            window_start = now - timedelta(hours=2)
            window_end = now + timedelta(hours=within_hours + 24)
            occurrences = recurring_ical_events.of(calendar).between(window_start, window_end)
        except Exception as exc:  # noqa: BLE001
            log.warning("calendar parse failed: %s", exc)
            return None

        tz = now.tzinfo or timezone.utc
        events: list[Event] = []
        for item in occurrences:
            try:
                start = _as_datetime(item.get("DTSTART").dt, tz)
                end_prop = item.get("DTEND")
                end = _as_datetime(end_prop.dt, tz) if end_prop else start + timedelta(hours=1)
                events.append(
                    Event(
                        summary=str(item.get("SUMMARY", "(untitled)")),
                        start=start,
                        end=end,
                        location=str(item.get("LOCATION", "")),
                    )
                )
            except (AttributeError, TypeError, ValueError) as exc:
                log.debug("skipping malformed calendar entry: %s", exc)

        events.sort(key=lambda e: e.start)
        log.info("calendar: %d events in the next %dh", len(events), within_hours)
        return events
