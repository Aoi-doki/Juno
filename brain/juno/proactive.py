"""When Juno speaks without being spoken to.

Three sources of initiative, in increasing order of how much judgement they
need:

1. **Deterministic** — a calendar event is in ten minutes. No model required.
2. **Rule-triggered** — you've been on Instagram for half an hour; you said you
   were writing the report and you're in a different app.
3. **Discretionary** — a periodic check-in where the model looks at the recent
   timeline and decides whether anything is worth saying. Usually it isn't, and
   the prompt says so in as many words.

The hardest part of this file is not deciding when to speak. It's the machinery
for *not* speaking: quiet hours, presence gating, snooze, repeat suppression.
An assistant that nags is uninstalled within a week, and the only thing
protecting against that is the code below.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import IntEnum
from typing import Any

log = logging.getLogger(__name__)


class Tier(IntEnum):
    """The escalation ladder. Each rung is louder than the last, and tier 4 is
    rationed — fire it for ordinary distraction and it becomes noise."""

    GENTLE = 1
    FIRM = 2
    BLUNT = 3
    ALARM = 4


# Minutes of unbroken use before each tier, when the app has no explicit
# threshold in config.
DEFAULT_LADDER = {Tier.GENTLE: 15, Tier.FIRM: 30, Tier.BLUNT: 45}

# Never nag about the same subject more often than this, whatever the rules
# say. Repetition is what makes people stop listening.
REPEAT_SUPPRESSION_SECONDS = 8 * 60

# A snooze is honoured, then it comes back. Each successive snooze on the same
# subject buys less time — asking five times in a row is itself information.
SNOOZE_SECONDS = (10 * 60, 7 * 60, 4 * 60, 2 * 60)


def in_quiet_hours(now: datetime, quiet: tuple[int, int]) -> bool:
    """Whether ``now`` falls in the quiet window.

    Handles the normal case where quiet hours wrap past midnight (23 to 8),
    which a naive ``start <= hour < end`` gets exactly backwards.
    """
    start, end = quiet
    hour = now.hour
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def scroll_tier(minutes: float, thresholds: dict[Tier, int]) -> Tier | None:
    """Highest tier earned by this much unbroken scrolling."""
    earned = [tier for tier, limit in thresholds.items() if minutes >= limit]
    return max(earned) if earned else None


def thresholds_for(app: str, config_apps: dict[str, int]) -> dict[Tier, int]:
    """Per-app ladder.

    Config gives one number — when the *first* nudge lands — and the rest scale
    from it, so tuning an app is one number rather than three.
    """
    first = config_apps.get(app)
    if first is None:
        return dict(DEFAULT_LADDER)
    return {Tier.GENTLE: first, Tier.FIRM: first * 2, Tier.BLUNT: first * 3}


@dataclass(slots=True)
class Nag:
    """One thing Juno might say, and how insistently."""

    subject: str          # what this is about, for repeat suppression
    tier: Tier
    prompt: str           # what the model is asked to say something about


@dataclass(slots=True)
class Gate:
    """Everything that stops her talking.

    Kept separate from the rules that decide *what* to say so the two can be
    reasoned about independently — and so the suppression logic is testable
    without a model in the loop.
    """

    quiet_hours: tuple[int, int] = (23, 8)
    repeat_suppression: float = REPEAT_SUPPRESSION_SECONDS
    _last_spoken: dict[str, float] = field(default_factory=dict)
    _snoozed_until: dict[str, float] = field(default_factory=dict)
    _snooze_count: dict[str, int] = field(default_factory=dict)

    def allows(
        self,
        nag: Nag,
        *,
        now: float,
        clock: datetime,
        present: bool | None,
    ) -> tuple[bool, str]:
        """Whether this nag may be delivered, and why not if not."""
        # An alarm-tier nag is what quiet hours are *for* — a commitment at
        # 07:30 has to be able to wake you. Everything below it stays silent.
        if nag.tier < Tier.ALARM and in_quiet_hours(clock, self.quiet_hours):
            return False, "quiet hours"

        until = self._snoozed_until.get(nag.subject)
        if until is not None and now < until:
            return False, f"snoozed for another {int(until - now)}s"

        last = self._last_spoken.get(nag.subject)
        if last is not None and now - last < self.repeat_suppression:
            return False, "said recently"

        # Talking to an empty room is worse than saying nothing: she is not
        # heard, but she believes she was, and escalates from there.
        if present is False and nag.tier < Tier.ALARM:
            return False, "nobody there"

        return True, ""

    def record_spoken(self, subject: str, now: float) -> None:
        self._last_spoken[subject] = now
        # A fresh delivery resets the snooze ladder for that subject.
        self._snooze_count.pop(subject, None)

    def snooze(self, subject: str, now: float) -> float:
        """Accept a 'not now'. Returns how many seconds were granted."""
        count = self._snooze_count.get(subject, 0)
        granted = SNOOZE_SECONDS[min(count, len(SNOOZE_SECONDS) - 1)]
        self._snooze_count[subject] = count + 1
        self._snoozed_until[subject] = now + granted
        return granted

    def clear(self, subject: str) -> None:
        """Drop all state for a subject — it stopped being true."""
        self._last_spoken.pop(subject, None)
        self._snoozed_until.pop(subject, None)
        self._snooze_count.pop(subject, None)


TIER_STYLE = {
    Tier.GENTLE: (
        "Nudge them once, lightly. One short sentence, genuinely curious rather than "
        "disapproving. If they say they meant to be doing this, drop it."
    ),
    Tier.FIRM: (
        "Be direct. Name how long it has been and what they said they would be doing "
        "instead. Offer one concrete alternative. Still two sentences at most."
    ),
    Tier.BLUNT: (
        "Short and blunt. They have ignored you twice. Do not be cruel and do not "
        "lecture — one flat sentence about the gap between what they said and what "
        "they are doing lands harder than a paragraph."
    ),
    Tier.ALARM: (
        "This is the loud one, and it takes over their screen. Say what is wrong in "
        "one sentence, and what to do about it in another. Nothing else."
    ),
}


def build_prompt(nag: Nag, digest: str, plan: str | None) -> str:
    """The turn handed to the model when a rule fires.

    The rule has already decided *that* she speaks; the model only decides
    *what she says*. Keeping that boundary means a bug in the model's judgement
    cannot make her chattier — only differently worded.
    """
    parts = [
        nag.prompt,
        "",
        f"Their recent activity:\n{digest}",
    ]
    if plan:
        parts += ["", f"What they said they were doing: {plan}"]
    parts += [
        "",
        TIER_STYLE[nag.tier],
        "",
        "Say it out loud, in your own voice. No preamble, no markdown.",
    ]
    return "\n".join(parts)


def check_in_delay(config_range: tuple[int, int]) -> float:
    """Seconds until the next discretionary check-in.

    Jittered on purpose. A check-in exactly every 30 minutes becomes furniture
    — you stop hearing it — and it also means every drift gets noticed at the
    same phase of the clock rather than when it happens.
    """
    low, high = config_range
    return random.uniform(low * 60, high * 60)


CHECK_IN_PROMPT = """\
This is a periodic check-in. Nobody asked you anything.

Look at what they have been doing and decide whether anything is worth saying \
right now. Most of the time it is not, and staying quiet is the right answer — \
you are not here to fill silence.

Say something only if there is a real reason: they are drifting from something \
they told you they were doing, they have been at the screen without a break for \
a long time, something on their calendar needs attention, or a commitment they \
made to you has come due.

If there is nothing, reply with exactly: SILENCE

Their recent activity:
{digest}
"""

# The sentinel the model returns when a check-in has nothing worth saying. It
# is checked for exactly, and anything else is treated as speech.
SILENCE = "SILENCE"


def is_silence(reply: str) -> bool:
    """Whether a check-in reply means 'say nothing'.

    Tolerant of the model adding punctuation or wrapping the word, because a
    reply of "SILENCE." should not be spoken aloud as the word "silence".
    """
    stripped = reply.strip().strip(".!\"'`*").upper()
    return stripped == SILENCE or not stripped
