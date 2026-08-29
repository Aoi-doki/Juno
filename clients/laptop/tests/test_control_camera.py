"""Desktop control guards and camera presence hysteresis."""

from __future__ import annotations

import pytest

from juno_laptop.camera import Presence, PresenceTracker
from juno_laptop.control import MAX_TYPE_CHARS, Controller, is_forbidden, normalise_keys


class TestKeyGuards:
    def test_modifier_order_does_not_matter(self):
        """A denylist that can be evaded by reordering modifiers is not one."""
        assert normalise_keys("Alt+CTRL+Delete") == normalise_keys("ctrl+alt+delete")

    @pytest.mark.parametrize(
        "combo",
        ["ctrl+alt+delete", "Ctrl+Alt+Delete", "alt+ctrl+DELETE", "ctrl+alt+backspace"],
    )
    def test_dangerous_combinations_are_refused(self, combo):
        assert is_forbidden(combo) is True

    @pytest.mark.parametrize("combo", ["ctrl+c", "super", "alt+tab", "ctrl+shift+t"])
    def test_ordinary_combinations_are_allowed(self, combo):
        assert is_forbidden(combo) is False

    def test_empty_input_is_handled(self):
        assert normalise_keys("") == ""
        assert normalise_keys("+++") == ""


class TestInputSynthesisGate:
    async def test_typing_is_refused_when_disabled(self):
        """The gate is config, not prompting — asking a model to be careful is
        not a security control."""
        outcome = await Controller(allow_input_synthesis=False).type_text("rm -rf /")
        assert outcome.ok is False
        assert "disabled" in outcome.detail

    async def test_key_presses_are_refused_when_disabled(self):
        outcome = await Controller(allow_input_synthesis=False).press("ctrl+c")
        assert outcome.ok is False
        assert "disabled" in outcome.detail

    async def test_an_oversized_paste_is_refused_even_when_enabled(self):
        outcome = await Controller(allow_input_synthesis=True).type_text("x" * (MAX_TYPE_CHARS + 1))
        assert outcome.ok is False
        assert "refusing" in outcome.detail

    def test_can_type_requires_both_the_flag_and_a_tool(self):
        controller = Controller(allow_input_synthesis=True)
        controller._ydotool = controller._xdotool = None
        assert controller.can_type is False


class TestPresenceTracker:
    def test_starts_away_and_needs_a_face_to_become_present(self):
        """An empty chair at startup is not news, so nothing is reported until
        something actually changes."""
        tracker = PresenceTracker(away_after=45, present_after=3, posture_after=300)
        assert tracker.observe(False, False, 0) is None
        assert tracker.observe(True, False, 1) is None   # not long enough yet
        assert tracker.observe(True, False, 5) == Presence("at_desk", None)

    def test_a_single_missed_frame_does_not_mean_gone(self):
        """Face detectors drop frames when you turn your head or reach for a
        mug; without hysteresis she'd report you gone a dozen times an hour."""
        tracker = PresenceTracker(away_after=45, present_after=3, posture_after=300)
        tracker.observe(True, False, 0)
        tracker.observe(True, False, 5)
        assert tracker.observe(False, False, 10) is None
        assert tracker.observe(True, False, 12) is None  # still at_desk throughout

    def test_a_sustained_absence_reports_away(self):
        tracker = PresenceTracker(away_after=45, present_after=3, posture_after=300)
        tracker.observe(True, False, 0)
        tracker.observe(True, False, 5)
        assert tracker.observe(False, False, 20) is None   # grace period runs from here
        assert tracker.observe(False, False, 60) is None   # only 40s of it gone
        assert tracker.observe(False, False, 70) == Presence("away", None)

    def test_returning_reports_present_again(self):
        tracker = PresenceTracker(away_after=45, present_after=3, posture_after=300)
        tracker.observe(True, False, 0)
        tracker.observe(True, False, 5)
        tracker.observe(False, False, 20)
        tracker.observe(False, False, 70)                  # away
        tracker.observe(True, False, 100)
        assert tracker.observe(True, False, 105) == Presence("at_desk", None)

    def test_slouching_must_persist_before_it_is_mentioned(self):
        tracker = PresenceTracker(away_after=45, present_after=3, posture_after=300)
        tracker.observe(True, False, 0)
        tracker.observe(True, False, 5)
        assert tracker.observe(True, True, 10) is None      # everyone leans
        assert tracker.observe(True, True, 200) is None
        assert tracker.observe(True, True, 400) == Presence("at_desk", "slouching")

    def test_sitting_up_clears_the_posture(self):
        tracker = PresenceTracker(away_after=45, present_after=3, posture_after=300)
        tracker.observe(True, False, 0)
        tracker.observe(True, False, 5)
        tracker.observe(True, True, 10)
        tracker.observe(True, True, 400)
        assert tracker.observe(True, False, 410) == Presence("at_desk", None)

    def test_leaving_clears_the_posture_too(self):
        tracker = PresenceTracker(away_after=45, present_after=3, posture_after=300)
        tracker.observe(True, False, 0)
        tracker.observe(True, False, 5)
        tracker.observe(True, True, 10)
        tracker.observe(True, True, 400)                  # slouching
        assert tracker.observe(False, False, 500) is None  # inside the grace period
        assert tracker.observe(False, False, 560) == Presence("away", None)

    def test_looking_away_briefly_does_not_emit_a_posture_change(self):
        """Clearing posture on the first missed frame would emit 'at the desk,
        sitting up' immediately before 'away' — two events for one act of
        standing up."""
        tracker = PresenceTracker(away_after=45, present_after=3, posture_after=300)
        tracker.observe(True, False, 0)
        tracker.observe(True, False, 5)
        tracker.observe(True, True, 10)
        assert tracker.observe(True, True, 400) == Presence("at_desk", "slouching")
        assert tracker.observe(False, False, 410) is None
        assert tracker.observe(True, True, 420) is None

    def test_an_unchanged_state_reports_nothing(self):
        tracker = PresenceTracker(away_after=45, present_after=3, posture_after=300)
        tracker.observe(True, False, 0)
        assert tracker.observe(True, False, 5) == Presence("at_desk", None)
        for t in range(6, 40):
            assert tracker.observe(True, False, t) is None
