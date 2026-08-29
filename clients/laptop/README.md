# Juno — laptop client

Wake word, speech recognition and speech synthesis. All of it runs on this
machine: no audio ever leaves it, and nothing is metered.

## Setup

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
./scripts/fetch-models.sh            # Kokoro, ~340 MB, once
cp client.example.yaml client.yaml   # then edit brain_url
export JUNO_AUTH_TOKEN=...           # must match the brain's
```

On Arch you also need PortAudio for the microphone:

```bash
sudo pacman -S portaudio
```

## Pick her voice first

You will hear this voice thousands of times. Reading preset names tells you
nothing, so listen:

```bash
.venv/bin/juno-audition                # every voice, two lines each
.venv/bin/juno-audition --save /tmp/v  # write WAVs instead of playing
```

It reads one calm line and one nudge on purpose — a voice that sounds pleasant
reading your calendar can sound insufferable telling you to put your phone
down, and the second is what you'll hear most. Put your pick in `client.yaml`
as `kokoro_voice`.

## Run

```bash
.venv/bin/juno-laptop
```

Say **"Hey Juno"**, wait for the log line, talk. Interrupt her any time — she
stops mid-word.

## The wake word

openWakeWord ships pretrained models for a handful of phrases, and "hey juno"
is not among them, so the config starts on `hey_jarvis` as a placeholder. To get
the real one, train a custom model with openWakeWord's synthetic-data pipeline
— free, about an hour, mostly unattended — then drop the `.onnx` into `models/`
and set `wake_word: hey_juno`.

If she triggers on ordinary conversation, raise `wake_threshold` toward 0.8. If
she misses you, lower it toward 0.4.

## Why these libraries

There is no PyTorch here, deliberately. Kokoro and openWakeWord run on
onnxruntime, faster-whisper on CTranslate2, and endpointing uses `webrtcvad`.
The whole stack installs in seconds and starts instantly rather than pulling
~2 GB and taking several seconds to import — which matters for something meant
to run from boot on a laptop.

One audio input stream serves all three jobs — wake-word detection when idle,
utterance capture after a trigger, barge-in detection while she talks. Opening
three streams is the usual way these setups break on Linux.

## Running it from boot

```bash
mkdir -p ~/.config/systemd/user
cp ../../deploy/juno-laptop.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now juno-laptop
journalctl --user -u juno-laptop -f
```

## Screen awareness

She knows which window has focus, so "what have I been doing?" has a real
answer. Supported compositors, in preference order:

| | How |
| --- | --- |
| niri | `niri msg --json focused-window` |
| Hyprland | `hyprctl -j activewindow` |
| sway | `swaymsg -t get_tree` |
| X11 | `xdotool` (title only) |

Native IPC is preferred over `xdotool` even when both are present, since
XWayland means `xdotool` is usually installed in a Wayland session too.

If none is found, the client says so once and simply doesn't claim the `screen`
capability — the brain then knows not to expect events, rather than waiting for
ones that never arrive.

**Titles, not screenshots.** A window title is one cheap IPC call and already
text. Nothing is captured, and no image is produced or sent.

**Redaction happens here, not in the brain.** Private-browsing windows,
password managers and anything matching the patterns in `windows.py` are never
reported at all — the string doesn't leave the machine. They report as *nothing*
rather than as `[redacted]`, because a run of redaction markers would advertise
exactly when you were doing something private.

**Reporting is debounced.** Polls every 5 s, reports only on change once a
window has held focus for 8 s, plus a heartbeat every 2 minutes. Alt-tabbing
through windows produces no events, and an hour in one app produces a handful
rather than 720.

## What is not here yet

Desktop control (launching apps, typing) and camera presence are Phase 2's
remainder and Phase 5. The client answers `command` frames for those with an
honest "not implemented" rather than silently ignoring them, so the brain knows
they went nowhere.
