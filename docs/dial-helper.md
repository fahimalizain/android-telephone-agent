# Dial Helper

Status: reference implementation, not production-hardened. Buildable APK with Android 36
emulator smoke tests; physical-phone, inbound-call, and USB-audio validation pending.
Applies to the Samsung Galaxy A23 4G (SM-A235F), One UI, Android 13-15.

## Purpose

Phone A is the USB host for the Pi Zero 2 W: its only USB-C port is occupied by the hub
and audio gadget, and adb over USB is unavailable. The helper is a small LAN service
that places calls on request and exposes the cellular call state so the agent can notice
and answer an inbound call. Audio travels over the USB gadget or the Bluetooth HFP link
(through PipeWire), not through this app.

The helper is the call-state and signaling path only. Bluetooth/PipeWire carries audio
only: the HFP SCO nodes appear after the call is answered, not while it is ringing, and
the helper never touches media. Placing or answering a call changes cellular and HFP
state; it does not by itself open an audio stream.

Prerequisites: Phone A is provisioned per `docs/android-setup.md`; phone and Pi share a
private LAN without client isolation; the APK is installed with runtime permissions;
the service is running; the Pi holds the token compiled into the APK.

## API contract

The service binds `0.0.0.0:8765` and speaks HTTP/1.1 over cleartext TCP. Every response
is `application/json`, and the connection closes after one request. LAN use only. Every
endpoint except `GET /health` requires `Authorization: Bearer <token>`.

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

### `GET /state`

Requires the bearer token and no body. It is a side-effect-free poll: one request per
connection, safe to repeat.

```http
GET /state HTTP/1.1
Authorization: Bearer <token>

HTTP/1.1 200 OK
{"state":"ringing"}
```

| State | Meaning |
|---|---|
| `idle` | `TelephonyManager.callState` is idle and Telecom reports no call |
| `ringing` | `TelephonyManager.callState` is `CALL_STATE_RINGING` |
| `offhook` | `callState` is `CALL_STATE_OFFHOOK`, or Telecom reports a call while TelephonyManager does not |
| `unknown` | The platform refused the query with `SecurityException` or `RuntimeException` |

The cross-check with `TelecomManager.isInCall()` covers a call on another subscription,
which `TelephonyManager.callState` may not report. Such a call is therefore reported as
`offhook`, not `ringing`: the helper has only Telecom's boolean fallback for it, and
classifying it as ringing would require a `PhoneStateListener`. A platform error returns
`200` with `{"state":"unknown"}`; the request itself does not fail.

The response contains no caller ID and no phone number. The helper does not hold
`READ_CALL_LOG` and must not request it.

### `POST /answer`

Requires the bearer token and no body. Answers the currently ringing cellular call.

```http
POST /answer HTTP/1.1
Authorization: Bearer <token>
Content-Length: 0

HTTP/1.1 202 Accepted
{"state":"answering"}
```

- `409 {"error":"no ringing call"}` if `/state` is not `ringing` at the time of the
  request, including when the caller hung up first or the state is `unknown`.
- `403 {"error":"answer permission denied"}` if `ANSWER_PHONE_CALLS` is not granted, or
  if `acceptRingingCall()` throws `SecurityException`.
- `500 {"error":"answer failed"}` if `acceptRingingCall()` throws another
  `RuntimeException`.
- `202 {"state":"answering"}` means Telecom accepted the answer request. It does not
  guarantee that the call connected or that audio exists yet.

On success the helper records the inbound call as owned, so `/hangup` can end it and a
following `/call` is refused while it is active. `answering` is the immediate response
body only; a subsequent `/state` reports `offhook` once the call is up.

### Inbound state machine

```text
idle --incoming call--> ringing --POST /answer--> offhook --hangup/remote end--> idle
                          |
                          +--missed or declined--> idle
```

`/state` samples this machine; it is not a push callback. The agent should poll on the
order of once per second while waiting for a call, not in a tight loop. `/answer` is
valid only in `ringing`; in every other state it returns `409`. `unknown` is a read
failure, not a call phase, and `/answer` treats it as not ringing.

A ringing call also blocks outbound dialing: `POST /call` returns `409` while
`TelecomManager.isInCall()` reports the ringing or active call.

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

