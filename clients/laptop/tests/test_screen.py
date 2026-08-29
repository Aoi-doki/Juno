"""Compositor parsing and the debounce rules.

The parsers take raw command output, so real fixtures from each compositor can
be tested without any of them installed.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from juno_laptop.screen import FocusReporter
from juno_laptop.windows import (
    BACKENDS,
    FocusWatcher,
    Window,
    detect_backend,
    parse_hyprland,
    parse_niri,
    parse_sway,
)


class TestParsers:
    def test_niri_bare_object(self):
        raw = json.dumps({"id": 3, "title": "reddit.com — Firefox", "app_id": "firefox"})
        assert parse_niri(raw) == Window(app="firefox", title="reddit.com — Firefox")

    def test_niri_tagged_enum_form(self):
        """Some niri versions wrap the window in a Focused/None enum."""
        raw = json.dumps({"Focused": {"title": "nvim", "app_id": "ghostty"}})
        assert parse_niri(raw) == Window(app="ghostty", title="nvim")

    def test_niri_nothing_focused(self):
        assert parse_niri(json.dumps(None)) is None

    def test_hyprland(self):
        raw = json.dumps({"class": "firefox", "title": "reddit.com"})
        assert parse_hyprland(raw) == Window(app="firefox", title="reddit.com")

    def test_hyprland_returns_empty_object_when_nothing_focused(self):
        assert parse_hyprland("{}") is None

    def test_sway_walks_the_tree_for_the_focused_node(self):
        tree = {
            "focused": False,
            "nodes": [
                {"focused": False, "nodes": []},
                {
                    "focused": False,
                    "nodes": [{"focused": True, "app_id": "ghostty", "name": "nvim", "nodes": []}],
                },
            ],
        }
        assert parse_sway(json.dumps(tree)) == Window(app="ghostty", title="nvim")

    def test_sway_checks_floating_windows_too(self):
        tree = {
            "focused": False,
            "nodes": [],
            "floating_nodes": [{"focused": True, "app_id": "pavucontrol", "name": "Volume"}],
        }
        assert parse_sway(json.dumps(tree)) == Window(app="pavucontrol", title="Volume")

    def test_sway_with_nothing_focused(self):
        assert parse_sway(json.dumps({"focused": False, "nodes": []})) is None


class TestSummary:
    def test_app_and_title_are_joined(self):
        assert Window("firefox", "reddit.com").summary() == "firefox — reddit.com"

    def test_long_titles_are_truncated(self):
        summary = Window("firefox", "x" * 200).summary(max_title=20)
        assert summary.endswith("…")
        assert len(summary) < 40

    def test_missing_app_or_title_degrades_gracefully(self):
        assert Window("", "just a title").summary() == "just a title"
        assert Window("firefox", "").summary() == "firefox"
        assert Window("", "").summary() == "unknown"

    def test_whitespace_is_collapsed(self):
        assert Window("a", "b   \n  c").summary() == "a — b c"


class TestRedaction:
    @pytest.mark.parametrize(
        "title",
        [
            "Reddit — Mozilla Firefox (Private Browsing)",
            "New Incognito Tab - Chromium",
            "Bitwarden",
            "Recovery phrase — KeePassXC",
        ],
    )
    def test_sensitive_windows_are_never_reported(self, title):
        watcher = FocusWatcher(runner=lambda _cmd: "", backend=BACKENDS[0])
        assert watcher.redacted(Window("app", title)) is True

    def test_ordinary_windows_are_not_redacted(self):
        watcher = FocusWatcher(runner=lambda _cmd: "", backend=BACKENDS[0])
        assert watcher.redacted(Window("firefox", "reddit.com")) is False

    def test_a_redacted_window_reports_as_nothing_at_all(self):
        """A gap reveals less than a run of '[redacted]', which would advertise
        exactly when something private was on screen."""
        raw = json.dumps({"app_id": "firefox", "title": "Private Browsing"})
        watcher = FocusWatcher(runner=lambda _cmd: raw, backend=BACKENDS[0])
        assert watcher.focused() is None


class TestFocusWatcherFailureModes:
    def _watcher(self, runner):
        return FocusWatcher(runner=runner, backend=BACKENDS[0])

    def test_unparseable_output_is_survivable(self):
        assert self._watcher(lambda _cmd: "not json at all").focused() is None

    def test_a_nonzero_exit_means_nothing_is_focused(self):
        def runner(_cmd):
            raise subprocess.CalledProcessError(1, "niri")

        assert self._watcher(runner).focused() is None

    def test_a_hung_compositor_does_not_hang_the_client(self):
        def runner(_cmd):
            raise subprocess.TimeoutExpired("niri", 2)

        assert self._watcher(runner).focused() is None

    def test_no_backend_means_unavailable_rather_than_crashing(self):
        watcher = FocusWatcher(runner=lambda _cmd: "", backend=None)
        watcher.backend = None
        assert watcher.available is False
        assert watcher.focused() is None


class TestBackendDetection:
    def test_native_ipc_wins_over_xdotool(self):
        """XWayland means xdotool is often present in a niri session too."""
        backend = detect_backend(available=lambda b: b in ("niri", "xdotool"))
        assert backend.name == "niri"

    def test_falls_back_to_x11(self):
        assert detect_backend(available=lambda b: b == "xdotool").name == "x11"

    def test_none_when_nothing_is_installed(self):
        assert detect_backend(available=lambda _b: False) is None


class TestFocusReporter:
    def test_brief_focus_is_not_reported(self):
        """Alt-tabbing through windows must not fill the timeline."""
        reporter = FocusReporter(min_dwell=8, heartbeat=120)
        window = Window("firefox", "reddit.com")
        assert reporter.observe(window, 0) is None      # first sighting
        assert reporter.observe(window, 3) is None      # still too brief
        assert reporter.observe(window, 5) is None

    def test_reports_once_the_window_has_been_held(self):
        reporter = FocusReporter(min_dwell=8, heartbeat=120)
        window = Window("firefox", "reddit.com")
        reporter.observe(window, 0)
        assert reporter.observe(window, 9) == window

    def test_does_not_report_the_same_window_repeatedly(self):
        reporter = FocusReporter(min_dwell=8, heartbeat=120)
        window = Window("firefox", "reddit.com")
        reporter.observe(window, 0)
        assert reporter.observe(window, 9) == window
        assert reporter.observe(window, 20) is None
        assert reporter.observe(window, 100) is None

    def test_heartbeat_re_reports_a_long_session(self):
        """So the brain can tell 'still working' from 'the client died'."""
        reporter = FocusReporter(min_dwell=8, heartbeat=120)
        window = Window("firefox", "reddit.com")
        reporter.observe(window, 0)
        reporter.observe(window, 9)
        assert reporter.observe(window, 130) == window

    def test_switching_windows_starts_a_new_dwell(self):
        reporter = FocusReporter(min_dwell=8, heartbeat=120)
        first, second = Window("firefox", "reddit"), Window("ghostty", "nvim")
        reporter.observe(first, 0)
        assert reporter.observe(first, 9) == first
        assert reporter.observe(second, 10) is None
        assert reporter.observe(second, 14) is None
        assert reporter.observe(second, 19) == second

    def test_none_ends_the_run_without_reporting(self):
        reporter = FocusReporter(min_dwell=8, heartbeat=120)
        window = Window("firefox", "reddit.com")
        reporter.observe(window, 0)
        assert reporter.observe(None, 5) is None
        assert reporter.observe(None, 20) is None

    def test_returning_to_a_window_after_a_gap_reports_again(self):
        """Otherwise a lunch break inside one app reads as one unbroken
        session, and Juno claims an hour where there were twenty minutes."""
        reporter = FocusReporter(min_dwell=8, heartbeat=120)
        window = Window("firefox", "reddit.com")
        reporter.observe(window, 0)
        assert reporter.observe(window, 9) == window

        reporter.observe(None, 10)                    # screen locked
        reporter.observe(window, 1800)                # back half an hour later
        assert reporter.observe(window, 1809) == window
