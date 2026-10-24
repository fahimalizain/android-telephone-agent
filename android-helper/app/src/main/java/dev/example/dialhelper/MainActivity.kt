package dev.example.dialhelper

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.util.TypedValue
import android.view.Gravity
import android.view.ViewGroup
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView

/**
 * Minimal launcher: requests the runtime permissions the helper needs and asks
 * the system to start [DialService]. Deliberately uses the platform Activity and
 * widgets only, so the helper has no AndroidX or AppCompat dependency.
 *
 * The API token is never displayed, logged, or read by this activity.
 */
class MainActivity : Activity() {

    private lateinit var statusView: TextView
    private var startState: String = ""

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(buildContentView())
        requestHelperPermissions()
        startHelper()
    }

    private fun buildContentView(): LinearLayout {
        val padding = dp(24)
        return LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            gravity = Gravity.CENTER_HORIZONTAL
            setPadding(padding, padding, padding, padding)

            addView(TextView(context).apply {
                text = getString(R.string.app_name)
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 24f)
            })

            statusView = TextView(context).apply {
                setTextSize(TypedValue.COMPLEX_UNIT_SP, 16f)
                setPadding(0, dp(16), 0, dp(16))
            }
            addView(
                statusView,
                LinearLayout.LayoutParams(
                    ViewGroup.LayoutParams.MATCH_PARENT,
                    ViewGroup.LayoutParams.WRAP_CONTENT
                )
            )

            addView(Button(context).apply {
                text = getString(R.string.start_helper)
                setOnClickListener {
                    requestHelperPermissions()
                    startHelper()
                }
            })
        }
    }

    private fun dp(value: Int): Int =
        (value * resources.displayMetrics.density).toInt()

    private fun requestHelperPermissions() {
        val permissions = mutableListOf(
            Manifest.permission.CALL_PHONE,
            Manifest.permission.READ_PHONE_STATE,
            Manifest.permission.READ_PHONE_NUMBERS,
            Manifest.permission.ANSWER_PHONE_CALLS
        )
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            permissions += Manifest.permission.POST_NOTIFICATIONS
        }
        val missing = permissions.filter {
            checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED
        }
        if (missing.isNotEmpty()) {
            requestPermissions(missing.toTypedArray(), PERMISSION_REQUEST_CODE)
        }
    }

    /** Starts the foreground service, tolerating background-start restrictions. */
    private fun startHelper() {
        startState = try {
            startForegroundService(Intent(this, DialService::class.java))
            getString(R.string.status_start_requested)
        } catch (e: RuntimeException) {
            getString(R.string.status_start_failed, e.javaClass.simpleName)
        }
        updateStatus()
    }

    private fun updateStatus() {
        statusView.text = getString(
            R.string.status_template,
            packageName,
            PORT,
            startState
        )
    }

    companion object {
        private const val PERMISSION_REQUEST_CODE = 1001
        private const val PORT = 8765
    }
}
