package dev.example.dialhelper

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.IBinder
import android.os.SystemClock
import android.telecom.PhoneAccount
import android.telecom.PhoneAccountHandle
import android.telecom.TelecomManager
import android.telephony.TelephonyManager
import android.util.Log
import org.json.JSONException
import org.json.JSONObject
import java.io.BufferedInputStream
import java.io.ByteArrayOutputStream
import java.io.IOException
import java.io.InputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketTimeoutException
import java.nio.charset.StandardCharsets
import java.security.MessageDigest
import java.util.concurrent.Semaphore
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

/**
 * Reference LAN dialing endpoint for the android-telephone-agent project.
 *
 * This file is a reference implementation, not production-hardened code. It is
 * dependency-free apart from the Android SDK and org.json. Read
 * android-helper/README.md and docs/dial-helper.md before using it.
 *
 * Design notes:
 * - A foreground service keeps a raw ServerSocket alive while the phone charges.
 * - The phone is a USB host during normal operation, so adb is not available and
 *   this LAN endpoint is the supported control path.
 * - Calls are placed through TelecomManager with an explicit PhoneAccountHandle so
 *   One UI does not show a SIM picker on dual-SIM devices.
 */
class DialService : Service() {

  companion object {
    private const val TAG = "DialHelper"

    private const val PORT = 8765
    private const val CHANNEL_ID = "dial_helper"
    private const val NOTIFICATION_ID = 1001

    private const val MAX_LINE_BYTES = 2048
    private const val MAX_HEADER_COUNT = 64
    private const val MAX_BODY_BYTES = 4096
    private const val SOCKET_TIMEOUT_MS = 5_000
    private const val REQUEST_DEADLINE_MS = 10_000L

    /**
     * Injected at build time from the dialHelperToken Gradle property (for
     * example in $GRADLE_USER_HOME/gradle.properties). When the property is
     * unset the build falls back to a placeholder, and tokenConfigured() keeps
     * /call and /hangup disabled. See android-helper/README.md for generation
     * and rotation.
     *
     * Production hardening: a token compiled into the APK can be extracted from
     * the APK. Prefer a per-install secret provisioned at runtime, and treat
     * anyone holding the token as authorized to place calls.
     */
    private val API_TOKEN = BuildConfig.API_TOKEN

    /**
     * Case-insensitive substring of the PhoneAccount label for the agent SIM,
     * for example "Jio" or "Airtel". Injected at build time from the
     * dialHelperSimLabel Gradle property; empty on a single-SIM device.
     *
     * On a dual-SIM device, leaving this empty makes the helper refuse to guess:
     * POST /call returns 503 instead of showing a SIM picker on the locked screen.
     * Verify the label on your device via GET /health.
     */
    private val AGENT_SIM_LABEL = BuildConfig.AGENT_SIM_LABEL

    /**
     * How long a placed call is assumed to remain active. isCallActive() is
     * checked before refusing a new call or after a refused hangup, but the
     * platform can decline to report call state, so the window still bounds a
     * call that may outlive the remote party hanging up. Keep it slightly above
     * the agent's maximum call duration.
     */
    private const val CALL_WINDOW_MS = 10 * 60 * 1000L

    /** Keep a successful hangup observable for a retry that lost its response. */
    private const val HANGUP_GRACE_MS = 60 * 1000L

    private val E164 = Regex("^\\+[1-9][0-9]{6,14}$")
  }

  private val telecom: TelecomManager by lazy {
    getSystemService(Context.TELECOM_SERVICE) as TelecomManager
  }

  private val running = AtomicBoolean(false)

  @Volatile
  private var serverSocket: ServerSocket? = null

  private val callLock = Any()

  // Bound concurrent client handlers so an unauthenticated slowloris cannot
  // exhaust threads or file descriptors. The reference still has no per-IP
  // rate limiting; see the limitations in README.md.
  private val clientSlots = Semaphore(8)

  @Volatile
  private var lastCallAt = 0L

