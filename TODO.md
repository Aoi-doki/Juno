# Everything left to do

Juno is written and CI is green, but **nothing has run on real hardware** — no
microphone, no webcam, no phone. This is the honest list of what stands between
here and a working assistant.

Setup instructions live in [SETUP.md](SETUP.md). This is the *checklist*.

---

## 1. Things only you can do

Accounts, hardware and decisions I can't make from here.

- [ ] **Get an Oracle Always Free ARM instance.** The one step that can simply
      refuse — free ARM capacity is scarce. Retry across availability domains,
      and across days.
- [ ] **Anthropic API key**, with billing set up. Budget $1–3/month.
- [ ] **Tailscale account**, on all three devices, box renamed `juno-brain`.
- [ ] **Pick her voice** — `juno-audition`. Nobody else can make this call and
      it's the thing you'll hear most.
- [ ] **Install PortAudio and libnotify** on the laptop, or the microphone and
      notifications silently do nothing.
- [ ] **Calendar ICS URL**, if you want her to know your schedule.
- [ ] **Buy one smart device.** The Home Assistant layer is written and has
      never spoken to real hardware, because there is none yet.

## 2. First contact with hardware

None of this can be verified without your devices. Expect problems here — this
is where they'll be.

### Laptop
- [ ] `juno-audition` actually produces sound (shakes out PortAudio first)
- [ ] Wake word triggers on "Hey Juno" — then **tune `wake_threshold`**. Too
      many false triggers → raise toward 0.8; missing you → lower toward 0.4.
      The shipped 0.6 is a guess.
- [ ] Barge-in cuts her off mid-word, and doesn't trigger on her own voice
      coming back through the speakers. **The likeliest thing to be wrong**, and
      it depends on your speaker volume and mic placement.
- [ ] Whisper transcribes you accurately. If not, `small.en` is better and
      about 3× slower.
- [ ] Focus tracking works on niri — the parser handles two output shapes
      because niri changed it between versions, and I could not test either.
- [ ] Camera presence: does the slouch threshold (`centre > 0.62`) mean
      anything for your camera's placement? It's a guess that needs one sitting.

### Phone
- [ ] APK installs (Auto Blocker off first)
- [ ] Every row of the permission checklist goes green
- [ ] **It survives three days.** This is the real test of the Samsung battery
      settings, and the only one that matters.
- [ ] Doomscroll detection fires at roughly the right time
- [ ] The alarm takes over a locked, face-down screen and speaks

### End to end
- [ ] Tell her a plan, drift from it, confirm she notices
- [ ] Walk away; confirm she stops talking to an empty chair
- [ ] Ignore a calendar event; confirm the ladder escalates

## 3. Known gaps in the code

Written down rather than quietly left out.

- [ ] **The wake word is still "hey jarvis".** openWakeWord has no pretrained
      "hey juno". Training one is free and takes about an hour, mostly
      unattended, via its synthetic-data pipeline.
- [ ] **Her voice differs between devices.** Laptop uses Kokoro, phone uses
      Android's system TTS, because the brain sends text rather than audio.
      Fixing it means running Kokoro on the brain and streaming PCM down the
      socket.
- [ ] **`scheduler.py` has no tests.** The rules it calls are thoroughly
      tested — quiet hours, tiers, snooze, suppression — but the tick loop that
      wires them together isn't, and it's the thing that decides when she
      speaks. This is the most valuable test to write next.
- [ ] **`calendar.py` has no tests.** The date normalisation matters: all-day
      events arrive as `date` rather than `datetime`, and comparing those raises
      — which would take down a scheduler tick.
- [ ] **`agent.py` has no tests.** Needs the Anthropic client mocked; the tool
      loop and the budget ceiling are both worth pinning.
- [ ] **Scroll-session parsing is brittle.** The scheduler reads the phone's
      session out of an event summary with a string split. It should read the
      structured fields the phone already sends.
- [ ] **No phone location.** Small to add, but nothing currently uses it, so it
      would be a permission prompt bought for nothing.

## 4. Deliberately not built

Not oversights — decisions, listed so they can be revisited.

| | Why not, and what it would take |
| --- | --- |
| **Screen OCR** | Window titles answer "what app, what document" for almost everything. Browser tabs are the gap — you get the page title, not the domain. Would need `grim` + `tesseract`, or a browser extension. |
| **Live phone screen** (`scrcpy`) | Works unrooted over Tailscale, about half an hour's work. Dropped when the phone's job narrowed to nagging. |
| **Real phone calls** | Nothing can place a WhatsApp or Signal call programmatically. Twilio could ring you for ~$1.15/month plus usage; the lock-screen alarm does the same job for free. |
| **Force-closing apps** | Needs root or an accessibility service. She can talk, ring, and make ignoring her annoying — the intervention is social, not technical. |

## 5. Once it's running

Tuning that only makes sense with real data behind it.

- [ ] **Set the proactivity dial honestly.** It ships at 7. Give it a week
      before deciding — the instinct after one good nag is to raise it, and
      after one bad one to switch it off.
- [ ] **Tune `scroll_apps` thresholds** in `config.yaml` to your actual apps.
- [ ] **Check `/health` for spend** after a week and set
      `monthly_budget_usd` somewhere you're comfortable.
- [ ] **Decide about `allow_input_synthesis`.** Off by default deliberately.
      Turn it on only once you trust what she does with everything else.
