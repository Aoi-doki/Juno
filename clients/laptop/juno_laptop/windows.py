"""Finding out which window has focus, across compositors.

Wayland has no common way to ask "what is focused?" — each compositor exposes
its own IPC — so this probes for the one in use and parses its output. The
parsers are pure functions taking the raw command output, which is what makes
them testable without the compositor being present.

Deliberately *not* screenshot-and-OCR. A window title is one cheap IPC call,
already text, and answers "what app, what document" for almost everything.
Optional OCR sits on top of this for the cases where it doesn't (Phase 2's
`screen.py`), rather than underneath it as the primary source.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Sequence

log = logging.getLogger(__name__)

CommandRunner = Callable[[Sequence[str]], str]

# Titles matching these never leave the machine — not even as a timeline entry.
# Redaction happens at the point of capture rather than in the brain, so the
# sensitive string is never transmitted at all.
DEFAULT_REDACT = (
    r"(?i)\bprivate\b.*\bbrowsing\b",
    r"(?i)\bincognito\b",
    r"(?i)\bbitwarden\b|\b1password\b|\bkeepass\b|\bpass(word)? manager\b",
    r"(?i)\bseed phrase\b|\brecovery phrase\b",
)


@dataclass(frozen=True, slots=True)
class Window:
    app: str
    title: str

    def summary(self, max_title: int = 90) -> str:
        """One line for the timeline: ``Firefox — reddit.com``."""
        title = " ".join(self.title.split())
        if len(title) > max_title:
            title = title[: max_title - 1].rstrip() + "…"
        if not title:
            return self.app or "unknown"
        if not self.app:
            return title
        return f"{self.app} — {title}"


def run(command: Sequence[str]) -> str:
    return subprocess.run(
        command, capture_output=True, text=True, timeout=2, check=True
    ).stdout


# --- parsers (pure) ----------------------------------------------------------


def parse_niri(raw: str) -> Window | None:
    data = json.loads(raw)
    # niri wraps the window in a "Focused"/"None" tagged enum in some versions
    # and returns it bare in others; accept both rather than pinning a version.
    if isinstance(data, dict) and "Focused" in data:
        data = data["Focused"]
    if not isinstance(data, dict):
        return None
    return Window(app=str(data.get("app_id") or ""), title=str(data.get("title") or ""))


def parse_hyprland(raw: str) -> Window | None:
    data = json.loads(raw)
    if not isinstance(data, dict) or not data:
        return None
    # Hyprland returns {} when nothing is focused.
    if not data.get("class") and not data.get("title"):
        return None
    return Window(app=str(data.get("class") or ""), title=str(data.get("title") or ""))


def parse_sway(raw: str) -> Window | None:
    """Sway returns the whole tree; walk it for the focused node."""

    def walk(node: dict) -> dict | None:
        if node.get("focused"):
            return node
        for child in (*node.get("nodes", []), *node.get("floating_nodes", [])):
            found = walk(child)
            if found is not None:
                return found
        return None

    found = walk(json.loads(raw))
    if found is None:
        return None
    props = found.get("window_properties") or {}
    return Window(
        app=str(found.get("app_id") or props.get("class") or ""),
        title=str(found.get("name") or ""),
    )


# --- backends ----------------------------------------------------------------


@dataclass(slots=True)
class Backend:
    name: str
    binary: str
    command: tuple[str, ...]
    parse: Callable[[str], Window | None]


BACKENDS = (
    Backend("niri", "niri", ("niri", "msg", "--json", "focused-window"), parse_niri),
    Backend("hyprland", "hyprctl", ("hyprctl", "-j", "activewindow"), parse_hyprland),
    Backend("sway", "swaymsg", ("swaymsg", "-t", "get_tree"), parse_sway),
    Backend(
        "x11",
        "xdotool",
        ("xdotool", "getactivewindow", "getwindowname"),
        lambda raw: Window(app="", title=raw.strip()) if raw.strip() else None,
    ),
)


def detect_backend(available: Callable[[str], bool] = lambda b: shutil.which(b) is not None):
    """First backend whose binary is installed.

    Order matters: a niri session may still have xdotool present via XWayland,
    and the native IPC is both faster and more accurate.
    """
    for backend in BACKENDS:
        if available(backend.binary):
            return backend
    return None


class FocusWatcher:
    def __init__(
        self,
        redact_patterns: Sequence[str] = DEFAULT_REDACT,
        runner: CommandRunner = run,
        backend: Backend | None = None,
    ) -> None:
        self.runner = runner
        self.backend = backend or detect_backend()
        self._redact = [re.compile(p) for p in redact_patterns]
        if self.backend is None:
            log.warning(
                "no supported compositor found (tried %s) — screen awareness is off",
                ", ".join(b.name for b in BACKENDS),
            )
        else:
            log.info("screen awareness using %s", self.backend.name)

    @property
    def available(self) -> bool:
        return self.backend is not None

    def redacted(self, window: Window) -> bool:
        haystack = f"{window.app} {window.title}"
        return any(p.search(haystack) for p in self._redact)

    def focused(self) -> Window | None:
        """The focused window, or None if unavailable or redacted.

        Returning None for a redacted window rather than a placeholder is
        deliberate: a gap in the timeline reveals less than a run of
        "[redacted]" entries, which would advertise exactly when you were doing
        something private.
        """
        if self.backend is None:
            return None
        try:
            raw = self.runner(self.backend.command)
        except subprocess.CalledProcessError:
            return None  # nothing focused; normal on an empty workspace
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            log.debug("focus query failed: %s", exc)
            return None

        try:
            window = self.backend.parse(raw)
        except (json.JSONDecodeError, ValueError, KeyError, TypeError) as exc:
            log.debug("could not parse %s output: %s", self.backend.name, exc)
            return None

        if window is None or self.redacted(window):
            return None
        return window
