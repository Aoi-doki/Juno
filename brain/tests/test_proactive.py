"""The rules that decide whether Juno speaks.

Most of these test her staying *quiet*. An assistant that nags gets
uninstalled, and this logic is the only thing preventing that.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from juno.proactive import (
    DEFAULT_LADDER,
    Gate,
    Nag,
    Tier,
    build_prompt,
    check_in_delay,
    in_quiet_hours,
    is_silence,
    scroll_tier,
    thresholds_for,
)


def at(hour: int) -> datetime:
    return datetime(2026, 8, 29, hour, 30)


class TestQuietHours:
    @pytest.mark.parametrize("hour", [23, 0, 3, 7])
    def test_overnight_window_wraps_past_midnight(self, hour):
        """The common case, and the one a naive start <= h < end gets backwards."""
        assert in_quiet_hours(at(hour), (23, 8)) is True

    @pytest.mark.parametrize("hour", [8, 12, 18, 22])
    def test_daytime_is_not_quiet(self, hour):
        assert in_quiet_hours(at(hour), (23, 8)) is False

    def test_a_same_day_window_works_too(self):
        assert in_quiet_hours(at(14), (13, 15)) is True
        assert in_quiet_hours(at(16), (13, 15)) is False

    def test_an_empty_window_is_never_quiet(self):
        assert in_quiet_hours(at(3), (8, 8)) is False


class TestScrollLadder:
    def test_no_tier_below_the_first_threshold(self):
        assert scroll_tier(10, DEFAULT_LADDER) is None

    @pytest.mark.parametrize(
        "minutes,expected",
        [(15, Tier.GENTLE), (29, Tier.GENTLE), (30, Tier.FIRM), (44, Tier.FIRM), (45, Tier.BLUNT)],
    )
    def test_tier_rises_with_time(self, minutes, expected):
        assert scroll_tier(minutes, DEFAULT_LADDER) is expected

    def test_the_ladder_never_reaches_alarm_on_its_own(self):
        """Tier 4 takes over the screen. Ordinary scrolling must never earn it,
        or it stops meaning anything."""
        assert scroll_tier(600, DEFAULT_LADDER) is Tier.BLUNT

    def test_per_app_threshold_scales_the_whole_ladder(self):
        thresholds = thresholds_for("com.zhiliaoapp.musically", {"com.zhiliaoapp.musically": 10})
        assert thresholds == {Tier.GENTLE: 10, Tier.FIRM: 20, Tier.BLUNT: 30}

    def test_unlisted_apps_get_the_default_ladder(self):
        assert thresholds_for("org.gnome.Calculator", {}) == DEFAULT_LADDER


class TestGate:
    def _nag(self, tier=Tier.GENTLE, subject="scroll:instagram") -> Nag:
        return Nag(subject=subject, tier=tier, prompt="…")

    def test_allows_an_ordinary_nag(self):
        gate = Gate(quiet_hours=(23, 8))
        allowed, _ = gate.allows(self._nag(), now=0, clock=at(14), present=True)
        assert allowed is True

    def test_quiet_hours_silence_everything_below_alarm(self):
        gate = Gate(quiet_hours=(23, 8))
        for tier in (Tier.GENTLE, Tier.FIRM, Tier.BLUNT):
            allowed, why = gate.allows(self._nag(tier), now=0, clock=at(3), present=True)
            assert allowed is False
            assert why == "quiet hours"

    def test_an_alarm_still_fires_in_quiet_hours(self):
        """Quiet hours exist so she doesn't chat at 3am — not so a missed
        commitment stays missed."""
        gate = Gate(quiet_hours=(23, 8))
        allowed, _ = gate.allows(self._nag(Tier.ALARM), now=0, clock=at(3), present=True)
        assert allowed is True

    def test_nothing_is_said_to_an_empty_room(self):
        gate = Gate()
        allowed, why = gate.allows(self._nag(), now=0, clock=at(14), present=False)
        assert allowed is False
        assert why == "nobody there"

    def test_unknown_presence_is_not_treated_as_absence(self):
        """No camera must not mean permanent silence."""
        gate = Gate()
        allowed, _ = gate.allows(self._nag(), now=0, clock=at(14), present=None)
        assert allowed is True

    def test_an_alarm_fires_even_if_they_seem_absent(self):
        gate = Gate()
        allowed, _ = gate.allows(self._nag(Tier.ALARM), now=0, clock=at(14), present=False)
        assert allowed is True

    def test_the_same_subject_is_not_repeated_immediately(self):
        gate = Gate(repeat_suppression=480)
        nag = self._nag()
        gate.record_spoken(nag.subject, now=1000)

        allowed, why = gate.allows(nag, now=1100, clock=at(14), present=True)
        assert allowed is False
        assert why == "said recently"

    def test_it_may_be_raised_again_after_the_suppression_window(self):
        gate = Gate(repeat_suppression=480)
        nag = self._nag()
        gate.record_spoken(nag.subject, now=1000)
        allowed, _ = gate.allows(nag, now=1500, clock=at(14), present=True)
        assert allowed is True

    def test_suppression_is_per_subject(self):
        gate = Gate(repeat_suppression=480)
        gate.record_spoken("scroll:instagram", now=1000)
        allowed, _ = gate.allows(
            self._nag(subject="calendar:standup"), now=1100, clock=at(14), present=True
        )
        assert allowed is True


class TestSnooze:
    def test_a_snooze_is_honoured(self):
        gate = Gate()
        granted = gate.snooze("scroll:instagram", now=1000)
        assert granted == 600

        allowed, why = gate.allows(
            Nag("scroll:instagram", Tier.GENTLE, "…"), now=1200, clock=at(14), present=True
        )
        assert allowed is False
        assert "snoozed" in why

    def test_it_comes_back_afterwards(self):
        gate = Gate()
        gate.snooze("scroll:instagram", now=1000)
        allowed, _ = gate.allows(
            Nag("scroll:instagram", Tier.GENTLE, "…"), now=1700, clock=at(14), present=True
        )
        assert allowed is True

    def test_repeated_snoozes_buy_less_time_each(self):
        """Asking five times in a row is itself information."""
        gate = Gate()
        granted = [gate.snooze("scroll:instagram", now=0) for _ in range(4)]
        assert granted == [600, 420, 240, 120]

    def test_the_shortest_snooze_is_the_floor(self):
        gate = Gate()
        for _ in range(10):
            granted = gate.snooze("s", now=0)
        assert granted == 120

    def test_delivering_it_resets_the_snooze_ladder(self):
        gate = Gate()
        gate.snooze("s", now=0)
        gate.snooze("s", now=0)
        gate.record_spoken("s", now=0)
        assert gate.snooze("s", now=10_000) == 600

    def test_clearing_a_subject_forgets_everything_about_it(self):
        gate = Gate()
        gate.record_spoken("scroll:instagram", now=1000)
        gate.clear("scroll:instagram")
        allowed, _ = gate.allows(
            Nag("scroll:instagram", Tier.GENTLE, "…"), now=1010, clock=at(14), present=True
        )
        assert allowed is True


class TestSilenceSentinel:
    @pytest.mark.parametrize("reply", ["SILENCE", "  SILENCE  ", "SILENCE.", '"SILENCE"', "silence"])
    def test_recognised_in_the_forms_a_model_actually_emits(self, reply):
        assert is_silence(reply) is True

    def test_an_empty_reply_is_silence(self):
        assert is_silence("   ") is True

    @pytest.mark.parametrize(
        "reply",
        ["You've been on Reddit for an hour.", "Silence is golden, but you're late."],
    )
    def test_real_speech_is_not_swallowed(self, reply):
        assert is_silence(reply) is False


class TestPromptBuilding:
    def test_the_tier_decides_the_style_not_the_model(self):
        nag = Nag("scroll:instagram", Tier.BLUNT, "They have been on Instagram for 45 minutes.")
        prompt = build_prompt(nag, "18:00  Instagram  (45m)", "writing the report")

        assert "45 minutes" in prompt
        assert "writing the report" in prompt
        assert "blunt" in prompt.lower()

    def test_a_missing_plan_is_simply_omitted(self):
        prompt = build_prompt(Nag("s", Tier.GENTLE, "x"), "digest", None)
        assert "What they said they were doing" not in prompt


def test_check_in_delay_stays_within_its_range_and_varies():
    delays = {check_in_delay((20, 40)) for _ in range(50)}
    assert all(20 * 60 <= d <= 40 * 60 for d in delays)
    # Jittered on purpose: a check-in exactly every 30 minutes becomes furniture.
    assert len(delays) > 1
