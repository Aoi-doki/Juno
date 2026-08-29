package dev.aoi.juno

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.os.Build
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast

/**
 * Setup and status.
 *
 * Deliberately a checklist rather than a chat window. The phone client does not
 * need a conversation UI — you talk to Juno through your laptop — but it very
 * much needs to tell you which of seven scattered Settings toggles is currently
 * off, because every one of them fails silently and none of them announces
 * itself when a One UI update reverts it.
 *
 * Plain views rather than Compose: this screen is a list and two text fields,
 * and it is worth roughly ten megabytes of APK not to pull in a UI toolkit for
 * it.
 */
class MainActivity : Activity() {

    private lateinit var settings: Settings
    private lateinit var checker: PermissionChecker
    private lateinit var container: LinearLayout

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        settings = Settings(this)
        checker = PermissionChecker(this)

        container = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(48, 64, 48, 64)
            setBackgroundColor(BACKGROUND)
        }
        setContentView(ScrollView(this).apply {
            setBackgroundColor(BACKGROUND)
            addView(container)
        })

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) !=
            PackageManager.PERMISSION_GRANTED
        ) {
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
        }
    }

    override fun onResume() {
        super.onResume()
        // Rebuilt every time the screen is shown, so returning from a Settings
        // page immediately reflects what you just changed.
        render()
    }

    private fun render() {
        container.removeAllViews()
        container.addView(heading("Juno"))
        container.addView(
            body("She watches what you're actually doing and tells you the truth about it."),
        )

        container.addView(heading("Where's the brain?"))
        val urlField = EditText(this).apply {
            hint = "ws://juno-brain:8765/ws"
            setText(settings.brainUrl)
            setTextColor(TEXT)
            inputType = InputType.TYPE_TEXT_VARIATION_URI
        }
        val tokenField = EditText(this).apply {
            hint = "shared token"
            setText(settings.token)
            setTextColor(TEXT)
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
        }
        container.addView(urlField)
        container.addView(tokenField)
        container.addView(
            Button(this).apply {
                text = "Save and connect"
                setOnClickListener {
                    settings.brainUrl = urlField.text.toString()
                    settings.token = tokenField.text.toString()
                    if (settings.configured) {
                        JunoService.start(this@MainActivity)
                        toast("Connecting…")
                    } else {
                        toast("Both the URL and the token are needed")
                    }
                    render()
                }
            },
        )

        val missing = checker.missing()
        container.addView(
            heading(if (missing.isEmpty()) "Permissions — all set" else "Permissions — ${missing.size} missing"),
        )
        if (missing.isNotEmpty()) {
            container.addView(
                body(
                    "Every one of these fails silently. If Juno goes quiet, come back " +
                        "here first.",
                ),
            )
        }
        for (grant in Grant.entries) {
            container.addView(row(grant, checker.granted(grant)))
        }

        container.addView(heading("Samsung"))
        container.addView(
            body(
                "One UI will put Juno to sleep whatever the above says. Two settings, " +
                    "both needed:\n\n" +
                    "• Battery → Background usage limits → turn off \"Put unused apps to " +
                    "sleep\", and add Juno to \"Never sleeping apps\"\n\n" +
                    "• Apps → Juno → Battery → Unrestricted\n\n" +
                    "\"Never sleeping apps\" is the one that actually matters — without it " +
                    "Juno is deep-slept after a few days of the screen being off.",
            ),
        )
    }

    private fun row(grant: Grant, granted: Boolean): View = LinearLayout(this).apply {
        orientation = LinearLayout.VERTICAL
        setPadding(0, 24, 0, 24)

        addView(
            TextView(this@MainActivity).apply {
                text = "${if (granted) "✓" else "✗"}  ${grant.title}"
                textSize = 17f
                setTextColor(if (granted) GREEN else RED)
            },
        )
        addView(
            TextView(this@MainActivity).apply {
                text = grant.why
                textSize = 13f
                setTextColor(MUTED)
                setPadding(40, 4, 0, 0)
            },
        )
        if (!granted) {
            grant.settingsIntent(this@MainActivity)?.let { intent ->
                addView(
                    Button(this@MainActivity).apply {
                        text = "Open settings"
                        setOnClickListener {
                            try {
                                startActivity(intent)
                            } catch (_: Exception) {
                                // Some OEMs remove or rename these screens.
                                toast("Couldn't open that screen — find it in Settings")
                            }
                        }
                    },
                )
            }
        }
    }

    private fun heading(text: String) = TextView(this).apply {
        this.text = text
        textSize = 22f
        setTextColor(BLUE)
        setPadding(0, 48, 0, 12)
        gravity = Gravity.START
    }

    private fun body(text: String) = TextView(this).apply {
        this.text = text
        textSize = 14f
        setTextColor(MUTED)
        setPadding(0, 0, 0, 12)
    }

    private fun toast(message: String) =
        Toast.makeText(this, message, Toast.LENGTH_SHORT).show()

    private companion object {
        // Catppuccin Mocha, to match the rest of Aoi's things.
        val BACKGROUND = Color.parseColor("#1e1e2e")
        val TEXT = Color.parseColor("#cdd6f4")
        val MUTED = Color.parseColor("#a6adc8")
        val BLUE = Color.parseColor("#89b4fa")
        val GREEN = Color.parseColor("#a6e3a1")
        val RED = Color.parseColor("#f38ba8")
    }
}
