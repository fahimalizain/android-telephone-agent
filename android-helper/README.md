# Android Dial Helper

Reference implementation for the LAN dialing endpoint described in
`docs/dial-helper.md`.

> **Reference only.** This code is not production-hardened. It has no TLS, no rate
> limiting, no replay protection, and a minimal HTTP parser. The bearer token is
> compiled into the APK and can be extracted. Use it on a trusted private LAN for
> personal experimentation only, and follow the calling regulations that apply to you.

## What it is

`DialService.kt` is a foreground Android service that listens on `0.0.0.0:8765` and
places a call when it receives an authenticated `POST /call`. It exists because Phone A
is the USB host for the Pi's audio gadget, so adb over USB is unavailable during normal
operation.

It provides:

- `GET /health`, an unauthenticated liveness and device/SIM check.
- `POST /call` with a bearer token and an E.164 number.
- `POST /hangup`, best effort, because Android restricts `endCall()` for non-dialer apps.

It does not touch call audio. Audio is carried by the Pi's USB Audio Class gadget.

## Before you start

- Android Studio with Android Gradle Plugin 8.6 or newer (API 35 support).
- Android SDK Platform 35 and build tools installed.
- A phone running Android 13 or newer. The code uses `minSdk 30` and `targetSdk 35`;
  the provisioning guide targets One UI on Android 13-15.
- `adb` available on the computer, over wireless debugging, as described in
  `docs/android-setup.md`.
- A 32-byte or longer random token, for example `openssl rand -hex 32`.

## Create the project

1. In Android Studio, choose **New Project**.
2. Select **No Activity** if available. Otherwise select **Empty Views Activity** or
   **Empty Compose Activity**; the helper has no UI, so either template works.
3. Set:
   - Name: `Dial Helper`
   - Package name: `dev.example.dialhelper` (or your own reverse-domain name)
   - Language: Kotlin
   - Minimum SDK: API 30
   - Build configuration language: Kotlin DSL
4. Finish the wizard and let Gradle sync.
5. In `app/build.gradle.kts`, set `compileSdk = 35` and `targetSdk = 35`. If the
   template set a lower target, change it and sync again.

The generated namespace and application ID must match the Kotlin package. If you choose
a different package, replace `dev.example.dialhelper` everywhere, including the commands
in this README and `docs/android-setup.md`.

This is a reference project, not a published app. The reference code has no launcher
activity, icon set, or Play Store configuration. A template activity, if you keep one, is
only a convenience.

## Add the code

1. Create the package directory under `app/src/main/java/`:
   `dev/example/dialhelper/`.
2. Copy `DialService.kt` from this directory into it. The file declares
   `package dev.example.dialhelper`, so the path and package must match.
3. Open `app/src/main/AndroidManifest.xml`. Merge the entries from this directory's
   `AndroidManifest.xml`:
   - Add the `<uses-permission>` elements before `<application>`.
   - Add the `<service android:name=".DialService" ...>` element inside `<application>`.
   - Keep the generated `<application>` attributes and any activity the template added.
   - Optionally uncomment the `BootReceiver` entry.

The service is exported so `adb shell am start-foreground-service` can start it during
provisioning. It ignores all intent data; the only control surface is the token-protected
LAN API. A hardened build should set `android:exported="false"` and start it from a
launcher activity or the boot receiver.

`DialService.kt` also contains a small `BootReceiver` class. It is only used if the
matching manifest receiver is uncommented. Android may reject a foreground-service start
from `BOOT_COMPLETED`; test it on your device.

## Configure the token

The token is a shared secret between the Pi and the phone. Use at least 32 random bytes.
Do not commit it, paste it into an issue, or reuse a token from a guide.

### Option A: edit the constant

Replace the placeholder in `DialService.kt`:

```kotlin
private const val API_TOKEN = "REPLACE_WITH_32_BYTE_RANDOM_TOKEN"
```

This is the simplest path for a local test. The token is in source and in the APK, so
keep the source private and never commit the edited file.

### Option B: BuildConfig

Keep the token out of the Kotlin source by generating a `BuildConfig` field.

In `app/build.gradle.kts`:

```kotlin
android {
  buildFeatures {
    buildConfig = true
  }
  defaultConfig {
    val token = project.providers.gradleProperty("dialHelperToken").orNull
      ?: error("Set dialHelperToken in ~/.gradle/gradle.properties")
    buildConfigField("String", "API_TOKEN", "\"$token\"")
  }
}
```