A `/call` is refused with `409 {"error":"call already active"}` whenever
`TelecomManager.isInCall()` reports a ringing or active call, including an inbound call
this helper did not place. That check happens before dialing and applies outside the
ten-minute window as well.

Inside the window, if Telecom reports no call, the window is stale — typically because
the remote party hung up first — and is cleared so the new call proceeds. The service
logs `stale call window cleared` without any number. When the state cannot be read, the
window is retained and `/call` returns `409`, so an unreadable platform state cannot
allow a second call to overlap one this helper placed.

### `POST /hangup`

```http
POST /hangup HTTP/1.1
Authorization: Bearer <token>

HTTP/1.1 202 Accepted
{"state":"hangup-requested"}
```

Requires the bearer token and no body. `409 {"error":"no active call"}` means the helper
has no record of a placed or answered call (it restarted, the window was reset, or no
call was placed). A call answered through `/answer` sets the same window, so `/hangup`
can end it. `202 {"state":"hangup-requested"}` means no call is active after the request:
either `endCall()` succeeded, or it returned `false` and `TelecomManager.isInCall()`
reported no call, in which case the window is cleared. `202 {"state":"hangup-unavailable"}`
means `endCall()` did not end anything and a call may still be active, or its state could
not be read; the window is kept. A successful `202 hangup-requested` is repeated for a
60-second grace period so a client that lost the first response can retry safely.

### Error model

| Status | Body example | Meaning |
|---|---|---|
| 400 | `{"error":"invalid number"}` | Missing/invalid JSON, or not E.164 |
| 401 | `{"error":"unauthorized"}` | Missing, malformed, or wrong token |
| 403 | `{"error":"call permission denied"}` | `CALL_PHONE` is not granted |
| 403 | `{"error":"answer permission denied"}` | `ANSWER_PHONE_CALLS` is missing, or `acceptRingingCall()` threw `SecurityException` |
| 409 | `{"error":"call already active"}` | Telecom reports a ringing or active call, or the state is unknown inside the window |
| 409 | `{"error":"no active call"}` | `/hangup` with no recorded call |
| 409 | `{"error":"no ringing call"}` | `/answer` while `/state` is not `ringing` |
| 500 | `{"error":"answer failed"}` | `acceptRingingCall()` threw a non-security `RuntimeException` |
| 503 | `{"error":"no SIM available"}` | No account, or no label match |
| 503 | `{"error":"token not configured"}` | Placeholder token left in the build |

## Authentication

Every endpoint except `GET /health` requires the bearer token. That includes `/state`
and `/answer`; neither returns a phone number.

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
| `READ_PHONE_STATE` | Dangerous, runtime | Read `TelephonyManager.callState` and enumerate call-capable accounts |
| `READ_PHONE_NUMBERS` | Dangerous, runtime | Read account labels through `getPhoneAccount()` on Android 14+ |
| `ANSWER_PHONE_CALLS` | Dangerous, runtime | `acceptRingingCall()` and best-effort `endCall()` |
| `INTERNET` | Normal | Opens the LAN listening socket |
| `FOREGROUND_SERVICE` | Normal | Keeps the socket alive |
| `FOREGROUND_SERVICE_SPECIAL_USE` | Normal, Android 14+ | Required with the `specialUse` type |
| `POST_NOTIFICATIONS` | Dangerous, runtime, 13+ | Ongoing notification |
| `RECEIVE_BOOT_COMPLETED` | Normal, optional | Start the service after reboot |
| `WAKE_LOCK` | Normal, optional | Only if a partial wake lock is added |

`READ_PHONE_STATE` is required by `TelephonyManager.callState` and by
`getCallCapablePhoneAccounts()` unless the app is the default dialer. The helper also
calls `TelecomManager.isInCall()` before trusting a stale call window and as the
cross-subscription fallback on `/state`; if the platform denies that query, the window is
retained and `/state` reports `unknown`. Android 14 and newer additionally require
`READ_PHONE_NUMBERS` for `getPhoneAccount()`, which the helper uses to read the account
label for SIM selection. Neither permission is used for call audio, and neither grants
call recording or call-log access. The helper does not request `READ_CALL_LOG`: it cannot
read caller ID, and `/state` intentionally exposes no phone number. Do not make the
helper the default dialer; that changes the phone's call UI and is unnecessary for
dialing or answering. Grant the runtime permissions during provisioning as shown in
`docs/android-setup.md`.

