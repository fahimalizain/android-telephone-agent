# Android Dial Helper

Reference implementation for the LAN dialing endpoint described in
`docs/dial-helper.md`. This directory is a complete Android Gradle project, not a set of
files to paste into a new project.

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

## Status

The project builds and produces an installable APK. It has been smoke-tested on an
Android 36 emulator: service startup, `/health`, bearer authentication, input validation,
the call window, hangup responses, and the signed release APK. It has not been validated
on a physical phone or against the USB audio hardware in this repository yet. Treat
permission prompts, real SIM-label routing, boot startup, actual hangup, and vendor
battery management as unverified until you test them on your device.

## Project layout

```text
android-helper/
  settings.gradle.kts
  build.gradle.kts
  gradle.properties
  gradlew
  gradlew.bat
  gradle/wrapper/gradle-wrapper.jar
  gradle/wrapper/gradle-wrapper.properties
  app/
    build.gradle.kts
    proguard-rules.pro
    src/main/AndroidManifest.xml
    src/main/java/dev/example/dialhelper/
      DialService.kt
      MainActivity.kt
    src/main/res/values/strings.xml
```

The root project is named `DialHelper` and has one `app` module. The namespace and
application ID are `dev.example.dialhelper`; change both in `app/build.gradle.kts` if
you rename the package, and move the Kotlin sources to the matching package directory.

`MainActivity` is a minimal launcher. Opening it requests the runtime permissions the
helper needs and asks the system to start `DialService`, then shows the package name,
port, and start status. It uses platform widgets only, so there is no AndroidX or
AppCompat dependency. It never reads or displays the API token.

`DialService.kt` contains both the foreground service and the `BootReceiver` that the
manifest registers for `BOOT_COMPLETED`. The receiver starts the service after a reboot
on a best-effort basis; Android may refuse a foreground-service start from that context.

Everything the build needs is already here. Do not create a new project, copy Kotlin
files, or merge manifest fragments.

You can also open the `android-helper` directory in Android Studio. Gradle sync works
with the checked-in wrapper because the project pins JDK 21 in `gradle.properties`; the
IDE's bundled JBR can be newer. The project needs an Android SDK with platform 36. No
manual project import or file shuffling is required.

## Toolchain

| Component | Version |
|---|---|
| Gradle wrapper | 8.14.6 |
| Android Gradle Plugin | 8.13.2 |
| Kotlin | 2.2.21 |
| JDK for Gradle | 21 |
| `compileSdk` / `targetSdk` | 36 / 36 |
| `minSdk` | 30 |
| Build tools | 36.1.0 |

The app compiles to Java 17 bytecode (`compileOptions` and Kotlin `jvmTarget` are 17),
but Gradle itself needs JDK 21. On this machine the Android SDK is at `/data/Android`,
with platform 36 and build tools 36.1.0 installed. Platform 35 is not required.

`local.properties` points `sdk.dir` at the SDK and is gitignored. On another machine,
install platform 36 and build-tools 36.1.0 through Android Studio's SDK Manager or
`sdkmanager`, and point `ANDROID_HOME` or `local.properties` at that SDK.

## Build from the command line

From this directory, select the JDK, SDK, and Gradle user home explicitly:

```sh
export JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64
export ANDROID_HOME=/data/Android
export GRADLE_USER_HOME=/scratch/gradle

./gradlew assembleDebug
```

Those paths are the ones used on this machine. Elsewhere, substitute your JDK 21, SDK,
and Gradle home; `GRADLE_USER_HOME` is optional and defaults to `~/.gradle`. The first
build downloads the Gradle 8.14.6 distribution and dependencies, so it needs network
access.

The debug APK is written to:

```text
app/build/outputs/apk/debug/app-debug.apk
```

## Debug vs signed release

`assembleDebug` signs with the local debug key. That is fine for installing and testing
on your own phone, but the debug key is machine-specific, so a debug APK is not suitable
for long-lived unattended deployment or for updates produced on another computer.

`assembleRelease` uses the release signing config only when
`android-helper/keystore.properties` exists. With that file, the signed APK is:

```text
app/build/outputs/apk/release/app-release.apk
```

