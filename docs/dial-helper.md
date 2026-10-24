# Dial Helper

Status: reference implementation, not production-hardened. Buildable APK with Android 36
emulator smoke tests; physical-phone and USB-audio validation pending. Applies to the
Samsung Galaxy A23 4G (SM-A235F), One UI, Android 13-15.

## Purpose

Phone A is the USB host for the Pi Zero 2 W: its only USB-C port is occupied by the hub
and audio gadget, and adb over USB is unavailable. The helper is a small LAN service
that places calls on request; audio travels over the USB gadget, not through this app.

Prerequisites: Phone A is provisioned per `docs/android-setup.md`; phone and Pi share a
private LAN without client isolation; the APK is installed with runtime permissions;
the service is running; the Pi holds the token compiled into the APK.

## API contract

The service binds `0.0.0.0:8765` and speaks HTTP/1.1 over cleartext TCP. Every response
is `application/json`, and the connection closes after one request. LAN use only.

### `GET /health`

Returns liveness, the device model, and the SIM/account label; no token required.

```http
GET /health HTTP/1.1

HTTP/1.1 200 OK
{"status":"ok","device":"SM-A235F","sim":"Jio"}
```

`device` is `Build.MODEL`. `sim` is the selected account label, or the available labels
joined by commas when none is configured; it is never a phone number. `/health` is
unauthenticated for liveness probes; require the token if the label is sensitive.

### `POST /call`

Requires `Authorization: Bearer <token>` and an E.164 number matching
`^\+[1-9][0-9]{6,14}$` (ASCII digits, 7 to 15 digits total).

```http
POST /call HTTP/1.1
Authorization: Bearer <token>
Content-Type: application/json
Content-Length: 26

{"number":"+919876543210"}

HTTP/1.1 202 Accepted
{"state":"dialing"}
```

`202` means Telecom accepted the request, not that the call connected; the helper does
not wait for setup. Numbers must match `^\+[1-9]\d{6,14}$` and be normalized on the Pi.

### `POST /hangup`

```http
POST /hangup HTTP/1.1
Authorization: Bearer <token>

HTTP/1.1 202 Accepted
{"state":"hangup-requested"}
```

Requires the bearer token and no body. `409 {"error":"no active call"}` means the helper
has no record of a placed call (it restarted, the window was reset, or no call was placed);
`202 {"state":"hangup-unavailable"}` means the platform refused to end the call. A
successful `202 hangup-requested` is repeated for a 60-second grace period so a client
that lost the first response can retry safely.

### Error model

| Status | Body example | Meaning |
|---|---|---|
| 400 | `{"error":"invalid number"}` | Missing/invalid JSON, or not E.164 |
| 401 | `{"error":"unauthorized"}` | Missing, malformed, or wrong token |
| 403 | `{"error":"call permission denied"}` | `CALL_PHONE` is not granted |
| 409 | `{"error":"call already active"}` | Call placed inside the active window |
| 409 | `{"error":"no active call"}` | `/hangup` with no recorded call |
| 503 | `{"error":"no SIM available"}` | No account, or no label match |
| 503 | `{"error":"token not configured"}` | Placeholder token left in the build |

## Authentication

- Use at least 32 random bytes, for example `openssl rand -hex 32`.
- Send only in `Authorization: Bearer <token>`. Query-string tokens are not supported.
- Compare with `MessageDigest.isEqual`; token length is not treated as secret.
- Never log the token, the body, or the destination number.
- The token is compiled into the APK and is extractable. Treat the APK as a secret.
- Rotation is manual with no overlap window: change `dialHelperToken` in the Gradle user
  home, rebuild, reinstall, update the Pi, and restart the service.

## Android permissions

| Permission | Protection | Why |
|---|---|---|
| `CALL_PHONE` | Dangerous, runtime | `TelecomManager.placeCall()` |
| `READ_PHONE_STATE` | Dangerous, runtime | Enumerate call-capable accounts |
| `READ_PHONE_NUMBERS` | Dangerous, runtime | Read account labels through `getPhoneAccount()` on Android 14+ |
| `ANSWER_PHONE_CALLS` | Dangerous, runtime | Best-effort `endCall()` |
| `INTERNET` | Normal | Opens the LAN listening socket |
| `FOREGROUND_SERVICE` | Normal | Keeps the socket alive |
| `FOREGROUND_SERVICE_SPECIAL_USE` | Normal, Android 14+ | Required with the `specialUse` type |
| `POST_NOTIFICATIONS` | Dangerous, runtime, 13+ | Ongoing notification |
| `RECEIVE_BOOT_COMPLETED` | Normal, optional | Start the service after reboot |
| `WAKE_LOCK` | Normal, optional | Only if a partial wake lock is added |

