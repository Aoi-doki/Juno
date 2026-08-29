package dev.aoi.juno

import android.app.Activity
import android.app.KeyguardManager
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.media.AudioAttributes
import android.media.AudioManager
import android.media.RingtoneManager
import android.os.Build
import android.os.Bundle
import android.speech.tts.TextToSpeech
import android.view.Gravity
import android.view.WindowManager
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import java.util.Locale

/**
 * The loud one: takes over the lock screen, rings through Do Not Disturb, and
 * says what is wrong out loud.
 *
 * This is Juno's last rung, and it is rationed deliberately — the brain only
 * sends `urgency: alarm` for a genuine problem. Spend it on ordinary
 * distraction and it becomes noise, and then the app gets uninstalled.
 *
 * Reached via a full-screen intent rather than by starting an activity
 * directly, because background activity starts are blocked on modern Android.
 * The notification is the vehicle; this is what it opens.
 */
class AlarmActivity : Activity() {

    private var tts: TextToSpeech? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        showOverLockScreen()

        val message = intent.getStringExtra(EXTRA_MESSAGE).orEmpty()
        setContentView(buildView(message))

        // Speak on the alarm stream so it is audible even with media muted.
        tts = TextToSpeech(this) { status ->
            if (status != TextToSpeech.SUCCESS) return@TextToSpeech
            tts?.language = Locale.getDefault()
            tts?.setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ALARM)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build(),
            )
            tts?.speak(message, TextToSpeech.QUEUE_FLUSH, null, "juno-alarm")
        }
    }

    private fun showOverLockScreen() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O_MR1) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
            (getSystemService(Context.KEYGUARD_SERVICE) as KeyguardManager)
                .requestDismissKeyguard(this, null)
        } else {
            @Suppress("DEPRECATION")
            window.addFlags(
                WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED or
                    WindowManager.LayoutParams.FLAG_TURN_SCREEN_ON or
                    WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON,
            )
        }
    }

    private fun buildView(message: String) = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        gravity = Gravity.CENTER
        setBackgroundColor(Color.parseColor("#1e1e2e"))
        setPadding(64, 64, 64, 64)

        addView(
            TextView(this@AlarmActivity).apply {
                text = "Juno"
                textSize = 20f
                setTextColor(Color.parseColor("#89b4fa"))
                gravity = Gravity.CENTER
            },
        )
        addView(
            TextView(this@AlarmActivity).apply {
                text = message
                textSize = 28f
                setTextColor(Color.parseColor("#cdd6f4"))
                gravity = Gravity.CENTER
                setPadding(0, 48, 0, 64)
            },
        )
        addView(
            Button(this@AlarmActivity).apply {
                text = "All right"
                setOnClickListener { finish() }
            },
        )
    }

    override fun onDestroy() {
        tts?.stop()
        tts?.shutdown()
        super.onDestroy()
    }

    companion object {
        private const val EXTRA_MESSAGE = "message"
        private const val CHANNEL_ID = "juno-alarm"
        private const val NOTIFICATION_ID = 2

        /**
         * Fire the alarm.
         *
         * Posts a max-priority notification carrying a full-screen intent. When
         * the screen is off or locked, Android launches the activity directly;
         * when it is on, the user gets a heads-up they can act on. Both paths
         * matter, which is why this is not just `startActivity`.
         */
        fun launch(context: Context, message: String) {
            val manager =
                context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                manager.createNotificationChannel(
                    NotificationChannel(
                        CHANNEL_ID,
                        "Juno alarms",
                        NotificationManager.IMPORTANCE_HIGH,
                    ).apply {
                        description = "Only fires for genuine problems"
                        setBypassDnd(true)
                        setSound(
                            RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM),
                            AudioAttributes.Builder()
                                .setUsage(AudioAttributes.USAGE_ALARM)
                                .build(),
                        )
                    },
                )
            }

            val full = PendingIntent.getActivity(
                context,
                0,
                Intent(context, AlarmActivity::class.java)
                    .putExtra(EXTRA_MESSAGE, message)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TASK),
                PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT,
            )

            manager.notify(
                NOTIFICATION_ID,
                Notification.Builder(context, CHANNEL_ID)
                    .setContentTitle("Juno")
                    .setContentText(message)
                    .setSmallIcon(android.R.drawable.ic_dialog_alert)
                    .setCategory(Notification.CATEGORY_ALARM)
                    .setFullScreenIntent(full, true)
                    .setAutoCancel(true)
                    .build(),
            )
        }
    }
}
