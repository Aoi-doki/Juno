"""Acting on the laptop: notifications, launching apps, clipboard, input.

Three tiers of risk, treated differently:

* **Harmless** — a notification, reading the clipboard, listing apps. Always on.
* **Visible** — launching an application. On by default; you will see it happen.
* **Dangerous** — synthesising keystrokes and clicks. Off unless you turn it on,
  because a model that can type into your session can do anything you can, and
  a misheard sentence should not be able to run a command.

The gate is config, not prompting. Asking the model to be careful is not a
security control; refusing to load the capability is.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from dataclasses import dataclass
from typing import Sequence

log = logging.getLogger(__name__)

# Typing this much in one go is not a command, it's a paste — and a paste into
# an unknown focused window is how you end up with a shell running something.
MAX_TYPE_CHARS = 500

# Never synthesised, regardless of the input-synthesis flag. These are the
# combinations that close, kill or escalate, where a misheard word is
# unrecoverable.
FORBIDDEN_KEYS = frozenset(
    {
        "ctrl+alt+delete",
        "ctrl+alt+backspace",  # kills the X server on some configs
        "alt+sysrq",
        "ctrl+alt+f1", "ctrl+alt+f2", "ctrl+alt+f3", "ctrl+alt+f4",
    }
)


@dataclass(slots=True)
class Outcome:
    ok: bool
    detail: str


def _run(command: Sequence[str], timeout: float = 5.0) -> Outcome:
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return Outcome(False, f"{command[0]} is not installed")
    except subprocess.TimeoutExpired:
        return Outcome(False, f"{command[0]} timed out")
    except OSError as exc:
        return Outcome(False, f"{command[0]} failed: {exc}")

    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip() or f"exit {done.returncode}"
        return Outcome(False, detail[:200])
    return Outcome(True, (done.stdout or "").strip()[:500])


def normalise_keys(combo: str) -> str:
    """Canonical form for a key combination, for comparison against the denylist.

    Sorted modifiers with a lowercased key, so ``Alt+CTRL+Delete`` and
    ``ctrl+alt+delete`` are recognised as the same thing — a denylist that can
    be evaded by reordering modifiers is not a denylist.
    """
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    if not parts:
        return ""
    modifiers = sorted(p for p in parts[:-1] if p)
    return "+".join([*modifiers, parts[-1]])


def is_forbidden(combo: str) -> bool:
    return normalise_keys(combo) in {normalise_keys(k) for k in FORBIDDEN_KEYS}


class Controller:
    """Desktop actions. Capabilities are decided once, at construction."""

    def __init__(self, allow_input_synthesis: bool = False) -> None:
        self.allow_input_synthesis = allow_input_synthesis
        self._notify = shutil.which("notify-send")
        self._ydotool = shutil.which("ydotool")
        self._xdotool = shutil.which("xdotool")
        self._wl_copy = shutil.which("wl-copy")
        self._wl_paste = shutil.which("wl-paste")
        self._xclip = shutil.which("xclip")

    @property
    def capabilities(self) -> list[str]:
        caps = ["control"] if self._notify else []
        return caps

    @property
    def can_type(self) -> bool:
        return self.allow_input_synthesis and bool(self._ydotool or self._xdotool)

    # --- harmless ------------------------------------------------------------

    async def notify(self, summary: str, body: str = "", urgent: bool = False) -> Outcome:
        if not self._notify:
            return Outcome(False, "notify-send is not installed")
        command = [self._notify, "--app-name=Juno"]
        if urgent:
            command += ["--urgency=critical"]
        command += [summary]
        if body:
            command.append(body)
        return await asyncio.to_thread(_run, command)

    async def read_clipboard(self) -> Outcome:
        if self._wl_paste:
            return await asyncio.to_thread(_run, [self._wl_paste, "--no-newline"])
        if self._xclip:
            return await asyncio.to_thread(_run, [self._xclip, "-o", "-selection", "clipboard"])
        return Outcome(False, "no clipboard tool (install wl-clipboard or xclip)")

    async def write_clipboard(self, text: str) -> Outcome:
        if self._wl_copy:
            command = [self._wl_copy]
        elif self._xclip:
            command = [self._xclip, "-selection", "clipboard"]
        else:
            return Outcome(False, "no clipboard tool (install wl-clipboard or xclip)")
        try:
            subprocess.run(command, input=text, text=True, timeout=5, check=True)
        except Exception as exc:  # noqa: BLE001
            return Outcome(False, f"clipboard write failed: {exc}")
        return Outcome(True, f"copied {len(text)} characters")

    # --- visible -------------------------------------------------------------

    async def launch(self, application: str) -> Outcome:
        """Start an application by desktop-entry name or executable.

        ``gtk-launch`` handles .desktop entries properly — right environment,
        right working directory — and falls back to running the binary.
        """
        launcher = shutil.which("gtk-launch")
        name = application.removesuffix(".desktop")
        if launcher:
            outcome = await asyncio.to_thread(_run, [launcher, name])
            if outcome.ok:
                return Outcome(True, f"launched {name}")

        binary = shutil.which(name)
        if not binary:
            return Outcome(False, f"could not find {application!r}")
        try:
            subprocess.Popen(
                [binary],
                start_new_session=True,   # survives Juno restarting
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            return Outcome(False, f"could not start {name}: {exc}")
        return Outcome(True, f"launched {name}")

    # --- dangerous -----------------------------------------------------------

    async def type_text(self, text: str) -> Outcome:
        if not self.allow_input_synthesis:
            return Outcome(False, "input synthesis is disabled in client.yaml")
        # Length is a policy limit, checked before tooling: whether this is a
        # command or a paste does not depend on which tool would deliver it.
        if len(text) > MAX_TYPE_CHARS:
            return Outcome(False, f"refusing to type {len(text)} characters at once")
        if not self.can_type:
            return Outcome(False, "neither ydotool nor xdotool is installed")

        if self._ydotool:
            return await asyncio.to_thread(_run, [self._ydotool, "type", "--", text], 30.0)
        return await asyncio.to_thread(_run, [self._xdotool, "type", "--", text], 30.0)

    async def press(self, combo: str) -> Outcome:
        if not self.allow_input_synthesis:
            return Outcome(False, "input synthesis is disabled in client.yaml")
        if not self.can_type:
            return Outcome(False, "neither ydotool nor xdotool is installed")
        if is_forbidden(combo):
            return Outcome(False, f"{combo} is never synthesised")

        if self._xdotool:
            return await asyncio.to_thread(_run, [self._xdotool, "key", combo])
        # ydotool wants key names, not an xdotool-style combo string; the
        # translation table is large and version-dependent, so rather than
        # guess wrong and press something unintended, say so plainly.
        return Outcome(False, "key combinations need xdotool; ydotool is not supported for this")
