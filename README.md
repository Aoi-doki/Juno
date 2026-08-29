# Juno

An always-on personal assistant that watches what you're actually doing and
tells you the truth about it.

Named for *Juno Moneta* — "Juno who warns", the aspect of the goddess who
warned Rome of what was coming. Wake word: **"Hey Juno."**

> **→ Start with [SETUP.md](SETUP.md).** Start to finish in about two hours,
> most of it waiting on downloads. You can stop a third of the way in and
> already have an assistant you can talk to.
>
> **→ [TODO.md](TODO.md) is what's still outstanding** — what only you can do,
> what has never met real hardware, and the known gaps.

## What she does

| | |
| --- | --- |
| **Talks, either way, any time** | Local wake word, speech recognition and synthesis. She can start the conversation, not just answer. |
| **Watches the laptop** | Which window has focus, so "what have I been doing?" has a real answer. Titles, not screenshots. |
| **Watches you** | Webcam presence — at your desk, away, slouching — derived locally, never uploaded. |
| **Watches the phone** | Per-app foreground time and notifications. This is how she catches doomscrolling. |
| **Keeps the schedule honest** | Calendar-driven, escalating to a phone alarm that rings through Do Not Disturb. |
| **Runs the house** | Home Assistant as the device layer, once there are devices. |

## Why it's built this way

Two constraints drove every decision: it has to be **free to run**, and it has
to be **awake when the laptop isn't** — proactive nagging that dies when you
close the lid is worthless.

So the brain lives on an always-on box, and every *continuous* workload runs
locally on free software: wake word, speech recognition, speech synthesis and
presence detection never touch a network. Free VPS, free Tailscale, Gemini's
free tier, Ollama. **Nothing here has a bill.**

**Clients send text, never pixels.** Thirty screen samples of the same app
collapse into one line — `Firefox — reddit.com  (30m)` — before anything
reaches a model. Camera frames become the word `away`. That keeps context small
enough to be free, and it means the sensitive material mostly doesn't exist to
leak.

The one real decision is **where each kind of thinking happens**. Check-ins
carry your activity timeline, so they default to the **local** model and never
leave the box; only ordinary conversation goes out to a hosted one. That trade —
privacy against judgement — is measurable rather than a matter of taste:

```bash
python -m juno.evals.checkin --engine gemini --engine local
```

Sixteen realistic scenarios, scored on how often each model correctly says
nothing. Watch the false-alarm column; that's the one that decides whether you
keep her installed.

## Layout

```
brain/                  the always-on service
  juno/
    protocol.py           the wire format, shared by every client
    orchestrator.py       WebSocket hub, device registry, presence routing
    agent.py              the tool-use loop, routed per role
    engines.py            Gemini / Ollama / Claude behind one interface
    memory.py             SQLite: conversation, timeline, facts
    proactive.py          the rules for speaking — and mostly for not speaking
    scheduler.py          the tick loop that gives her initiative
    calendar.py           ICS subscription reading
    homeassistant.py      the smart home device layer
    tools/                capabilities exposed to the model
    evals/                does this model know when to shut up?
clients/laptop/         wake word, speech, screen focus, camera, control
clients/phone/          Android: app usage, notifications, the ringing alarm
deploy/                 systemd units and host setup
```

## Running the brain

[SETUP.md](SETUP.md) has the real instructions. The short version, on the
machine in front of you:

```bash
cd brain
python -m venv .venv && .venv/bin/pip install -e ".[dev,calendar]"
cp config.example.yaml config.yaml

export JUNO_AUTH_TOKEN=$(openssl rand -hex 32)
export GEMINI_API_KEY=...          # free, no card
.venv/bin/python -m juno
```

`GET /health` lists the devices currently connected. Clients connect to `/ws`
and must present the auth token in their first frame.

Tests, each from the repository root:

```bash
(cd brain          && .venv/bin/python -m pytest)
(cd clients/laptop && .venv/bin/python -m pytest)
(cd clients/phone  && gradle test)
```

## Security

The brain has your microphone, your screen and your camera. Two locks, not one:

- **Tailscale.** The listening port is never exposed publicly; the host
  firewall stays closed and devices reach it over the mesh.
- **A shared token.** Every client presents it in its first frame or the socket
  closes before it can send or receive anything. This is the lock that still
  holds when the Tailscale ACL is wrong.

Camera frames and screenshots never leave the machine that captured them — only
derived text does. Private-browsing and password-manager windows are dropped at
capture, and report as *nothing* rather than `[redacted]`, since a run of
redaction markers would advertise exactly when you were doing something private.

## Status

Everything is written, and **nothing has run on real hardware** — no
microphone, no webcam, no phone. That gap is larger than the test count
suggests, and it's the only thing between this and a working assistant.

| | State |
| --- | --- |
| Brain | Runs; devices connect and authenticate; memory survives restarts |
| Engines | Gemini, Ollama and Claude behind one interface, chosen per role |
| Voice | Wake word, Whisper, Kokoro, barge-in. **Audio path unverified** |
| Screen + control | Focus tracking on niri/Hyprland/sway/X11; notify, launch, gated input |
| Phone | Kotlin app, built by CI. **Never installed on a device** |
| Proactivity | Escalation ladder, calendar, quiet hours, snooze |
| Camera | MediaPipe presence, frames never leave the machine |
| Smart home | Home Assistant, with guards on locks and covers. **No devices to test against** |

Tests cover what can be checked without hardware: the timing and debounce
rules, the suppression gates, the compositor parsers, the session arithmetic,
the engine adapters. The audio, camera and Android runtime paths need your
devices — see [TODO.md](TODO.md) for exactly which.
