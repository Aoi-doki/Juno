package dev.aoi.juno

import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit
import kotlin.math.min

/**
 * The connection to the brain.
 *
 * A phone loses its network constantly — dead spots, aeroplane mode, Wi-Fi to
 * mobile handover, Samsung freezing the process — so reconnection is the normal
 * case rather than the error case. Backoff doubles and caps, and every attempt
 * re-registers, because the brain forgets a device the moment its socket drops.
 */
class Link(
    private val url: String,
    private val token: String,
    private val deviceId: String,
    private val onFrame: (JSONObject) -> Unit,
) {
    private val client = OkHttpClient.Builder()
        // The brain pings every 45s; this is the other direction, and it is
        // what detects a socket that a dozing radio silently dropped.
        .pingInterval(30, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()

    @Volatile private var socket: WebSocket? = null
    @Volatile private var closed = false
    private var attempt = 0

    val connected: Boolean get() = socket != null

    fun connect() {
        if (closed) return
        val request = Request.Builder().url(url).build()
        client.newWebSocket(request, object : WebSocketListener() {

            override fun onOpen(webSocket: WebSocket, response: Response) {
                Log.i(TAG, "connected to brain")
                socket = webSocket
                attempt = 0
                webSocket.send(
                    JSONObject().apply {
                        put("type", "hello")
                        put("token", token)
                        put("device_id", deviceId)
                        put("kind", "phone")
                        put(
                            "capabilities",
                            JSONArray(listOf("speak", "alarm", "notify", "usage")),
                        )
                    }.toString(),
                )
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                val frame = try {
                    JSONObject(text)
                } catch (e: Exception) {
                    Log.w(TAG, "unparseable frame", e); return
                }
                if (frame.optString("type") == "ping") {
                    webSocket.send(
                        JSONObject().apply {
                            put("type", "pong")
                            put("id", frame.optString("id"))
                        }.toString(),
                    )
                    return
                }
                onFrame(frame)
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.w(TAG, "connection failed: ${t.message}")
                socket = null
                scheduleReconnect()
            }

            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                Log.i(TAG, "socket closed: $code $reason")
                socket = null
                // A rejected token is not worth retrying every few seconds —
                // it will keep failing until the user fixes the config.
                if (code == BAD_TOKEN_CODE) {
                    Log.e(TAG, "brain rejected our token; not retrying")
                    closed = true
                    return
                }
                scheduleReconnect()
            }
        })
    }

    private fun scheduleReconnect() {
        if (closed) return
        val delay = min(INITIAL_BACKOFF_MS shl attempt, MAX_BACKOFF_MS)
        attempt = min(attempt + 1, MAX_ATTEMPT_SHIFT)
        Log.i(TAG, "reconnecting in ${delay}ms")
        Thread {
            Thread.sleep(delay)
            connect()
        }.start()
    }

    /**
     * Events are dropped rather than queued when offline.
     *
     * Usage state is only meaningful now: delivering "you have been scrolling
     * for 40 minutes" twenty minutes after the fact would have Juno nag about
     * something already over.
     */
    fun send(frame: JSONObject) {
        val socket = socket
        if (socket == null) {
            Log.d(TAG, "offline; dropping ${frame.optString("type")}")
            return
        }
        socket.send(frame.toString())
    }

    fun close() {
        closed = true
        socket?.close(1000, "shutting down")
        socket = null
    }

    private companion object {
        const val TAG = "JunoLink"
        const val INITIAL_BACKOFF_MS = 1_000L
        const val MAX_BACKOFF_MS = 60_000L
        const val MAX_ATTEMPT_SHIFT = 6
        const val BAD_TOKEN_CODE = 4401
    }
}
