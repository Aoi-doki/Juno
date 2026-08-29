# Setting up Juno

Start to finish, in order. Roughly **two hours**, most of it waiting on
downloads.

You can stop after step 4 and have a working assistant you can talk to. Steps 5
onward add the phone, the calendar and the smart home.

## What you need first

| | |
| --- | --- |
| An always-on machine | Oracle Cloud's Always Free tier is what this is designed around — free forever, no card charge |
| An Anthropic API key | From [console.anthropic.com](https://console.anthropic.com). Budget $1–3/month |
| Your laptop | Linux. Wayland (niri, Hyprland, sway) or X11 |
| An Android phone | Optional, and only for the doomscroll nagging |
| A Tailscale account | Free for personal use |

---

## 1. The always-on box

The brain has to be awake when your laptop isn't — proactive nagging that dies
when you close the lid is useless.

Create an **Always Free** ARM instance (4 OCPU / 24 GB, Ubuntu 24.04) in the
Oracle Cloud console. Capacity for the free ARM shape is genuinely scarce; if
the console says none is available, try the other availability domains in your
region, and try again later. This is the most annoying step and it is the only
one that can just refuse.

Leave the security list closed. Nothing here is ever exposed to the internet.

## 2. Tailscale

Install on the box, your laptop and your phone:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

In the Tailscale admin console, rename the box to **`juno-brain`**. That
hostname is what every client connects to.

Worth tightening the ACL so only your own devices can reach the port:

```json
{
  "acls": [
    { "action": "accept", "src": ["autogroup:member"], "dst": ["juno-brain:8765"] }
  ]
}
```

## 3. The brain

On the box:

```bash
sudo useradd -r -m -d /opt/juno juno
sudo apt install -y python3-venv git
sudo -u juno git clone https://github.com/Aoi-doki/Juno /opt/juno
cd /opt/juno/brain
sudo -u juno python3 -m venv .venv
sudo -u juno .venv/bin/pip install -e ".[calendar]"
sudo -u juno cp config.example.yaml config.yaml
```

**Generate the shared token now — you'll need it on every device.**

```bash
sudo mkdir -p /etc/juno
printf 'ANTHROPIC_API_KEY=sk-ant-...\nJUNO_AUTH_TOKEN=%s\n' "$(openssl rand -hex 32)" \
  | sudo tee /etc/juno/env > /dev/null
sudo chmod 600 /etc/juno/env
sudo cat /etc/juno/env        # copy the token somewhere for step 4
```

Edit `config.yaml` — at minimum set `user_name` and `timezone`. Then start it:

```bash
sudo cp /opt/juno/deploy/juno-brain.service /etc/systemd/system/
sudo systemctl enable --now juno-brain
curl localhost:8765/health
```

You should get `{"ok": true, "devices": [], ...}`.

**Stop Oracle reclaiming the instance.** It takes back Always Free compute it
considers idle — roughly a week under 10% CPU. Give it something to do:

```bash
sudo -u juno crontab -e
```

```cron
*/5 * * * * curl -s localhost:8765/health > /dev/null
0 * * * * timeout 60 dd if=/dev/zero of=/dev/null
```

## 4. The laptop — where you actually talk to her

```bash
git clone https://github.com/Aoi-doki/Juno ~/juno
cd ~/juno/clients/laptop
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
./scripts/fetch-models.sh          # Kokoro, ~340 MB, once
```

On Arch you need PortAudio for the microphone, and libnotify for
notifications:

```bash
sudo pacman -S portaudio libnotify
```

### Pick her voice before anything else

You will hear this voice thousands of times, and preset names tell you nothing:

```bash
.venv/bin/juno-audition
```

It reads one calm line and one nudge in every voice — both on purpose, because
a voice that's pleasant reading your calendar can be insufferable telling you
to put your phone down, and the second is what you'll hear most.

### Configure and run

```bash
cp client.example.yaml client.yaml
```

Set `brain_url: ws://juno-brain:8765/ws` and `kokoro_voice` to whichever you
picked. Then the token from step 3:

```bash
mkdir -p ~/.config/juno
echo "JUNO_AUTH_TOKEN=<the token>" > ~/.config/juno/env
chmod 600 ~/.config/juno/env

set -a; source ~/.config/juno/env; set +a
.venv/bin/juno-laptop
```

Say **"Hey Juno"**, wait for the log line, and talk. Interrupt her whenever —
she stops mid-word.

> The wake word ships as `hey_jarvis`, because openWakeWord has no pretrained
> "hey juno". Training the real one is free and takes about an hour, mostly
> unattended — see `clients/laptop/README.md`.

### Start it at boot

```bash
mkdir -p ~/.config/systemd/user
cp ~/juno/deploy/juno-laptop.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now juno-laptop
sudo loginctl enable-linger $USER     # survives logout
```

### Optional extras

```bash
# Camera presence — so she doesn't talk to an empty chair
.venv/bin/pip install -e ".[camera]"
# then set enable_camera: true in client.yaml

# Letting her type and click. Off by default on purpose: anything that can
# type into your session can do anything you can.
sudo pacman -S ydotool          # Wayland  (xdotool on X11)
# then set allow_input_synthesis: true in client.yaml
```

**At this point Juno works.** Everything below is optional.

---

## 5. The phone (Android)

Only needed for doomscroll nagging. The app reports what you're doing and talks
back — it is not remote-controlled.

Grab `juno-phone-debug` from the latest [CI run's
artifacts](https://github.com/Aoi-doki/Juno/actions), or build it:

```bash
cd clients/phone && gradle assembleDebug
```

**On Samsung, turn off Auto Blocker first** or the install is refused outright:
Settings → Security and privacy → Auto Blocker → off.

Install it, open it, and enter the same brain URL and token. The first screen
is a permission checklist — work down it until every row is green. Each row has
a button that opens the right Settings page.

Then the two Samsung settings the checklist can't set for you:

> Settings → Battery → Background usage limits
> → turn **off** "Put unused apps to sleep"
> → add Juno to **Never sleeping apps**

> Settings → Apps → Juno → Battery → **Unrestricted**

*Never sleeping apps* is the one that actually matters. Without it One UI
deep-sleeps the service after a few days and Juno quietly goes blind.

Full detail, including why the service is declared `specialUse`, is in
[`clients/phone/README.md`](clients/phone/README.md).

## 6. Calendar

Any calendar with a private subscription URL — Google, Radicale, Nextcloud,
Fastmail. In Google Calendar it's Settings → your calendar → *Secret address in
iCal format*.

Add it to `/etc/juno/env` on the box and restart:

```
JUNO_CALENDAR_URL=https://calendar.google.com/calendar/ical/.../basic.ics
```

Treat that URL as a password — anyone with it can read your calendar.

## 7. Smart home

Run Home Assistant **on your LAN**, not the box — most device protocols
(Matter, Zigbee, mDNS discovery) are local-network-only. Reach it over
Tailscale.

Create a long-lived access token from your HA profile page, then in
`/etc/juno/env`:

```
JUNO_HA_TOKEN=...
```

and in `config.yaml` set `home_assistant.url`. Anything you'd rather she never
touch goes in `forbidden` — locks and garage doors are the obvious ones.

---

## Checking it all works

```bash
curl http://juno-brain:8765/health
```

Every connected device and your 30-day spend, in one place.

Then, out loud:

1. "Hey Juno, I'm working on the report for the next two hours." → she records it
2. Open something unrelated for a few minutes → she should notice
3. "Hey Juno, what have I been doing?" → a real answer from the timeline

## When something doesn't work

| Symptom | Usually |
| --- | --- |
| Device missing from `/health` | Token mismatch, or Tailscale isn't up yet. The client logs both plainly — check its journal first |
| She never hears the wake word | Lower `wake_threshold` toward 0.4. Too eager? Raise it toward 0.8 |
| `juno-audition` fails immediately | PortAudio isn't installed, or the models weren't fetched |
| She hears you but never replies | `ANTHROPIC_API_KEY` isn't set on the box — the brain logs a warning at startup and still records events |
| She stops talking entirely | Check quiet hours in `config.yaml`, and whether the camera thinks you're away |
| Phone reports for a day, then stops | Samsung deep-sleep. Re-check *Never sleeping apps* |
| She goes quiet after a month | The budget cap tripped and dropped her to the local model. `/health` shows the spend |

## What it costs

Infrastructure is free — the VPS, Tailscale, and every model that runs
continuously (Kokoro, Whisper, openWakeWord, MediaPipe). The only bill is
Claude tokens, **about $1–3/month**, and `models.monthly_budget_usd` is a hard
ceiling that falls back to a local model rather than overspending.