In `DialService.kt`, replace the constant with:

```kotlin
private val API_TOKEN = BuildConfig.API_TOKEN
```

Put the value in `~/.gradle/gradle.properties`, outside the repository:

```properties
dialHelperToken=<your-hex-token>
```

A `BuildConfig` value is still embedded in the APK and is equally extractable. This
option only keeps the token out of version control.

## Build a signed APK

In Android Studio:

1. Choose **Build → Generate Signed App Bundle or APK**.
2. Select **APK**, then **Next**.
3. Create a new keystore or select an existing one. Store it outside the repository and
   back it up; losing it prevents in-place updates.
4. Select the **release** build variant and finish.
5. The APK is written under `app/build/outputs/apk/release/` (Android Studio may open
   the folder for you).

From the command line, after adding a `signingConfigs` block that reads your keystore
from a gitignored properties file:

```sh
./gradlew assembleRelease
```

Verify the signature:

```sh
apksigner verify --print-certs app/build/outputs/apk/release/app-release.apk
```

Do not commit the keystore, `keystore.properties`, or the token. The repository's
`.gitignore` covers `*.key` and `*.pem`, but not every keystore extension; check before
you commit.

## Install and run

With wireless debugging connected:

```sh
adb install -r app/build/outputs/apk/release/app-release.apk

adb shell pm grant dev.example.dialhelper android.permission.CALL_PHONE
adb shell pm grant dev.example.dialhelper android.permission.READ_PHONE_STATE
adb shell pm grant dev.example.dialhelper android.permission.ANSWER_PHONE_CALLS
adb shell pm grant dev.example.dialhelper android.permission.POST_NOTIFICATIONS

adb shell cmd deviceidle whitelist +dev.example.dialhelper
adb shell am start-foreground-service -n dev.example.dialhelper/.DialService
```

If the shell is refused a background foreground-service start on your build, add a minimal
launcher activity that calls `startForegroundService()` and open it once.

Set the Samsung battery setting to Unrestricted as well:
Settings → Apps → Dial helper → Battery → Unrestricted. See `docs/android-setup.md` for
the full checklist, including Sleeping apps and auto-update settings.

Check that the service is listening:

```sh
curl -sS http://<phone-ip>:8765/health
# {"status":"ok","device":"SM-A235F","sim":"Jio"}
```

If `POST /call` returns `503 no SIM available`, grant `READ_PHONE_STATE`, set
`AGENT_SIM_LABEL` to match the account label, and reinstall.

## Test

Use the token that is compiled into the APK:

```sh
export PHONE_IP=192.168.1.50
export DIAL_TOKEN='<the-token-in-the-apk>'

curl -sS "http://$PHONE_IP:8765/health"

curl -i -sS -X POST "http://$PHONE_IP:8765/call" \
  -H "Authorization: Bearer $DIAL_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"number":"+919876543210"}'

curl -i -sS -X POST "http://$PHONE_IP:8765/hangup" \
  -H "Authorization: Bearer $DIAL_TOKEN"
```

A wrong token must return `401`. A call placed while the previous call is inside the
ten-minute window must return `409`. Watch the log:

```sh
adb logcat -s DialHelper
```

The log must never contain the destination number or the token.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Connection refused | Service not running, wrong IP, or port 8765 blocked |
| `401 unauthorized` | Token mismatch; rebuild after changing the constant |
| `503 no SIM available` | `READ_PHONE_STATE` missing, or no account matches `AGENT_SIM_LABEL` |
| `500 call permission denied` | `CALL_PHONE` not granted |
| SIM picker on the phone | Missing or wrong `EXTRA_PHONE_ACCOUNT_HANDLE` |
| `202 hangup-unavailable` | `endCall()` refused by the platform; see `docs/dial-helper.md` |
| Service disappears | Samsung sleeping apps or battery optimization; re-check the settings |

## Limitations

This is a reference, not a product. It lacks TLS, rate limiting, replay protection, a
real HTTP server, call-state tracking, and secure token storage. The HTTP parser handles
one request per connection, caps line and body size, and closes the socket, but it has
not been fuzzed. The foreground service can still be killed by vendor battery management.
`endCall()` is deprecated and restricted, so hangup may not work.

For the protocol, threat model, and provisioning details, read `docs/dial-helper.md` and
`docs/android-setup.md`.
