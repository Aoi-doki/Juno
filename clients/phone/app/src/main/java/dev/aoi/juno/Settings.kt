package dev.aoi.juno

import android.content.Context
import android.provider.Settings as AndroidSettings

/**
 * Where the brain is, and which apps are worth watching.
 *
 * Plain SharedPreferences. The token is not a credential worth protecting with
 * the keystore here — it only authorises talking to your own brain over your
 * own Tailscale network, and anyone with the device unlocked has your phone
 * anyway.
 */
class Settings(private val context: Context) {

    private val prefs = context.getSharedPreferences("juno", Context.MODE_PRIVATE)

    var brainUrl: String
        get() = prefs.getString(KEY_URL, "").orEmpty()
        set(value) = prefs.edit().putString(KEY_URL, value.trim()).apply()

    var token: String
        get() = prefs.getString(KEY_TOKEN, "").orEmpty()
        set(value) = prefs.edit().putString(KEY_TOKEN, value.trim()).apply()

    /**
     * A stable id for this phone.
     *
     * ANDROID_ID survives app updates and reboots but changes on factory reset,
     * which is the right lifetime — the brain should treat a wiped phone as a
     * new device rather than resuming its history.
     */
    val deviceId: String
        get() {
            prefs.getString(KEY_DEVICE_ID, null)?.let { return it }
            @Suppress("HardwareIds")
            val android = AndroidSettings.Secure.getString(
                context.contentResolver, AndroidSettings.Secure.ANDROID_ID,
            ) ?: "unknown"
            val id = "phone-${android.take(8)}"
            prefs.edit().putString(KEY_DEVICE_ID, id).apply()
            return id
        }

    /**
     * Packages that count as doomscrolling.
     *
     * The thresholds live in the brain's config, not here — tuning how long is
     * too long should not require rebuilding an APK.
     */
    var watchedApps: Set<String>
        get() = prefs.getStringSet(KEY_WATCHED, DEFAULT_WATCHED) ?: DEFAULT_WATCHED
        set(value) = prefs.edit().putStringSet(KEY_WATCHED, value).apply()

    val configured: Boolean get() = brainUrl.isNotBlank() && token.isNotBlank()

    private companion object {
        const val KEY_URL = "brain_url"
        const val KEY_TOKEN = "token"
        const val KEY_DEVICE_ID = "device_id"
        const val KEY_WATCHED = "watched_apps"

        val DEFAULT_WATCHED = setOf(
            "com.instagram.android",
            "com.zhiliaoapp.musically",   // TikTok
            "com.reddit.frontpage",
            "com.twitter.android",
            "com.x.android",
            "com.google.android.youtube",
            "com.facebook.katana",
            "com.snapchat.android",
        )
    }
}
