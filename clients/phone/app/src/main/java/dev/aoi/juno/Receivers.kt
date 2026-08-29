package dev.aoi.juno

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import android.util.Log

/**
 * Starts the service after a reboot.
 *
 * This works only because [JunoService] is declared `specialUse`. A `dataSync`
 * service started from here throws on Android 15+, and it throws at boot, where
 * nobody sees it — the app would simply appear to stop working after a restart.
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
        if (!Settings(context).configured) return
        Log.i("JunoBoot", "starting service after boot")
        JunoService.start(context)
    }
}

/**
 * Forwards notifications to the brain.
 *
 * This is most of "see my phone" in practice: what is competing for attention,
 * and what the user is ignoring. Only the app and the title travel — not the
 * message body, which is where the actually private content lives.
 */
class NotificationRelay : NotificationListenerService() {

    private val settings by lazy { Settings(this) }

    override fun onNotificationPosted(sbn: StatusBarNotification) {
        // Juno's own notifications would otherwise loop straight back in.
        if (sbn.packageName == packageName) return
        // Ongoing notifications are status displays — media players, navigation,
        // other foreground services — not things demanding attention.
        if (sbn.isOngoing) return

        val title = sbn.notification.extras.getCharSequence("android.title")?.toString().orEmpty()
        if (title.isBlank()) return

        val label = try {
            val info = packageManager.getApplicationInfo(sbn.packageName, 0)
            packageManager.getApplicationLabel(info).toString()
        } catch (_: Exception) {
            sbn.packageName
        }

        NotificationBus.send(
            app = label,
            title = title,
            packageName = sbn.packageName,
        )
    }
}

/**
 * A notification listener is its own process-level service and cannot hold the
 * socket, so relayed notifications hand off through here to whatever [Link] the
 * running [JunoService] owns. Dropped silently when the service is not up,
 * which is correct — a notification from an hour ago is not worth delivering.
 */
object NotificationBus {
    @Volatile
    var sink: ((String, String, String) -> Unit)? = null

    fun send(app: String, title: String, packageName: String) {
        sink?.invoke(app, title, packageName)
    }
}
