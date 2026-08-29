package dev.aoi.juno

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Session reconstruction — the one piece of real logic in the phone client,
 * and the thing the whole escalation ladder is computed from.
 */
class UsageTest {

    private val instagram = "com.instagram.android"
    private val camera = "com.sec.android.app.camera"
    private val watched = setOf(instagram)

    private fun minutes(n: Long) = n * 60_000L

    @Test
    fun `no events means no session`() {
        assertNull(sessionFrom(emptyList(), watched, now = minutes(10)))
    }

    @Test
    fun `an unwatched app in the foreground is not a session`() {
        val events = listOf(ForegroundEvent(camera, minutes(1), resumed = true))
        assertNull(sessionFrom(events, watched, now = minutes(2)))
    }

    @Test
    fun `having left the app means no session, even though it was watched`() {
        val events = listOf(
            ForegroundEvent(instagram, minutes(1), resumed = true),
            ForegroundEvent(instagram, minutes(30), resumed = false),
        )
        assertNull(sessionFrom(events, watched, now = minutes(31)))
    }

    @Test
    fun `a simple session runs from the resume to now`() {
        val events = listOf(ForegroundEvent(instagram, minutes(10), resumed = true))
        val session = sessionFrom(events, watched, now = minutes(52))!!

        assertEquals(instagram, session.packageName)
        assertEquals(42.0, session.minutes, 0.01)
    }

    @Test
    fun `a quick trip to another app does not reset the timer`() {
        // Replying to a story with the camera is part of the same scrolling
        // stretch. Splitting here is how a doomscroll detector never fires.
        val events = listOf(
            ForegroundEvent(instagram, minutes(0), resumed = true),
            ForegroundEvent(instagram, minutes(20), resumed = false),
            ForegroundEvent(camera, minutes(20), resumed = true),
            ForegroundEvent(camera, minutes(21), resumed = false),
            ForegroundEvent(instagram, minutes(21), resumed = true),
        )
        val session = sessionFrom(events, watched, now = minutes(40))!!
        assertEquals(40.0, session.minutes, 0.01)
    }

    @Test
    fun `a long gap does start a new session`() {
        val events = listOf(
            ForegroundEvent(instagram, minutes(0), resumed = true),
            ForegroundEvent(instagram, minutes(20), resumed = false),
            // ... two hours of doing something else ...
            ForegroundEvent(instagram, minutes(140), resumed = true),
        )
        val session = sessionFrom(events, watched, now = minutes(145))!!
        assertEquals(5.0, session.minutes, 0.01)
    }

    @Test
    fun `events arriving out of order are still handled`() {
        val events = listOf(
            ForegroundEvent(instagram, minutes(30), resumed = true),
            ForegroundEvent(instagram, minutes(10), resumed = true),
        )
        val session = sessionFrom(events, watched, now = minutes(40))!!
        assertEquals(30.0, session.minutes, 0.01)
    }

    @Test
    fun `the session survives being recomputed from scratch`() {
        // Samsung kills the service constantly. The session is rebuilt from
        // events every poll precisely so a restart mid-scroll does not reset
        // the clock and hand the user a fresh 45 minutes.
        val events = listOf(ForegroundEvent(instagram, minutes(0), resumed = true))

        val first = sessionFrom(events, watched, now = minutes(20))!!
        val afterRestart = sessionFrom(events, watched, now = minutes(46))!!

        assertEquals(20.0, first.minutes, 0.01)
        assertEquals(46.0, afterRestart.minutes, 0.01)
        assertTrue(afterRestart.minutes > first.minutes)
    }

    @Test
    fun `gap tolerance is configurable`() {
        val events = listOf(
            ForegroundEvent(instagram, minutes(0), resumed = true),
            ForegroundEvent(camera, minutes(1), resumed = true),
            ForegroundEvent(instagram, minutes(5), resumed = true),
        )
        // With a tight tolerance the four-minute excursion splits the session.
        val tight = sessionFrom(events, watched, now = minutes(10), gapTolerance = 60_000L)!!
        assertEquals(5.0, tight.minutes, 0.01)

        // With a generous one it does not.
        val loose = sessionFrom(events, watched, now = minutes(10), gapTolerance = 600_000L)!!
        assertEquals(10.0, loose.minutes, 0.01)
    }
}
