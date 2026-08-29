package dev.aoi.juno

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.speech.tts.TextToSpeech
import android.util.Log
import org.json.JSONObject
import java.util.Locale
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

/**
 * The always-running part.
 *
 * Declared `specialUse`, not `dataSync`, and that is not a preference: apps
 * targeting Android 15+ **cannot** start a `dataSync` foreground service from
 * `BOOT_COMPLETED`. It throws `ForegroundServiceStartNotAllowedException`, and
 * only after a reboot — exactly when nobody is reading logcat. `specialUse` is
 * permitted from boot.
 *
 * The same rule rules out `microphone` type, so there is no always-listening
 * here. The phone speaks and reports; the wake word lives on the laptop.
 */
class JunoService : Service() {

    private lateinit var settings: Settings
    private lateinit var usage: UsageReader
    private var link: Link? = null
    private var tts: TextToSpeech? = null
    private val poller = Executors.newSingleThreadScheduledExecutor()

    override fun onCreate() {
        super.onCreate()
        settings = Settings(this)
        usage = UsageReader(this)
        startForeground(NOTIFICATION_ID, buildNotification())
        tts = TextToSpeech(this) { status ->
            if (status == TextToSpeech.SUCCESS) tts?.language = Locale.getDefault()
        }
        connect()
        poller.scheduleWithFixedDelay(::poll, 10, POLL_SECONDS, TimeUnit.SECONDS)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // START_STICKY so Android restarts us after killing the process for
        // memory. On Samsung this only helps once the app is on the
        // "never sleeping" list; see the README.
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onDestroy() {
        poller.shutdownNow()
        NotificationBus.sink = null
        link?.close()
        tts?.shutdown()
        super.onDestroy()
    }

    private fun connect() {
        val url = settings.brainUrl
        val token = settings.token
        if (url.isBlank() || token.isBlank()) {
            Log.w(TAG, "brain URL or token not set; not connecting")
            return
        }
        link = Link(url, token, settings.deviceId, ::onFrame).also { it.connect() }

        // The notification listener runs as its own service and cannot hold the
        // socket, so it hands off through the bus to whichever link is live.
        NotificationBus.sink = { app, title, pkg ->
            link?.send(
                JSONObject().apply {
                    put("type", "event")
                    put("kind", "phone.notification")
                    put("summary", "$app: $title")
                    put("package", pkg)
                },
            )
        }
    }

    /** One poll: work out whether a watched app is being scrolled, and report. */
    private fun poll() {
        try {
            val watched = settings.watchedApps
            if (watched.isEmpty()) return
            val now = System.currentTimeMillis()
            val events = usage.recentEvents(sinceMillis = LOOKBACK_MS, now = now)
            val session = sessionFrom(events, watched, now) ?: return

            link?.send(
                JSONObject().apply {
                    put("type", "event")
                    put("kind", "usage.session")
                    put("app", session.packageName)
                    put("minutes", session.minutes)
                    put(
                        "summary",
                        JSONObject().apply {
                            put("app", labelFor(session.packageName))
                            put("minutes", session.minutes)
                        }.toString(),
                    )
                },
            )
        } catch (e: Exception) {
            Log.w(TAG, "poll failed", e)
        }
    }

    private fun onFrame(frame: JSONObject) {
        when (frame.optString("type")) {
            "welcome" -> Log.i(TAG, "registered with the brain")

            "speak" -> {
                val text = frame.optString("text").trim()
                if (text.isEmpty()) return
                if (frame.optString("urgency") == "alarm") {
                    AlarmActivity.launch(this, text)
                } else {
                    tts?.speak(text, TextToSpeech.QUEUE_FLUSH, null, "juno")
                }
            }

            "command" -> {
                // Nothing here is remotely controllable by design. Reply
                // honestly so the brain learns that rather than waiting.
                link?.send(
                    JSONObject().apply {
                        put("type", "result")
                        put("id", frame.optString("id"))
                        put("ok", false)
                        put("detail", "the phone reports and speaks; it is not controlled")
                    },
                )
            }
        }
    }

    private fun labelFor(packageName: String): String = try {
        val info = packageManager.getApplicationInfo(packageName, 0)
        packageManager.getApplicationLabel(info).toString()
    } catch (_: Exception) {
        packageName
    }

    private fun buildNotification(): Notification {
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            manager.createNotificationChannel(
                NotificationChannel(
                    CHANNEL_ID,
                    "Juno",
                    // LOW: it must be visible (a foreground service requires it)
                    // but it should never make a sound.
                    NotificationManager.IMPORTANCE_LOW,
                ).apply { description = "Juno is watching your attention" },
            )
        }
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE,
        )
        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("Juno")
            .setContentText("Watching your attention")
            .setSmallIcon(android.R.drawable.ic_menu_view)
            .setContentIntent(open)
            .setOngoing(true)
            .build()
    }

    companion object {
        private const val TAG = "JunoService"
        private const val CHANNEL_ID = "juno-service"
        private const val NOTIFICATION_ID = 1
        private const val POLL_SECONDS = 30L

        /**
         * How far back to read events when rebuilding the current session.
         * Must comfortably exceed the longest session worth reporting, since
         * the session is recomputed from scratch each poll rather than
         * accumulated — a restart mid-scroll must not reset the clock.
         */
        private const val LOOKBACK_MS = 4 * 60 * 60 * 1000L

        fun start(context: Context) {
            val intent = Intent(context, JunoService::class.java)
            context.startForegroundService(intent)
        }
    }
}