`READ_PHONE_STATE` is required by `getCallCapablePhoneAccounts()` unless the app is the
default dialer. Android 14 and newer additionally require `READ_PHONE_NUMBERS` for
`getPhoneAccount()`, which the helper uses to read the account label for SIM selection.
Neither permission is used for call audio, and neither grants call recording or call-log
access. Do not make the helper the default dialer; that changes the phone's call UI and
is unnecessary for dialing. Grant the runtime permissions during provisioning as shown
in `docs/android-setup.md`.

## Foreground service

`phoneCall` is not appropriate. That type is for an app that owns the calling
experience, such as the default dialer or a self-managed `ConnectionService`. This
helper has no in-call UI, does not hold the call, and only asks Telecom to place it.
`specialUse` describes the actual role; the manifest sets `foregroundServiceType="specialUse"`
and the `PROPERTY_SPECIAL_USE_FGS_SUBTYPE` property. Google Play reviews `specialUse`;
this project sideloads the APK, so no Play review applies.

The service calls `startForeground()` in `onStartCommand()` with
`FOREGROUND_SERVICE_TYPE_SPECIAL_USE` on Android 14+ and the two-argument form on older
releases. The notification is `IMPORTANCE_LOW` and ongoing. If permission is denied on
Android 13+, the service still runs but its notification may not appear in the shade.

A partial wake lock is optional; the phone charges with Stay awake enabled, so the
reference takes none. If added, use a timeout and release it in `onDestroy()`.

## Placing calls

```kotlin
val extras = Bundle().apply {
    putParcelable(TelecomManager.EXTRA_PHONE_ACCOUNT_HANDLE, account)
}
telecom.placeCall(Uri.fromParts("tel", number, null), extras)
```

`placeCall()` routes through the system Telecom service and avoids a background
activity start. `CALL_PHONE` is enforced here.

`getCallCapablePhoneAccounts()` returns one handle per enabled account. The
`PhoneAccount.label` is usually the carrier name; `shortDescription` or a subscription's
ISO country can disambiguate identical labels. The handle id is opaque and changeable.
The reference matches `AGENT_SIM_LABEL` case-insensitively. Leave it empty on a single-SIM
phone. On dual SIM, set it and verify with `GET /health`; if no label matches, the helper
returns `503` instead of guessing.

Always pass `EXTRA_PHONE_ACCOUNT_HANDLE`. Without it, One UI may show a "Call with SIM
1 / SIM 2" picker, including over the lock screen. Nobody taps it on an unattended
phone, so the call never connects even though `placeCall()` returned normally. This is
the most common dual-SIM failure mode. A wrong handle is also harmful: the call goes
out on the other SIM, with the wrong caller ID and possibly without a plan. Labels vary
by One UI version, locale, and carrier branding; verify on your device.

## Hanging up

`TelecomManager.endCall()` requires `ANSWER_PHONE_CALLS`. It is deprecated, returns
`false` when it did not end anything, and some builds refuse it for non-dialer apps.

The helper treats hangup as best effort: `202 hangup-requested` means `endCall()`
returned `true`; `202 hangup-unavailable` means the platform refused; `409` means the
helper has no record of a placed call. Do not read `202` as proof the call ended. The
call window is cleared only after `endCall()` returns `true`; on a refusal or exception
it stays in place, so a following `/call` returns `409` instead of overlapping a call
that may still be active. The ten-minute `CALL_WINDOW_MS` gates new `/call` requests,
not `/hangup`, so a call that lasts longer than ten minutes can still be ended. The
fallback is for the agent to finish and the remote party to hang up, with a hard maximum
duration enforced by the Pi; the cellular leg may stay up and billed until then.

## Reliability

- `START_STICKY` restarts the service after a low-memory kill; `onStartCommand()` works
  with a null intent.