## Foreground service

`phoneCall` is not appropriate. That type is for an app that owns the calling
experience, such as the default dialer or a self-managed `ConnectionService`. This
helper has no in-call UI and does not own the connection; it only asks Telecom to place
or answer a call. `specialUse` describes the actual role; the manifest sets
`foregroundServiceType="specialUse"` and the `PROPERTY_SPECIAL_USE_FGS_SUBTYPE` property.
Google Play reviews `specialUse`; this project sideloads the APK, so no Play review
applies.

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

Before placing, the helper checks `TelecomManager.isInCall()`. If any call is ringing or
active — including an inbound call this helper did not place — `/call` returns `409
{"error":"call already active"}`. The guard runs outside the ten-minute window too, so an
unanswered inbound call cannot be overlapped by an outbound call. A `null` (unreadable)
state does not block an otherwise idle helper; the window check below still protects a
call this helper placed.

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

## Answering calls

The agent polls `GET /state`. On `ringing`, it calls `POST /answer`; the helper verifies
the state again and asks Telecom to answer:

```kotlin
telecom.acceptRingingCall()
```

`ANSWER_PHONE_CALLS` is enforced by the platform and checked by the helper. `202` means
the request was accepted; the call may still fail to connect, and a vendor build may
ignore the request. `acceptRingingCall()` is best effort for non-dialer apps and is
deprecated on recent API levels in favour of richer call-control APIs, but it remains the
supported simple path for this reference. On Samsung One UI, test whether the call is
actually answered while the phone is locked; do not assume `202` means connected.

The helper never sees caller ID. Do not add `READ_CALL_LOG` for it: that permission is
not required for answering and would widen the data the service can read. `/state`
reports only the call phase.

Bluetooth/PipeWire carries audio only. After the call is answered, the phone routes the
cellular audio to the connected HFP hands-free unit and PipeWire exposes the SCO
source/sink. The helper does not create those nodes and cannot make audio appear before
the call is answered. On the USB-gadget path, the audio device is likewise independent
of the helper.

If the agent needs the call to end, `POST /hangup` works for an answered inbound call
because `/answer` records the call in the same window used for outbound calls. A missed
or declined call returns to `idle`; no hangup is needed.

## Hanging up

`TelecomManager.endCall()` requires `ANSWER_PHONE_CALLS`. It is deprecated, returns
`false` when it did not end anything, and some builds refuse it for non-dialer apps.

The helper treats hangup as best effort and checks `TelecomManager.isInCall()` before
deciding what a `false` return means. `202 hangup-requested` means no call is active after
the request: either `endCall()` returned `true`, or it returned `false` and `isInCall()`
reported no call, in which case the window is cleared and the 60-second retry grace
starts. `202 hangup-unavailable` means `endCall()` returned `false` or threw while
`isInCall()` reported a call or could not be read; the window is kept so a following
`/call` returns `409` instead of overlapping a call that may still be active. `409` means
the helper has no record of a placed or answered call.

Do not read `202` as proof the call ended: on `hangup-requested` the helper either ended
the call or observed that none is active, but a vendor build can be wrong. The ten-minute
`CALL_WINDOW_MS` gates new `/call` requests, not `/hangup`, so a call that lasts longer
than ten minutes can still be ended. A `/call` inside the window is likewise allowed when
`isInCall()` reports no call, which recovers from a remote hangup without restarting the
service. Outside the window, `/call` still refuses to place while `isInCall()` reports a
ringing or active call. The fallback for a call the helper cannot observe is for the
agent to finish and the remote party to hang up, with a hard maximum duration enforced by
the Pi; the cellular leg may stay up and billed until then.

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
- Do not hold a coroutine open for the call; placing or answering a call is a short IPC
  and the active window is a timestamp.
- `/state` is side-effect free and safe to poll once per second while waiting for an
  inbound call. It is a sample, not a subscription; a call can arrive or end between
  polls, and `/answer` re-checks the state.
- The reference uses one thread per connection; a multi-client deployment needs a bounded
  pool and rate limiting. Give Phone A a DHCP reservation so its URL does not change.

## Threat model

Anyone who can reach port 8765 and holds the token can call any E.164 number, including
premium-rate numbers, and can answer a ringing call. The token is the only authorization
check.