Without `keystore.properties`, Gradle still assembles a release APK but leaves it
unsigned (AGP names it `app-release-unsigned.apk`), and Android will not install it. See
[Signing a release build](#signing-a-release-build).

Both build types embed the same token and SIM label from the Gradle properties. Changing
either property requires a rebuild and reinstall.

## Configure the token

The token is injected at build time as `BuildConfig.API_TOKEN` from the Gradle property
`dialHelperToken`. It is not stored in the repository.

1. Use a random token of at least 32 bytes, for example `openssl rand -hex 32`, which
   produces 64 hex characters. On this machine a generated token already exists at
   `/scratch/android/dial-helper/token`. Do not print its value into logs, shell history,
   a commit, or an issue.
2. Put the property in the Gradle user home `gradle.properties`, not in this project:

   ```text
   $GRADLE_USER_HOME/gradle.properties
   ```

   On this machine that is `/scratch/gradle/gradle.properties`; by convention elsewhere
   it is `~/.gradle/gradle.properties`. The property looks like:

   ```properties
   dialHelperToken=<the-64-hex-character-token>
   ```

3. Rebuild and reinstall. The APK must be rebuilt for a token change to take effect.

The build also accepts `DIAL_HELPER_TOKEN`, then a `dialHelperToken` key in the
gitignored `android-helper/keystore.properties`, as fallbacks. That keeps Android Studio
builds working when the IDE does not inherit `GRADLE_USER_HOME` from the shell. The
Gradle property wins when more than one is set.

If you already have the token file on this machine, append it without echoing it:

```sh
GRADLE_PROPS=/scratch/gradle/gradle.properties
grep -q '^dialHelperToken=' "$GRADLE_PROPS" || \
  printf 'dialHelperToken=%s\n' "$(cat /scratch/android/dial-helper/token)" >> "$GRADLE_PROPS"
```

If the property already exists and you are rotating the token, edit the line by hand
rather than running that command again.

If `dialHelperToken` is absent or blank, the build does not fail. It embeds the
placeholder `REPLACE_WITH_32_BYTE_RANDOM_TOKEN`, and the service answers `POST /call` and
`POST /hangup` with `503 {"error":"token not configured"}`. `GET /health` still works, so
you can use it to confirm the service is up before fixing the build.

The Pi needs the same token in `[dialer] token` in `config/config.toml`, or in the
`TELEPHONE_AGENT_DIALER__TOKEN` environment variable. Never commit the real
`config/config.toml`.

A `BuildConfig` field is compiled into the APK and is extractable from it. Keeping the
value in the Gradle user home only keeps it out of version control; it does not make the
token secret from anyone who has the APK. Treat the APK as a secret and rotate the token
by rebuilding, reinstalling, updating the Pi, and restarting the agent.

## Configure the SIM label (optional)

On a dual-SIM phone, the helper must know which account to use. Set the optional Gradle
property `dialHelperSimLabel`; it is injected as `BuildConfig.AGENT_SIM_LABEL`:

```properties
dialHelperSimLabel=Jio
```

The value is a case-insensitive substring of the phone account label, not a phone number.
Leave it blank on a single-SIM phone. On a dual-SIM phone, a blank value does not show a
SIM picker: `POST /call` returns `503 {"error":"no SIM available"}` instead of guessing.

Read the available labels from `GET /health`. When no label is configured, the `sim` field
lists the available account labels joined by commas; when one is configured, it shows the
selected label. Set `dialHelperSimLabel` to a distinctive part of the agent SIM's label,
rebuild, reinstall, and confirm that a test call goes out on the correct SIM.

## Signing a release build

Create `android-helper/keystore.properties`, which is gitignored, with these four keys:

```properties
storeFile=/absolute/path/to/release.jks
storePassword=<keystore-password>
keyAlias=<key-alias>
keyPassword=<key-password>
```

An absolute `storeFile` avoids ambiguity: the Gradle `file()` path is resolved relative
to the `app` module, not the directory containing the properties file. Keep the keystore
itself outside the repository.

On this machine, a local keystore and a `signing.properties` file already exist under
`/scratch/android/dial-helper/`, outside the repository. `signing.properties` has the
same four keys and can provide the values for `android-helper/keystore.properties`; the
keystore is `release.jks`. Back up both the keystore and the passwords. Losing them
prevents in-place updates, because Android requires the same signing key for an update.

Then build and verify:

```sh
./gradlew assembleRelease
apksigner verify --print-certs app/build/outputs/apk/release/app-release.apk
```

On this machine `apksigner` is under `/data/Android/build-tools/36.1.0/`. Do not commit
the keystore, `keystore.properties`, or any password. The project `.gitignore` covers
`.gradle/`, `local.properties`, `keystore.properties`, `*.jks`, `*.keystore`, and build
outputs, but check `git status` before you commit.

## Install and run

With wireless debugging connected as described in `docs/android-setup.md`, install the
debug APK (or the signed release APK):

```sh
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

If the phone already has the app installed with a different signing key, for example a
release build followed by a debug build, uninstall it first:

```sh
adb uninstall dev.example.dialhelper
```

The easiest way to grant permissions and start the service is to open the launcher
activity once:

```sh
adb shell am start -n dev.example.dialhelper/.MainActivity
```

`MainActivity` requests `CALL_PHONE`, `READ_PHONE_STATE`, `READ_PHONE_NUMBERS`,
`ANSWER_PHONE_CALLS`, and, on Android 13 and newer, `POST_NOTIFICATIONS`, then starts the
foreground service. Tap **Start helper** if the first start was refused. On the phone you
can also open the **Dial Helper** app from the launcher.

For headless provisioning, grant the permissions and start the exported service directly:

```sh
adb shell pm grant dev.example.dialhelper android.permission.CALL_PHONE
adb shell pm grant dev.example.dialhelper android.permission.READ_PHONE_STATE
adb shell pm grant dev.example.dialhelper android.permission.READ_PHONE_NUMBERS
adb shell pm grant dev.example.dialhelper android.permission.ANSWER_PHONE_CALLS
adb shell pm grant dev.example.dialhelper android.permission.POST_NOTIFICATIONS

adb shell cmd deviceidle whitelist +dev.example.dialhelper
adb shell am start-foreground-service -n dev.example.dialhelper/.DialService
```

`POST_NOTIFICATIONS` exists only on Android 13 and newer; skip that grant on older
builds. The other four permissions are required. `READ_PHONE_STATE` is used to enumerate
phone accounts; Android 14 and newer gate `TelecomManager.getPhoneAccount()` behind
`READ_PHONE_NUMBERS`, so the helper needs that permission to read the SIM/account label.
Neither permission grants call audio or phone-number access to the helper's API.

`DialService` is exported so adb can start it during provisioning. It ignores intent
data; the only control surface is the token-protected LAN API.

The manifest registers `BootReceiver` for `BOOT_COMPLETED`, so the service attempts to
start after a reboot. This is best effort: Android may block a foreground-service start
from the boot context, and vendor battery management can stop it later. If the service is
not listening after a reboot, open the launcher activity or start it with adb. Do not
rely on the boot path for unattended recovery until it is verified on your device.

On Samsung, also set Settings → Apps → Dial helper → Battery → Unrestricted. See
`docs/android-setup.md` for the full checklist, including Sleeping apps and auto-update
settings.

Check that the service is listening:

```sh
curl -sS http://<phone-ip>:8765/health
# {"status":"ok","device":"SM-A235F","sim":"Jio"}
```

If `POST /call` returns `503 no SIM available`, grant `READ_PHONE_STATE` and
`READ_PHONE_NUMBERS`, read the label from `/health`, set `dialHelperSimLabel` to match,
and rebuild and reinstall.

## API

The service listens on port 8765 over cleartext HTTP and closes the connection after one
request. All responses are JSON.

| Method | Path | Authentication | Body |
|---|---|---|---|
| `GET` | `/health` | None | None |
| `POST` | `/call` | `Authorization: Bearer <token>` | `{"number":"+919876543210"}` |
| `POST` | `/hangup` | `Authorization: Bearer <token>` | None |

Numbers must match `^\+[1-9][0-9]{6,14}$` and be normalized on the Pi. `202` on `/call`
means Telecom accepted the request, not that the call connected. A second call inside
the ten-minute active window returns `409`. A wrong or missing token returns `401`; a
missing `CALL_PHONE` permission returns `403` (not retried by the Pi client). Hangup is
best effort: `202 {"state":"hangup-unavailable"}` means the platform refused
`endCall()`. A successful `202 hangup-requested` is repeated for 60 seconds so a retry
after a lost response sees the same result. See `docs/dial-helper.md` for the full
contract and error table.

## Test

Use the token that is compiled into the installed APK, not a newly generated one. On
this machine the generated token file is `/scratch/android/dial-helper/token`; read it
into the environment without printing it:

```sh
export PHONE_IP=192.168.1.50
export DIAL_TOKEN="$(cat /scratch/android/dial-helper/token)"

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
| `503 token not configured` | `dialHelperToken` was missing or blank at build time; set it in the Gradle user home, rebuild, reinstall |
| `401 unauthorized` | Token mismatch; rebuild after changing `dialHelperToken`, and update the Pi to the same value |
| Gradle fails with an unsupported Java version | `JAVA_HOME` is not JDK 21; export the JDK 21 path before running `./gradlew` |
| SDK location not found | `ANDROID_HOME` or `local.properties` is wrong; install platform 36 and build-tools 36.1.0 |
| `INSTALL_FAILED_UPDATE_INCOMPATIBLE` | The APK is signed with a different key (debug vs release); uninstall the old package first |
| Connection refused | Service not running, wrong IP, or port 8765 blocked |
| `503 no SIM available` | `READ_PHONE_STATE` or `READ_PHONE_NUMBERS` missing, no account matches `AGENT_SIM_LABEL`, or the label is blank on dual SIM |
| `403 call permission denied` | `CALL_PHONE` not granted |
| SIM picker on the phone | The configured label did not match a call-capable account; read `sim` from `/health` and correct `dialHelperSimLabel` |
| `202 hangup-unavailable` | `endCall()` refused by the platform; see `docs/dial-helper.md` |
| Service disappears | Samsung sleeping apps or battery optimization; re-check the settings |
| Service is not listening after reboot | Android blocked the boot start; open the launcher activity or start the service with adb |

## Limitations

This is a reference, not a product. It lacks TLS, rate limiting, replay protection, a
real HTTP server, call-state tracking, and secure token storage. The HTTP parser handles
one request per connection, caps line and body size, and closes the socket, but it has
not been fuzzed. The foreground service can still be killed by vendor battery management.
`endCall()` is deprecated and restricted, so hangup may not work.

The APK builds, but no physical-device or end-to-end audio validation has been recorded.
SIM labels, locked-screen dialing, boot startup, and vendor-specific permission behavior
must be tested on the target phone before relying on them.

For the protocol, threat model, and provisioning details, read `docs/dial-helper.md` and
`docs/android-setup.md`.
