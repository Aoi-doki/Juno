# Juno

An always-on personal assistant that watches what you're actually doing and
tells you the truth about it.

Named for *Juno Moneta* — "Juno who warns", the aspect of the goddess who
warned Rome of what was coming. Wake word: **"Hey Juno."**

## What she is meant to do

| | |
| --- | --- |
| **Talk, either way, any time** | Local wake word and speech recognition, local speech synthesis. She can start the conversation, not just answer. |
| **Watch the laptop** | Which window has focus, so "what have I been doing?" has a real answer. Titles, not screenshots. |
| **Watch you** | Webcam presence — at your desk, away, slouching — derived locally, never uploaded. |
| **Watch the phone** | Per-app foreground time and notifications, which is how she catches doomscrolling. |
| **Keep the schedule honest** | Calendar-driven, escalating to a phone alarm that rings through Do Not Disturb. |
| **Run the house** | Home Assistant as the device layer, once there are devices. |

## Why it's built this way

Two constraints drove every decision: it has to be **free to run**, and it has
to be **awake when the laptop isn't**.

So the brain lives on an always-on box, and every *continuous* workload —
listening, speaking, OCR, presence detection — runs locally on free software.
Cloud tokens are spent only on discrete moments of actual reasoning.

The rule that keeps it cheap: **clients send text, never pixels.** Thirty
screen samples of the same app collapse into one line — `Firefox — reddit.com
(30m)` — before anything reaches a model. A day of activity costs a fraction of
a cent to reason about.

Expected running cost: **$0 of infrastructure, $1–3/month of model tokens**,
with a hard budget cap in config that drops to a local model rather than
overspending.

## Layout

```
brain/              always-on service
  juno/
    protocol.py       the wire format, shared by every client
    orchestrator.py   WebSocket hub, device registry, presence routing
    agent.py          Claude tool-use loop, model tiering, budget ceiling
    memory.py         SQLite: conversation, timeline, facts, spend
    proactive.py      the rules for speaking — and mostly for not speaking
    scheduler.py      the tick loop that gives her initiative
    calendar.py       ICS subscription reading
    homeassistant.py  the smart home device layer
    tools/            capabilities exposed to the model
clients/laptop/     wake word, speech, screen focus, camera presence, control
clients/phone/      Android app: app usage, notifications, the ringing alarm
deploy/             systemd units and host setup
```

## Running the brain

```bash
cd brain
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp config.example.yaml config.yaml     # then edit it

export JUNO_AUTH_TOKEN=$(openssl rand -hex 32)
export ANTHROPIC_API_KEY=sk-ant-...
.venv/bin/python -m juno
```

`GET /health` lists connected devices and 30-day spend. Clients connect to
`/ws` and must present the auth token in their first frame.

```bash
cd brain && .venv/bin/python -m pytest
```

## Security

The brain has your microphone, your screen, and your camera. Two locks, not
one:

- **Tailscale.** The listening port is never exposed publicly; the host
  firewall stays closed and devices reach it over the mesh.
- **A shared token.** Every client presents it in the first frame or the socket
  is closed before it can send or receive anything. This is the lock that still
  holds if the Tailscale ACL is ever wrong.

Camera frames and screenshots stay on the machine that captured them. Only
derived text — `at_desk`, `Firefox — reddit.com` — is sent, unless you
explicitly ask her to look at something.

## Status

All six phases are written and tested. **Nothing has run on real hardware yet** —
no microphone, no webcam, no phone. That is the next step and it is the only
thing standing between this and a working assistant.

| Phase | State |
| --- | --- |
| 0 — brain | Runs; devices connect and authenticate; memory survives restarts |
| 1 — voice | Wake word, Whisper, Kokoro, barge-in. Audio path unverified |
| 2 — screen + control | Focus tracking on niri/Hyprland/sway/X11; notify, launch, gated input |
| 3 — phone | Kotlin app, built by CI. Never installed on a device |
| 4 — proactivity | Escalation ladder, calendar, quiet hours, snooze |
| 5 — camera | MediaPipe presence, frames never leave the machine |
| 6 — smart home | Home Assistant, with guards on locks and covers |

Tests cover the logic that can be tested without hardware: the timing and
debounce rules, the suppression gates, the parsers, the session arithmetic.
The audio, camera and Android runtime paths need your devices.

**Start here:** `clients/laptop/README.md` — fetch the models, run
`juno-audition`, and pick her voice.