- Keep the endpoint on the LAN. Do not port-forward, enable UPnP for it, or use a DMZ.
- The reference accepts any E.164 number. Enforce a destination allowlist on the Pi, or
  add one here before deployment.
- `/state` and `/answer` reveal and act on the cellular call phase but never expose a
  caller's number. An attacker with the token can still observe that a call is ringing
  and answer it.
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

Poll state and answer an inbound call:

```sh
curl -i -sS "http://$PHONE_IP:8765/state" \
  -H "Authorization: Bearer $DIAL_TOKEN"
# HTTP/1.1 200 OK / {"state":"idle"}

# While another phone is ringing this phone:
curl -i -sS -X POST "http://$PHONE_IP:8765/answer" \
  -H "Authorization: Bearer $DIAL_TOKEN"
# HTTP/1.1 202 Accepted / {"state":"answering"}
```

`/answer` without a ringing call must return `409 {"error":"no ringing call"}`. With
`ANSWER_PHONE_CALLS` revoked it must return `403 {"error":"answer permission denied"}`.
`/state` must never contain a phone number or caller ID. These calls are for a supervised
device test; do not place or answer a real cellular call from an automated test run.

A second call inside the active window returns `409` while Telecom reports the call as
active; a wrong token returns `401`:

```sh
curl -i -sS -X POST "http://$PHONE_IP:8765/call" \
  -H "Authorization: Bearer wrong-token" -H "Content-Type: application/json" \
  -d '{"number":"+919876543210"}'
# HTTP/1.1 401 Unauthorized / {"error":"unauthorized"}
```

Run these from the Pi; the phone has no curl binary by default. Watch
`adb logcat -s DialHelper` while testing. The log may contain `inbound call answered`,
but never the caller or a number.

## Known limitations

- Call state is sampled on each `/state` request; there is no `PhoneStateListener` and
  no push callback. A call that arrives or ends between polls is seen on the next one,
  and `/answer` can race with a caller hanging up (`409` is then correct).
- `TelephonyManager.callState` may not report a call on another subscription. Such a
  call is observed only through `TelecomManager.isInCall()` and appears as `offhook`, so
  `/answer` cannot answer that call in this reference.
- `acceptRingingCall()` is deprecated and restricted; some vendor builds, especially
  with a locked screen or a non-default dialer, may refuse it, throw, or accept it
  without connecting the call. `202` is not proof that the call is up.
- `endCall()` is deprecated and restricted; some builds return `false` or throw even with
  `ANSWER_PHONE_CALLS`.
- Caller ID is unavailable. The helper does not hold `READ_CALL_LOG` and must not request
  it; this is why `/state` returns only a phase.
- Samsung battery management can stop the service; exclude it from Sleeping apps, Deep
  sleeping apps, and "Put unused apps to sleep", and re-check after updates.
- HTTP is cleartext, HTTP/1.1 only, one request per connection, no chunked bodies, no
  keep-alive, no TLS, no rate limiting, and no replay protection. The parser handles
  `Content-Length` and caps lines and body size, but is not a general-purpose server.
- The token is embedded in the APK and the port is fixed at 8765; changing either needs a rebuild.

## Verification

1. `GET /health` returns `200` with the expected SIM label. `POST /call` returns `202`
   with no SIM picker, the callee sees the agent SIM's number, and a repeat gives `409`
   while the call is active.
2. `POST /hangup` returns `202`; confirm on the phone whether the call ended, and check
   `adb logcat -s DialHelper` for outcomes without any number or token. After the remote
   party hangs up first, `/hangup` must return `202 hangup-requested` and a following
   `/call` must be accepted without restarting the service.
3. With the phone idle, `GET /state` returns `{"state":"idle"}` and `/answer` returns
   `409`. Call the phone from another handset; within about one poll interval `/state`
   must return `{"state":"ringing"}`. `/answer` must return `202`, the call must connect,
   and `/state` must then return `{"state":"offhook"}`. Confirming that PipeWire exposes
   SCO nodes belongs to `docs/bluetooth-hfp.md`; the helper does not create them.
4. `POST /call` while the inbound call is ringing or answered must return `409`, not
   place a second call. `/hangup` after `/answer` must end the inbound call on a build
   where `endCall()` is permitted.
5. After a reboot, check whether the service is listening.
