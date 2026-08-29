package dev.aoi.juno

import android.app.usage.UsageEvents
import android.app.usage.UsageStatsManager
import android.content.Context

/**
 * Doomscroll detection.
 *
 * `UsageStatsManager` is the only unrooted way to know which app is in the
 * foreground and for how long. It needs the `PACKAGE_USAGE_STATS` special
 * access grant, which is a Settings toggle rather than a runtime prompt.
 *
 * The interesting work is turning a stream of resume/pause events into "you
 * have been on Instagram for 43 minutes", which is what the brain's escalation
 * ladder runs on. That logic is pure and lives in [sessionFrom] so it can be
 * tested without a device.
 */

/** One unbroken stretch in one app. */
data class Session(
    val packageName: String,
    val startedAt: Long,
    val lastSeenAt: Long,
) {
    val minutes: Double get() = (lastSeenAt - startedAt) / 60_000.0
}

/**
 * A gap shorter than this does not end a session.
 *
 * Switching to the camera to reply to a story, or glancing at a notification,
 * is part of the same scrolling stretch. Without this tolerance every quick
 * app-switch would reset the timer and the ladder would never climb — which is
 * precisely how a doomscroll detector ends up never firing.
 */
const val SESSION_GAP_TOLERANCE_MS = 90_000L

/**
 * Rebuild the current session from raw foreground events, newest last.
 *
 * Returns null when the tracked app is not currently in the foreground.
 * Deliberately recomputed from events each poll rather than accumulated in a
 * field: the service can be killed and restarted by Samsung at any moment, and
 * a session that resets to zero on every restart would never reach a threshold.
 */
fun sessionFrom(
    events: List<ForegroundEvent>,
    watched: Set<String>,
    now: Long,
    gapTolerance: Long = SESSION_GAP_TOLERANCE_MS,
): Session? {
    val ordered = events.sortedBy { it.timestamp }
    val last = ordered.lastOrNull() ?: return null
    if (!last.resumed || last.packageName !in watched) return null
    val app = last.packageName

    // Reconstruct the stretches during which the app was actually in the
    // foreground. Measuring gaps between *events* instead would be wrong: an
    // app left open for twenty minutes emits nothing in between, so a single
    // unbroken stretch would look like a long gap and split.
    val intervals = mutableListOf<Pair<Long, Long>>()
    var openedAt: Long? = null
    for (event in ordered) {
        if (event.packageName == app) {
            if (event.resumed) {
                if (openedAt == null) openedAt = event.timestamp
            } else {
                openedAt?.let { intervals += it to event.timestamp }
                openedAt = null
            }
        } else if (event.resumed) {
            // Another app came forward, so ours left even if no explicit pause
            // event was recorded for it.
            openedAt?.let { intervals += it to event.timestamp }
            openedAt = null
        }
    }

    val currentStart = openedAt ?: return null  // not in the foreground now
    intervals += currentStart to now

    // Walk backwards merging stretches separated by less than the tolerance,
    // so brief excursions elsewhere do not reset the clock.
    var runStart = currentStart
    for (index in intervals.size - 2 downTo 0) {
        val (start, end) = intervals[index]
        if (runStart - end > gapTolerance) break
        runStart = start
    }
    return Session(app, runStart, now)
}

/** A foreground transition, flattened out of [UsageEvents]. */
data class ForegroundEvent(
    val packageName: String,
    val timestamp: Long,
    val resumed: Boolean,
)

class UsageReader(private val context: Context) {

    private val manager: UsageStatsManager? =
        context.getSystemService(Context.USAGE_STATS_SERVICE) as? UsageStatsManager

    /** Foreground transitions in the recent past, oldest first. */
    fun recentEvents(sinceMillis: Long, now: Long = System.currentTimeMillis()): List<ForegroundEvent> {
        val manager = manager ?: return emptyList()
        val out = mutableListOf<ForegroundEvent>()
        val events = manager.queryEvents(now - sinceMillis, now)
        val event = UsageEvents.Event()
        while (events.hasNextEvent()) {
            events.getNextEvent(event)
            val resumed = when (event.eventType) {
                UsageEvents.Event.ACTIVITY_RESUMED -> true
                UsageEvents.Event.ACTIVITY_PAUSED -> false
                else -> continue
            }
            out += ForegroundEvent(event.packageName, event.timeStamp, resumed)
        }
        return out
    }
}