  @Volatile
  private var lastHangupAt = 0L

  override fun onCreate() {
    super.onCreate()
    val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
    manager.createNotificationChannel(
      NotificationChannel(
        CHANNEL_ID,
        "Dial helper",
        NotificationManager.IMPORTANCE_LOW
      ).apply {
        description = "Shown while the LAN dialing endpoint is running"
        setShowBadge(false)
      }
    )
  }

  override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
    return try {
      startForegroundCompat()
      if (running.compareAndSet(false, true)) {
        thread(name = "dial-helper-http", isDaemon = true) { serve() }
      }
      START_STICKY
    } catch (e: RuntimeException) {
      // Android 12+ may reject a foreground service start from the background.
      Log.e(TAG, "could not start foreground service", e)
      stopSelf()
      START_NOT_STICKY
    }
  }

  override fun onBind(intent: Intent?): IBinder? = null

  override fun onDestroy() {
    running.set(false)
    try {
      serverSocket?.close()
    } catch (e: IOException) {
      Log.w(TAG, "error closing server socket", e)
    }
    serverSocket = null
    super.onDestroy()
    Log.i(TAG, "service stopped")
  }

  private fun startForegroundCompat() {
    val notification = buildNotification()
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
      startForeground(
        NOTIFICATION_ID,
        notification,
        ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE
      )
    } else {
      startForeground(NOTIFICATION_ID, notification)
    }
  }

  private fun buildNotification(): Notification =
    Notification.Builder(this, CHANNEL_ID)
      .setContentTitle("Dial helper running")
      .setContentText("Listening on 0.0.0.0:$PORT")
      .setSmallIcon(android.R.drawable.ic_menu_call)
      .setOngoing(true)
      .setCategory(Notification.CATEGORY_SERVICE)
      .build()

  // Production hardening: this endpoint is cleartext HTTP on the LAN. Terminate
  // TLS here (SSLServerSocket) or in front of the service before exposing it
  // beyond a trusted network. The bearer token is the only access control.
  private fun serve() {
    var socket: ServerSocket? = null
    try {
      val server = ServerSocket().apply {
        reuseAddress = true
        bind(InetSocketAddress("0.0.0.0", PORT))
      }
      // Publish the socket before the liveness check so onDestroy() can always
      // close it, including a stop/start race during startup.
      socket = server
      serverSocket = server
      if (!running.get()) return
      Log.i(TAG, "listening on 0.0.0.0:$PORT")

      while (running.get()) {
        val client = try {
          server.accept()
        } catch (e: IOException) {
          if (running.get()) Log.e(TAG, "accept failed", e)
          break
        }
        if (!clientSlots.tryAcquire()) {
          Log.w(TAG, "connection limit reached; rejecting client")
          try {
            client.close()
          } catch (e: IOException) {
            Log.w(TAG, "could not close rejected client: ${e.javaClass.simpleName}")
          }
          continue
        }
        try {
          thread(name = "dial-helper-client", isDaemon = true) {
            try {
              client.use { handle(it) }
            } catch (e: IOException) {
              Log.w(TAG, "connection failed: ${e.javaClass.simpleName}")
            } catch (e: RuntimeException) {
              Log.w(TAG, "request failed: ${e.javaClass.simpleName}")
            } finally {
              clientSlots.release()
            }
          }
        } catch (e: RuntimeException) {
          clientSlots.release()
          try {
            client.close()
          } catch (closeError: IOException) {
            Log.w(TAG, "could not close client: ${closeError.javaClass.simpleName}")
          }
          Log.w(TAG, "could not start client handler: ${e.javaClass.simpleName}")
        }
      }
    } catch (e: IOException) {
      Log.e(TAG, "server loop stopped: ${e.javaClass.simpleName}")
      if (running.get()) {
        Log.e(TAG, "listener unavailable; stopping service so it can restart")
        stopSelf()
      }
    } finally {
      try {
        socket?.close()
      } catch (e: IOException) {
        Log.w(TAG, "socket close failed: ${e.javaClass.simpleName}")
      }
      // Only the thread that still owns the field may clear it. A late old
      // thread must not clobber a newer server after a restart.
      if (serverSocket === socket) {
        serverSocket = null
        running.set(false)
      }
      Log.i(TAG, "server loop exited")
    }
  }

  private fun handle(client: Socket) {
    // Per-read timeout plus an absolute deadline: a client cannot hold a
    // worker indefinitely by trickling headers or a body.
    client.soTimeout = SOCKET_TIMEOUT_MS
    val deadline = SystemClock.elapsedRealtime() + REQUEST_DEADLINE_MS
    val input = BufferedInputStream(client.getInputStream())
    val output = client.getOutputStream()

    val requestLine = try {
      readLine(input, MAX_LINE_BYTES, deadline)
    } catch (e: IllegalArgumentException) {
      writeResponse(output, 400, errorBody("request line too long"))
      return
    }
    if (requestLine.isNullOrBlank()) {
      writeResponse(output, 400, errorBody("malformed request"))
      return
    }

    val parts = requestLine.split(' ')
    if (parts.size != 3 || !parts[2].startsWith("HTTP/1.")) {
      writeResponse(output, 400, errorBody("malformed request line"))
      return
    }
    val method = parts[0].uppercase()
    val path = parts[1].substringBefore('?')

    val headers = mutableMapOf<String, String>()
    try {
      var count = 0
      while (true) {
        val line = readLine(input, MAX_LINE_BYTES, deadline) ?: break
        if (line.isEmpty()) break
        count++
        if (count > MAX_HEADER_COUNT) {
          writeResponse(output, 400, errorBody("too many headers"))
          return
        }
        val colon = line.indexOf(':')
        if (colon <= 0) continue
        headers[line.substring(0, colon).trim().lowercase()] =
          line.substring(colon + 1).trim()
      }
    } catch (e: IllegalArgumentException) {
      writeResponse(output, 400, errorBody("header line too long"))
      return
    }

    // Production hardening: reject Transfer-Encoding: chunked explicitly,
    // enforce total header bytes, and consider HTTP/2 behind a real server.
    val lengthHeader = headers["content-length"]
    val contentLength = lengthHeader?.toIntOrNull()
    if (lengthHeader != null && contentLength == null) {
      writeResponse(output, 400, errorBody("invalid Content-Length"))
      return
    }
    if (contentLength != null && contentLength > MAX_BODY_BYTES) {
      writeResponse(output, 413, errorBody("body too large"))
      return
    }

    val body = if (contentLength != null && contentLength > 0) {
      readBody(input, contentLength, deadline)
    } else {
      ""
    }

    val response: Response = when {
      method == "GET" && path == "/health" -> Response(200, healthBody())

      method == "POST" && path == "/call" -> {
        when {
          !tokenConfigured() -> Response(503, errorBody("token not configured"))
          !authorized(headers) -> Response(401, errorBody("unauthorized"))
          else -> placeCall(body)
        }
      }

      method == "POST" && path == "/hangup" -> {
        when {
          !tokenConfigured() -> Response(503, errorBody("token not configured"))
          !authorized(headers) -> Response(401, errorBody("unauthorized"))
          else -> hangup()
        }
      }

      // GET with no body; safe to poll. The response deliberately omits any
      // caller identity because the app does not hold READ_CALL_LOG.
      method == "GET" && path == "/state" -> {
        when {
          !tokenConfigured() -> Response(503, errorBody("token not configured"))
          !authorized(headers) -> Response(401, errorBody("unauthorized"))
          else -> Response(200, JSONObject().put("state", callState()))
        }
      }

      method == "POST" && path == "/answer" -> {
        when {
          !tokenConfigured() -> Response(503, errorBody("token not configured"))
          !authorized(headers) -> Response(401, errorBody("unauthorized"))
          else -> answer()
        }
      }

      path == "/call" || path == "/hangup" || path == "/health" ||
        path == "/state" || path == "/answer" ->
        Response(405, errorBody("method not allowed"))

      else -> Response(404, errorBody("not found"))
    }

    Log.i(TAG, "event=request method=$method path=$path status=${response.code}")
    writeResponse(output, response.code, response.body)
  }

  private fun healthBody(): JSONObject {
    // Unauthenticated by design: it exposes the model and SIM/account label only.
    // If that is unacceptable, require the bearer token here as well.
    return JSONObject()
      .put("status", "ok")
      .put("device", Build.MODEL)
      .put("sim", simLabel())
  }

  private fun tokenConfigured(): Boolean =
    API_TOKEN.length >= 32 && !API_TOKEN.startsWith("REPLACE")

  private fun authorized(headers: Map<String, String>): Boolean {
    val header = headers["authorization"] ?: return false
    val prefix = "Bearer "
    if (!header.regionMatches(0, prefix, 0, prefix.length, ignoreCase = true)) {
      return false
    }
    val presented = header.substring(prefix.length).trim()
      .toByteArray(StandardCharsets.UTF_8)
    val expected = API_TOKEN.toByteArray(StandardCharsets.UTF_8)
    // MessageDigest.isEqual is the platform constant-time comparison. It does
    // not hide the token length, which is not treated as secret.
    return MessageDigest.isEqual(presented, expected)
  }

  private fun placeCall(body: String): Response {
    val number = try {
      JSONObject(body).getString("number")
    } catch (e: JSONException) {
      return Response(400, errorBody("invalid JSON or missing number"))
    }

    if (!E164.matches(number)) {
      return Response(400, errorBody("invalid number"))
    }

    synchronized(callLock) {
      if (checkSelfPermission(Manifest.permission.CALL_PHONE) !=
        PackageManager.PERMISSION_GRANTED
      ) {
        Log.w(TAG, "placeCall denied; CALL_PHONE not granted")
        return Response(403, errorBody("call permission denied"))
      }

      val now = SystemClock.elapsedRealtime()
      // Refuse an outbound call while any call is ringing or active, even when
      // this helper did not place it (for example an unanswered inbound call).
      // isCallActive() is null when the platform will not say; in that case the
      // window check below still protects calls this helper placed.
      if (isCallActive() == true) {
        return Response(409, errorBody("call already active"))
      }

      if (lastCallAt != 0L && now - lastCallAt < CALL_WINDOW_MS) {
        // The remote party can end the call without this service noticing, so
        // an in-memory window alone would block every later call. Clear it only
        // when Telecom positively reports that nothing is in progress.
        if (isCallActive() == false) {
          lastCallAt = 0L
          Log.i(TAG, "stale call window cleared")
        } else {
          return Response(409, errorBody("call already active"))
        }
      }

      val account = agentAccount()
        ?: return Response(503, errorBody("no SIM available"))

      val extras = Bundle().apply {
        putParcelable(TelecomManager.EXTRA_PHONE_ACCOUNT_HANDLE, account)
        putBoolean(TelecomManager.EXTRA_START_CALL_WITH_SPEAKERPHONE, false)
      }

      try {
        telecom.placeCall(Uri.fromParts("tel", number, null), extras)
      } catch (e: SecurityException) {
        // A missing runtime permission is a provisioning error, not a
        // transient 5xx that the client should retry.
        Log.e(TAG, "placeCall denied: ${e.javaClass.simpleName}")
        return Response(403, errorBody("call permission denied"))
      } catch (e: RuntimeException) {
        // Do not log the throwable: a platform exception may contain the URI.
        Log.e(TAG, "placeCall failed: ${e.javaClass.simpleName}")
        return Response(500, errorBody("call failed"))
      }

      lastCallAt = now
      lastHangupAt = 0L
      // Never log the destination number.
      Log.i(TAG, "outbound call requested")
      return Response(202, JSONObject().put("state", "dialing"))
    }
  }

  @Suppress("DEPRECATION")
  private fun hangup(): Response {
    synchronized(callLock) {
      val now = SystemClock.elapsedRealtime()
      if (lastCallAt == 0L) {
        // A retry after a lost 202 should observe the same success for a short
        // grace period instead of a misleading 409.
        if (lastHangupAt != 0L && now - lastHangupAt < HANGUP_GRACE_MS) {
          return Response(202, JSONObject().put("state", "hangup-requested"))
        }
        return Response(409, errorBody("no active call"))
      }

      if (checkSelfPermission(Manifest.permission.ANSWER_PHONE_CALLS) !=
        PackageManager.PERMISSION_GRANTED
      ) {
        Log.w(TAG, "hangup requested without ANSWER_PHONE_CALLS")
        return Response(202, JSONObject().put("state", "hangup-unavailable"))
      }

      return try {
        if (telecom.endCall()) {
          // Clear the window only after the platform confirms the hangup.
          // Otherwise a failed or refused endCall could let a second /call
          // overlap the call that is still active.
          lastCallAt = 0L
          lastHangupAt = now
          Log.i(TAG, "hangup requested")
          Response(202, JSONObject().put("state", "hangup-requested"))
        } else {
          // No foreground call was ended. That is the normal outcome when the
          // remote party hung up first, but it also happens when the platform
          // refuses a non-dialer app. Clear the window only if Telecom confirms
          // that no call is active; otherwise /call could overlap a live call.
          if (isCallActive() == false) {
            lastCallAt = 0L
            lastHangupAt = now
            Log.i(TAG, "hangup skipped; no active call; window cleared")
            Response(202, JSONObject().put("state", "hangup-requested"))
          } else {
            Log.w(TAG, "endCall returned false")
            Response(202, JSONObject().put("state", "hangup-unavailable"))
          }
        }
      } catch (e: SecurityException) {
        // Common on modern Android for apps that are not the default dialer.
        Log.w(TAG, "endCall denied: ${e.javaClass.simpleName}")
        Response(202, JSONObject().put("state", "hangup-unavailable"))
      } catch (e: RuntimeException) {
        Log.w(TAG, "endCall failed: ${e.javaClass.simpleName}")
        Response(202, JSONObject().put("state", "hangup-unavailable"))
      }
    }
  }

  /**
   * Answers the currently ringing call with TelecomManager.acceptRingingCall().
   *
   * The ringing check happens before the permission check so a poll that races
   * with the caller hanging up gets 409 rather than a misleading permission
   * error. acceptRingingCall() is best effort for non-dialer apps on some vendor
   * builds; a SecurityException is reported as a permission problem and any
   * other RuntimeException as a failed answer.
   */
  @Suppress("DEPRECATION")
  private fun answer(): Response {
    synchronized(callLock) {
      if (callState() != "ringing") {
        return Response(409, errorBody("no ringing call"))
      }

      if (checkSelfPermission(Manifest.permission.ANSWER_PHONE_CALLS) !=
        PackageManager.PERMISSION_GRANTED
      ) {
        Log.w(TAG, "answer denied; ANSWER_PHONE_CALLS not granted")
        return Response(403, errorBody("answer permission denied"))
      }

      return try {
        telecom.acceptRingingCall()
        // Treat the inbound call like one this helper owns so /hangup can end
        // it and a following /call is guarded by the active window.
        lastCallAt = SystemClock.elapsedRealtime()
        lastHangupAt = 0L
        // Never log the caller or any number; the helper has no caller ID.
        Log.i(TAG, "inbound call answered")
        Response(202, JSONObject().put("state", "answering"))
      } catch (e: SecurityException) {
        Log.w(TAG, "acceptRingingCall denied: ${e.javaClass.simpleName}")
        Response(403, errorBody("answer permission denied"))
      } catch (e: RuntimeException) {
        Log.w(TAG, "acceptRingingCall failed: ${e.javaClass.simpleName}")
        Response(500, errorBody("answer failed"))
      }
    }
  }

  /**
   * Polls the platform call state without a PhoneStateListener.
   *
   * TelephonyManager.callState requires READ_PHONE_STATE. It may not report a
   * call on another subscription, and TelecomManager.isInCall covers calls on
   * another subscription or outside this app, so an idle TelephonyManager plus
   * an in-call Telecom is still reported as offhook. A refused query becomes
   * "unknown" instead of failing the HTTP request.
   */
  @Suppress("DEPRECATION")
  private fun callState(): String = try {
    val telephony = getSystemService(Context.TELEPHONY_SERVICE) as TelephonyManager
    when (telephony.callState) {
      TelephonyManager.CALL_STATE_RINGING -> "ringing"
      TelephonyManager.CALL_STATE_OFFHOOK -> "offhook"
      else -> if (telecom.isInCall) "offhook" else "idle"
    }
  } catch (e: SecurityException) {
    Log.w(TAG, "call state unavailable: ${e.javaClass.simpleName}")
    "unknown"
  } catch (e: RuntimeException) {
    Log.w(TAG, "call state unavailable: ${e.javaClass.simpleName}")
    "unknown"
  }

  /**
   * Returns true when Telecom reports an ongoing call, false when none is in
   * progress, or null when the state cannot be read. isInCall() covers calls
   * placed outside this helper as well, and it is the only signal the platform
   * offers without a PhoneStateListener. SecurityException means READ_PHONE_STATE
   * is missing or the caller is restricted; RuntimeException covers vendor
   * Telecom implementations that refuse the query.
   */
  private fun isCallActive(): Boolean? = try {
    telecom.isInCall
  } catch (e: SecurityException) {
    Log.w(TAG, "call state unavailable: ${e.javaClass.simpleName}")
    null
  } catch (e: RuntimeException) {
    Log.w(TAG, "call state unavailable: ${e.javaClass.simpleName}")
    null
  }

  private fun callAccounts(): List<PhoneAccountHandle> {
    // getCallCapablePhoneAccounts() requires READ_PHONE_STATE on most builds
    // unless this app is the default dialer, which it should not be.
    if (checkSelfPermission(Manifest.permission.READ_PHONE_STATE) !=
      PackageManager.PERMISSION_GRANTED
    ) {
      Log.w(TAG, "READ_PHONE_STATE not granted; cannot enumerate phone accounts")
      return emptyList()
    }
    return try {
      telecom.callCapablePhoneAccounts
    } catch (e: RuntimeException) {
      Log.e(TAG, "phone account enumeration failed; check READ_PHONE_STATE", e)
      emptyList()
    }
  }

  private fun simAccounts(): List<PhoneAccountHandle> {
    val accounts = callAccounts()
    if (accounts.isEmpty()) return emptyList()

    // getPhoneAccount() needs READ_PHONE_NUMBERS on target 31+. If it is
    // missing, keep single-SIM devices working but never guess between two
    // accounts; the install script and launcher request the permission.
    if (checkSelfPermission(Manifest.permission.READ_PHONE_NUMBERS) !=
      PackageManager.PERMISSION_GRANTED
    ) {
      Log.w(TAG, "READ_PHONE_NUMBERS not granted; cannot distinguish SIM accounts")
      return accounts.singleOrNull()?.let { listOf(it) } ?: emptyList()
    }

    return accounts.filter { handle ->
      val account = try {
        telecom.getPhoneAccount(handle)
      } catch (e: RuntimeException) {
        Log.w(TAG, "phone account lookup failed: ${e.javaClass.simpleName}")
        null
      }
      account != null &&
        (account.capabilities and PhoneAccount.CAPABILITY_SIM_SUBSCRIPTION) != 0
    }
  }

  private fun agentAccount(): PhoneAccountHandle? {
    val accounts = simAccounts()
    if (accounts.isEmpty()) return null
    if (AGENT_SIM_LABEL.isBlank()) return accounts.singleOrNull()

    val matches = accounts.filter { handle ->
      accountLabel(handle)?.contains(AGENT_SIM_LABEL, ignoreCase = true) == true
    }
    if (matches.size != 1) {
      Log.w(TAG, "SIM label matched ${matches.size} accounts; refusing to guess")
      return null
    }
    return matches.single()
  }

  private fun accountLabel(handle: PhoneAccountHandle): String? = try {
    telecom.getPhoneAccount(handle)?.label?.toString()
  } catch (e: RuntimeException) {
    Log.w(TAG, "phone account label unavailable", e)
    null
  }

  private fun simLabel(): String {
    val accounts = simAccounts()
    if (accounts.isEmpty()) return "unknown"
    val selected = agentAccount()
    if (selected != null) return accountLabel(selected) ?: "unknown"
    val labels = accounts.mapNotNull { accountLabel(it) }.distinct()
    return labels.joinToString(",").ifEmpty { "unknown" }
  }

  private fun readBody(input: InputStream, length: Int, deadline: Long): String {
    val data = ByteArray(length)
    var offset = 0
    while (offset < length) {
      if (SystemClock.elapsedRealtime() > deadline) {
        throw SocketTimeoutException("request deadline exceeded")
      }
      val read = input.read(data, offset, length - offset)
      if (read < 0) break
      offset += read
    }
    return String(data, 0, offset, StandardCharsets.UTF_8)
  }

  private fun readLine(input: InputStream, limit: Int, deadline: Long): String? {
    val buffer = ByteArrayOutputStream()
    while (true) {
      if (SystemClock.elapsedRealtime() > deadline) {
        throw SocketTimeoutException("request deadline exceeded")
      }
      val byte = input.read()
      if (byte == -1) {
        return if (buffer.size() == 0) {
          null
        } else {
          String(buffer.toByteArray(), StandardCharsets.UTF_8)
        }
      }
      if (byte == '\n'.code) {
        return String(buffer.toByteArray(), StandardCharsets.UTF_8)
      }
      if (byte != '\r'.code) {
        if (buffer.size() >= limit) {
          throw IllegalArgumentException("line too long")
        }
        buffer.write(byte)
      }
    }
  }

  private fun writeResponse(output: OutputStream, code: Int, body: JSONObject) {
    val bytes = body.toString().toByteArray(StandardCharsets.UTF_8)
    val head = buildString {
      append("HTTP/1.1 $code ${reason(code)}\r\n")
      append("Content-Type: application/json\r\n")
      append("Content-Length: ${bytes.size}\r\n")
      append("Connection: close\r\n")
      append("Cache-Control: no-store\r\n")
      append("\r\n")
    }
    output.write(head.toByteArray(StandardCharsets.US_ASCII))
    output.write(bytes)
    output.flush()
  }

  private fun reason(code: Int): String = when (code) {
    200 -> "OK"
    202 -> "Accepted"
    400 -> "Bad Request"
    401 -> "Unauthorized"
    403 -> "Forbidden"
    404 -> "Not Found"
    405 -> "Method Not Allowed"
    409 -> "Conflict"
    413 -> "Payload Too Large"
    500 -> "Internal Server Error"
    503 -> "Service Unavailable"
    else -> "Error"
  }

  private fun errorBody(message: String): JSONObject =
    JSONObject().put("error", message)

  private data class Response(val code: Int, val body: JSONObject)
}

/**
 * Optional boot receiver. Enable the matching <receiver> entry in the manifest
 * to start the service after a reboot. Android may refuse a background
 * foreground-service start here; verify on your device.
 */
class BootReceiver : BroadcastReceiver() {
  override fun onReceive(context: Context, intent: Intent) {
    if (intent.action != Intent.ACTION_BOOT_COMPLETED) return
    try {
      context.startForegroundService(Intent(context, DialService::class.java))
    } catch (e: RuntimeException) {
      Log.w("DialHelper", "could not start service after boot", e)
    }
  }
}
