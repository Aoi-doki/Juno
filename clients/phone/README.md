# Juno — phone client

Target device: **Samsung Galaxy S25 Ultra**, unrooted, One UI 7/8 on Android 15/16.

> **Status:** written, unit-tested, and built by CI — but never run on a
> physical device. Grab the APK from the latest CI run's artifacts (or
> `gradle assembleDebug`) and see the setup checklist below.

Scope is deliberately narrow. The phone is **not** remote-controlled. It reports
what you're doing and talks back at you — which is all that's needed to catch
doomscrolling, and it collapses the whole phase into one small Kotlin app with
a foreground service and a WebSocket to the brain.

## What it does

| Job | Mechanism |
| --- | --- |
| Notice doomscrolling | `UsageStatsManager`, polled every 30 s |
| Forward notifications | `NotificationListenerService` — app and title only, never the body |
| Talk | Android's built-in TTS |
| Ring at you | Full-screen intent that overrides Do Not Disturb, speaking on the alarm stream |
| Survive a reboot | `BOOT_COMPLETED` → `specialUse` foreground service |

### Two known gaps

**Her voice is different here.** The laptop uses Kokoro; the phone uses
Android's system TTS, because the brain has no synthesiser of its own — it
sends text, and each client speaks it however it can. Matching them would mean
running Kokoro on the brain and streaming PCM down the socket. Worth doing; not
done.

**No location reporting.** It would be a small addition (`FusedLocationProvider`
plus a runtime permission), but nothing in the current proactivity rules uses
it, so it would be a permission prompt bought for nothing.

## Samsung-specific obstacles

Samsung is the most aggressive mainstream OEM about killing background work.
None of this is optional on an S25 Ultra — skip it and the service dies within
hours and Juno silently goes blind.

### 1. Sideloading is blocked by default

One UI ships **Auto Blocker** on, which prevents installing apps from outside
the Play Store entirely. It has to be turned off before the APK will install:

> Settings → Security and privacy → Auto Blocker → off

(Or leave it on and install over wireless ADB, which Auto Blocker doesn't
cover. Auto Blocker also blocks USB commands while locked, so unlock first.)

### 2. Samsung will put the app to sleep

Two separate settings, both required:

> Settings → Battery → Background usage limits
> → turn **off** "Put unused apps to sleep"
> → add Juno to **Never sleeping apps**

> Settings → Apps → Juno → Battery → **Unrestricted**

Adding it to *Never sleeping apps* is the one that actually matters — Samsung's
"Deep sleeping apps" list will otherwise catch it after a few days of the
screen being off.

### 3. The service cannot start from boot as `dataSync`

Apps targeting Android 15+ **cannot** launch a `dataSync`, `mediaPlayback`,
`camera` or `phoneCall` foreground service from a `BOOT_COMPLETED` receiver.
Doing so throws `ForegroundServiceStartNotAllowedException` at runtime — and
only after a reboot, which is exactly when nobody is watching the logs.

So the service must be declared **`specialUse`**, which is permitted from
`BOOT_COMPLETED`:

```xml
<service
    android:name=".JunoService"
    android:foregroundServiceType="specialUse"
    android:exported="false">
    <property
        android:name="android.app.PROPERTY_SPECIAL_USE_FGS_SUBTYPE"
        android:value="Continuous attention monitoring for a personal assistant" />
</service>
```

This is a design constraint, not a preference: it rules out declaring the
service as `microphone` type, which in turn means **continuous phone-side
listening is off the table**. The phone speaks and reports; the laptop is where
the wake word lives. That matches the plan anyway.

### 4. The alarm needs a permission that is revoked by default

Since Android 14, `USE_FULL_SCREEN_INTENT` is granted at install only to apps
that provide calling or alarms; the Play Store revokes it for everything else.
A sideloaded app shouldn't rely on either outcome. Check and route the user:

```kotlin
if (!notificationManager.canUseFullScreenIntent()) {
    startActivity(Intent(Settings.ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT,
        Uri.parse("package:$packageName")))
}
```

Also needed for the alarm to pierce Do Not Disturb:

> Settings → Notifications → Advanced settings → Do not disturb → App
> exceptions → Juno

### 5. Usage access is a separate grant

`PACKAGE_USAGE_STATS` is a special access permission, not a runtime one — it
cannot be requested with a dialog:

```kotlin
startActivity(Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS))
```

Same for the notification listener
(`Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS`).

## Setup checklist

Because these are all one-time toggles in different corners of Settings, the
app should verify each on launch and show what's missing rather than failing
silently:

- [ ] Auto Blocker off (or install over wireless ADB)
- [ ] Battery → Unrestricted
- [ ] Never sleeping apps → Juno added
- [ ] Usage access granted
- [ ] Notification access granted
- [ ] Full-screen intent allowed
- [ ] Do Not Disturb exception granted
- [ ] Notification permission granted (Android 13+ runtime prompt)

The brain pings the phone agent every few minutes and reports when it stops
answering, so a setting that gets reverted by a One UI update surfaces as
"your phone stopped reporting" rather than Juno quietly going blind.

## The intervention ladder

Escalates only if you keep going, and never raises the volume. Thresholds and
the app list live in the brain's `config.yaml` under `proactivity.scroll_apps`,
so tuning them doesn't need a rebuild.

| Tier | Roughly | Behaviour |
| --- | --- | --- |
| 1 | 15 min | Spoken nudge at normal volume |
| 2 | 30 min | Firm — names what you said you'd be doing, offers an alternative |
| 3 | 45 min | Blunt, repeating on a timer until you engage |
| 4 | — | Full-screen takeover. **Genuine limits only** — very late at night, or a commitment you're about to miss |

Tier 4 is rationed on purpose. Fire it for ordinary scrolling and it becomes
noise, and then it gets uninstalled.

"Ten more minutes" is accepted, logged and honoured. Repeated snoozes shorten
the next fuse rather than being silently ignored.

## What this cannot do

Without root or an accessibility service it **cannot force-close Instagram**.
It can talk, ring, and make ignoring it annoying. The intervention is social,
not technical — which is generally the one that works anyway.

## References

- [Changes to foreground service types for Android 15](https://developer.android.com/about/versions/15/changes/foreground-service-types)
- [Foreground service types](https://developer.android.com/develop/background-work/services/fgs/service-types)
- [Behavior changes: apps targeting Android 14+](https://developer.android.com/about/versions/14/behavior-changes-14)
- [Full-screen intent limits](https://source.android.com/docs/core/permissions/fsi-limits)
- [dontkillmyapp.com — Samsung](https://dontkillmyapp.com/samsung)