- The optional `BootReceiver` starts it after `BOOT_COMPLETED`, but Android may reject a
  background foreground-service start; verify on your device.
- Retry only connection failures and timeouts, at most twice with backoff. The helper
  does not implement `Idempotency-Key`, so a `/call` retry after a lost response can
  return `409` (or place a duplicate after a service restart). Do not retry `/call`
  blindly; use one stable key per call once the helper supports deduplication. Do not
  retry a `202`; the active-call timestamp is not idempotent across a restart. One
  scheduled call per slot is enforced on the Pi.
- Log through logcat tag `DialHelper`: start/stop, listen address, account label, outcome,
  failures. Never log numbers, bodies, or tokens.
- Do not hold a coroutine open for the call; placing a call is a short IPC and the active
  window is a timestamp.
- The reference uses one thread per connection; a multi-client deployment needs a bounded
  pool and rate limiting. Give Phone A a DHCP reservation so its URL does not change.

## Threat model

Anyone who can reach port 8765 and holds the token can call any E.164 number, including
premium-rate numbers. The token is the only authorization check.

- Keep the endpoint on the LAN. Do not port-forward, enable UPnP for it, or use a DMZ.
- The reference accepts any E.164 number. Enforce a destination allowlist on the Pi, or
  add one here before deployment.
- Use WPA2/WPA3 with a strong passphrase. On a shared network, assume the cleartext
  token and requests can be captured; TLS, mTLS, and rate limiting are out of scope for
  the reference, which also embeds the secret in the APK.
- Keep the APK and token out of source control and rotate if lost; there is no replay
  protection or token expiry, and `/health` is unauthenticated, revealing the model and
  carrier label.

## Testing with curl

Set the phone's LAN address and the token compiled into the APK (not a new one).

```sh
export PHONE_IP=192.168.1.50
export DIAL_TOKEN='replace-with-the-token-in-the-apk'
curl -sS "http://$PHONE_IP:8765/health"
# {"status":"ok","device":"SM-A235F","sim":"Jio"}
```

Place a call, then hang up:

```sh
curl -i -sS -X POST "http://$PHONE_IP:8765/call" \
  -H "Authorization: Bearer $DIAL_TOKEN" -H "Content-Type: application/json" \
  -d '{"number":"+919876543210"}'
# HTTP/1.1 202 Accepted / {"state":"dialing"}

curl -i -sS -X POST "http://$PHONE_IP:8765/hangup" \
  -H "Authorization: Bearer $DIAL_TOKEN"
# HTTP/1.1 202 Accepted / {"state":"hangup-requested"}
```

A second call inside the active window returns `409`; a wrong token returns `401`:

```sh
curl -i -sS -X POST "http://$PHONE_IP:8765/call" \
  -H "Authorization: Bearer wrong-token" -H "Content-Type: application/json" \
  -d '{"number":"+919876543210"}'
# HTTP/1.1 401 Unauthorized / {"error":"unauthorized"}
```

Run these from the Pi; the phone has no curl binary by default. Watch
`adb logcat -s DialHelper` while testing.

## Known limitations

- No call-state callback; the ten-minute window is a heuristic. A real callback needs
  `PhoneStateListener` and `READ_PHONE_STATE` (plus `READ_CALL_LOG` for history).
- `endCall()` is deprecated and restricted; some builds return `false` or throw even with
  `ANSWER_PHONE_CALLS`.
- Samsung battery management can stop the service; exclude it from Sleeping apps, Deep
  sleeping apps, and "Put unused apps to sleep", and re-check after updates.
- HTTP is cleartext, HTTP/1.1 only, one request per connection, no chunked bodies, no
  keep-alive, no TLS, no rate limiting, and no replay protection. The parser handles
  `Content-Length` and caps lines and body size, but is not a general-purpose server.
- The token is embedded in the APK and the port is fixed at 8765; changing either needs a rebuild.

## Verification

1. `GET /health` returns `200` with the expected SIM label. `POST /call` returns `202`
   with no SIM picker, the callee sees the agent SIM's number, and a repeat gives `409`.
2. `POST /hangup` returns `202`; confirm on the phone whether the call ended, and check
   `adb logcat -s DialHelper` for outcomes without any number or token.
3. After a reboot, check whether the service is listening.
