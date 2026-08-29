package dev.aoi.juno

import android.app.AppOpsManager
import android.app.NotificationManager
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.PowerManager
import android.provider.Settings

/**
 * The permission checklist.
 *
 * Every grant Juno needs on Android is a different kind of thing — a runtime
 * prompt, a Settings toggle, an OEM battery screen — and **all of them fail
 * silently**. Nothing tells you the notification listener was revoked by a
 * One UI update; the events simply stop.
 *
 * So the app states them plainly, checks each on every launch, and sends the
 * user straight to the right screen. This is the difference between "it stopped
 * working and I don't know why" and a checklist with one red row.
 */
enum class Grant(
    val title: String,
    val why: String,
) {
    NOTIFICATIONS(
        "Show notifications",
        "So Juno can reach you at all, and keep a persistent notification while running.",
    ),
    USAGE_ACCESS(
        "Usage access",
        "How she knows which app you're in and for how long. This is the doomscroll signal.",
    ),
    NOTIFICATION_LISTENER(
        "Notification access",
        "Forwards your notifications so she knows what's competing for your attention.",
    ),
    BATTERY_UNRESTRICTED(
        "Unrestricted battery",
        "Samsung will otherwise put Juno to sleep and she'll quietly stop reporting.",
    ),
    FULL_SCREEN_INTENT(
        "Full-screen alerts",
        "The only escalation loud enough to matter. Android revokes this by default " +
            "for anything that isn't a calling or alarm app.",
    ),
    DND_OVERRIDE(
        "Do Not Disturb exception",
        "So a genuine problem can still reach you when everything else is silenced.",
    ),
    ;

    /** The Settings screen that grants this, or null if it's a runtime prompt. */
    fun settingsIntent(context: Context): Intent? = when (this) {
        NOTIFICATIONS -> Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS)
            .putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName)

        USAGE_ACCESS -> Intent(Settings.ACTION_USAGE_ACCESS_SETTINGS)

        NOTIFICATION_LISTENER -> Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)

        BATTERY_UNRESTRICTED ->
            Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS)
                .setData(Uri.parse("package:${context.packageName}"))

        FULL_SCREEN_INTENT ->
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
                Intent(Settings.ACTION_MANAGE_APP_USE_FULL_SCREEN_INTENT)
                    .setData(Uri.parse("package:${context.packageName}"))
            } else {
                null // granted at install below Android 14
            }

        DND_OVERRIDE -> Intent(Settings.ACTION_NOTIFICATION_POLICY_ACCESS_SETTINGS)
    }
}

class PermissionChecker(private val context: Context) {

    fun granted(grant: Grant): Boolean = when (grant) {
        Grant.NOTIFICATIONS -> notifications().areNotificationsEnabled()

        Grant.USAGE_ACCESS -> hasUsageAccess()

        Grant.NOTIFICATION_LISTENER -> {
            val enabled = Settings.Secure.getString(
                context.contentResolver, "enabled_notification_listeners",
            ).orEmpty()
            enabled.contains(context.packageName)
        }

        Grant.BATTERY_UNRESTRICTED -> {
            val power = context.getSystemService(Context.POWER_SERVICE) as PowerManager
            power.isIgnoringBatteryOptimizations(context.packageName)
        }

        Grant.FULL_SCREEN_INTENT ->
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
                notifications().canUseFullScreenIntent()
            } else {
                true
            }

        Grant.DND_OVERRIDE -> notifications().isNotificationPolicyAccessGranted
    }

    fun missing(): List<Grant> = Grant.entries.filterNot { granted(it) }

    private fun notifications() =
        context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager

    private fun hasUsageAccess(): Boolean {
        // AppOps reports the toggle directly. Querying for events instead and
        // checking whether any came back looks equivalent but is not: an idle
        // phone legitimately returns nothing, which would show as a revoked
        // permission and send the user chasing a setting that is already on.
        val appOps = context.getSystemService(Context.APP_OPS_SERVICE) as AppOpsManager
        val mode = appOps.unsafeCheckOpNoThrow(
            AppOpsManager.OPSTR_GET_USAGE_STATS,
            android.os.Process.myUid(),
            context.packageName,
        )
        return if (mode == AppOpsManager.MODE_DEFAULT) {
            // MODE_DEFAULT means "decided by permission", so fall through to it.
            context.checkSelfPermission(android.Manifest.permission.PACKAGE_USAGE_STATS) ==
                android.content.pm.PackageManager.PERMISSION_GRANTED
        } else {
            mode == AppOpsManager.MODE_ALLOWED
        }
    }
}
