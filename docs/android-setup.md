# Android Setup

Status: provisioning guide for the reference build
Applies to: Samsung Galaxy A23 4G (SM-A235F), One UI, Android 13-15

## Purpose

This guide turns a spare Galaxy A23 4G into the agent's phone: a charged, locked-down
device that places calls on command, answers calls when the inbound mode is enabled, and
routes their audio to a Linux host over Bluetooth HFP. It covers Bluetooth pairing and
routing checks, the settings that keep the phone alive and charging, adb over Wi-Fi for
provisioning, helper installation, and audio-route verification.

It does not cover the Linux host or the agent runtime. Those are in
[Bluetooth HFP](bluetooth-hfp.md) and [Voice agent](voice-agent.md).

## Prerequisites

| Item | Requirement |
|---|---|
| Phone | Samsung Galaxy A23 4G, model SM-A235F, One UI, agent SIM active |
| SIM | A voice-capable SIM with credit and a known number; SMS not required |
| Charger | Samsung 25 W USB-C PD charger, or another suitable charger |
| Host | Linux with BlueZ, PipeWire, and WirePlumber in the user session |
| Bluetooth | A working adapter on the host; onboard Bluetooth is sufficient |
| Test phone | A second phone you can call and answer |
| Computer | A workstation with `adb` 30 or newer for provisioning |
| Network | Phone and host on the same private LAN, with client isolation off |

The phone's USB port is free because Bluetooth is the audio link. It is used for charging;
a USB cable can also be used for adb during bench provisioning, but the production control
path is the helper's LAN endpoint.

## Phase 0: Bluetooth HFP routing and charging

Android routes call audio to a Bluetooth device when that device is paired, trusted, and
enabled for calls. The Linux host presents itself as an HFP hands-free unit (HF) and Phone
A is the audio gateway (AG). Phase 0 proves that this phone and this host route call audio
in both directions.

1. Insert the agent SIM and complete any carrier provisioning.
2. Disable the SIM PIN (Settings → Security and privacy → More security settings → SIM
   card lock), or accept that a reboot will leave the SIM locked until you unlock it.
3. Charge the phone above 50 percent.
4. On the host, install the WirePlumber fragment and restart the user service as described
   in [Bluetooth HFP](bluetooth-hfp.md) sections 3 and 4.
5. Pair Phone A with the host, trust the host, and enable **Calls and audio** for it. See
   [Bluetooth HFP](bluetooth-hfp.md) section 5.
6. Place a test call from Phone A to the test phone and answer it. Confirm that the call
   screen shows the Bluetooth route, not the earpiece or speaker.
7. On the host, run the check and audio test during the call:

   ```bash
   ./scripts/bluetooth-check.sh AA:BB:CC:DD:EE:FF
   ./scripts/bluetooth-audio-test.sh list
   ./scripts/bluetooth-audio-test.sh record 10 /tmp/bluetooth-downlink.wav
   ./scripts/bluetooth-audio-test.sh tone 2
   ```

   Replace the address with Phone A's. The check must show both SCO nodes; `record` must
   produce a non-empty WAV; the far end should hear the tone. See
   [Bluetooth HFP](bluetooth-hfp.md) sections 7 and 8.
8. Keep the call open for two to three minutes. Confirm the battery percentage is not
   falling, or check `adb shell dumpsys battery` after adb is connected. The phone must
   charge while Bluetooth carries the call.

Pass criteria: two-way call audio over HFP SCO, and charging continues for the duration of
the call. Both must hold before the helper is used for an unattended call.

If it fails:

- Audio stays in the earpiece or speaker: confirm **Calls and audio** is enabled, reconnect
  the phone, and check the route on the call screen.
- No SCO nodes: confirm the WirePlumber fragment is loaded, the phone is connected, and
  the call is active. SCO nodes do not exist while the phone is idle.
- No microphone audio: check the SCO sink direction and repeat the tone test; the remote
  party should hear the tone, not the host speaker.
- The phone does not charge: try another charger and cable, inspect the port for lint, and
  confirm the battery setting. Bluetooth does not use the USB data pins.

Do not proceed until Phase 0 passes. The host can only be as reliable as this Bluetooth
link.

## Enable Developer options

1. Open Settings → About phone → Software information.
2. Tap Build number seven times.
3. Enter the screen lock PIN, pattern, or password when prompted.
4. Return to the main Settings screen. Developer options appears near the bottom.

Menu labels vary by One UI version and carrier. If a path is missing, use the search
icon in Settings and search for the setting name.

## Enable Wireless debugging

Wireless debugging is the convenient adb path for provisioning. It does not depend on the
USB cable, which stays free for charging. A USB cable also works on the bench if you
prefer it.

1. Connect the phone and the computer to the same Wi-Fi network, not a guest network
   with client isolation.
2. Settings → Developer options → Wireless debugging → ON.
3. Tap Pair device with pairing code. A dialog shows a six-digit code and an IP address
   and port, for example `192.168.1.50:37123`. This is the pairing port.
4. On the computer, run `adb pair 192.168.1.50:37123` and enter the code when prompted.
5. Close the dialog. On the Wireless debugging screen, note the IP address and port under
   the toggle, for example `192.168.1.50:5555`. This is the connect port and is different
   from the pairing port.
6. Run `adb connect 192.168.1.50:5555`, then `adb devices`.

Pairing authorizes the computer's adb key. The phone may forget the pairing when Wireless
debugging is turned off, when the network changes, or after a reboot; the connect port
can also change. This is why wireless debugging is a provisioning and development path,
not the production control path. Turn it off when provisioning is complete.

If `adb devices` shows `unauthorized`, unlock the phone and accept the debugging prompt.
If it shows `offline`, run `adb disconnect`, re-enable Wireless debugging, and reconnect.
Some routers isolate wireless clients; disable AP isolation or use a different network.

## adb over Wi-Fi test

With the phone unlocked and wireless debugging connected:

```sh
adb devices
# 192.168.1.50:5555    device

adb shell am start -a android.intent.action.CALL -d tel:+919876543210
```

Replace the number with a test number. The system dialer opens and places the call. On a
dual-SIM phone, this path may show the SIM picker; the helper avoids that by passing a
phone account handle. End the test call with:

```sh
adb shell input keyevent KEYCODE_ENDCALL
```

`am start` is suitable for testing only. It depends on an activity start, can be blocked
by background-start restrictions, and does not choose a SIM account. Production calls go
through the helper.

## Settings checklist

Apply these after Phase 0 passes. Paths are for a recent One UI build; labels and nesting
vary by version and carrier. Verify the effect, not just the label.

| Setting | Path | Value | Reason |
|---|---|---|---|
| Protect battery | Settings → Battery and device care → Battery → Protect battery | ON (80-85%) | Reduces swelling during 24/7 charging |
| Stay awake | Settings → Developer options → Stay awake | ON | Keeps the screen, Wi-Fi, and adb alive while charging |
| Bluetooth calls | Settings → Connections → Bluetooth → host gear icon | **Calls and audio** ON | Routes call audio to the host; without it the call stays on the earpiece |
| Auto restart | Settings → General management → Reset → Auto restart | OFF | Avoids unattended reboots |
| Play Store updates | Play Store → profile → Settings → Network preferences → Auto-update apps | Don't auto-update apps | Avoids surprise app changes |
| Galaxy Store updates | Galaxy Store → Menu → Settings → Auto update apps | Turn off | Same |
| Software update | Settings → Software update → Auto download over Wi-Fi | OFF | Avoids an OTA reboot while unattended |
| Default calls | Settings → Connections → SIM manager → Default for calls | Agent SIM | Keeps the caller ID predictable |
| VoLTE | Settings → Connections → Mobile networks → VoLTE calls | ON for the agent SIM | Keeps voice on LTE where the carrier supports it |
| Put unused apps to sleep | Settings → Battery and device care → Battery → Background usage limits | OFF, or exclude the helper | Samsung otherwise limits background apps |
| Sleeping apps | Same → Sleeping apps | Exclude the helper | Prevents the helper from being frozen |
| Deep sleeping apps | Same → Deep sleeping apps | Exclude the helper | Same |
| Helper battery | Settings → Apps → Dial helper → Battery | Unrestricted | Keeps the foreground service alive |
| Notifications | Settings → Apps → Dial helper → Notifications | Allow | Keeps the foreground notification visible |
| SIM PIN | Settings → Security and privacy → More security settings → SIM card lock | OFF, or plan manual unlock | A locked SIM cannot place calls after a reboot |

Notes:

- Protect battery caps charging at roughly 80-85 percent; the toggle may be under
  Battery → More battery settings. Verify the ceiling on your device.
- Stay awake keeps the display on; lower brightness and keep the phone ventilated.
- Some carriers hide the VoLTE toggle or provision it automatically; confirm calls still
  connect after you change it.
- Samsung's background limits have several names: Sleeping apps, Deep sleeping apps, Put
  unused apps to sleep, and Adaptive battery. Exclude the helper from all of them.
- Lock screen: calls may be placed while locked; test this explicitly. Keeping a secure
  lock is preferable to disabling it, which is a last resort and exposes the device.
- Confirm the phone reconnects to the host after a reboot. Pairing and trust help, but a
  reconnect may still need a manual tap or `bluetoothctl connect`.

## Physical setup

- Remove the case. The phone charges continuously and benefits from airflow.
- Stand it upright in a ventilated spot, away from direct sun and soft surfaces.
- Strain-relieve the charging cable: loop it once and anchor it so the connector carries no
  weight.
- Keep the phone and host within Bluetooth range. A weak link (the validated run was about
  -87 dBm) can drop during call setup.
- Check the battery for swelling monthly. Stop using a swollen battery.

## Install the helper

`android-helper/` is a complete Android Gradle project, not a set of files to copy into a
new project. Build it with JDK 21 and Android SDK platform 36. The project uses
`compileSdk 36`, `targetSdk 36`, `minSdk 30`, and build tools 36.1.0; platform 35 is not
needed. On this machine:

```sh
cd android-helper
export JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64
export ANDROID_HOME=/data/Android
export GRADLE_USER_HOME=/scratch/gradle
./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

The debug APK is suitable for provisioning and testing. For an unattended deployment,
build and install a signed release APK instead; see `android-helper/README.md` for the
signing setup and the release output path.

The repository also includes host-side wrappers: `scripts/build-helper.sh` builds and
verifies the APKs, and `scripts/install-helper.sh` installs the newest one, grants the
permissions below, whitelists the package, starts the service, and can probe `/health`.

The bearer token is injected at build time from the `dialHelperToken` Gradle property in
`$GRADLE_USER_HOME/gradle.properties` (on this machine
`/scratch/gradle/gradle.properties`; conventionally `~/.gradle/gradle.properties`), never
from a file in the repository. Put the same token in the host's `[dialer] token` or
`TELEPHONE_AGENT_DIALER__TOKEN`. If the property is absent, the APK still builds with a
placeholder and `/call` and `/hangup` return `503`. A generated token already exists at
`/scratch/android/dial-helper/token`; never print or commit its value.

On a dual-SIM phone, also set the `dialHelperSimLabel` Gradle property to the agent SIM's
account label and rebuild. A blank label is correct on single SIM; on dual SIM it makes
`/call` return `503 no SIM available` rather than showing a SIM picker. Read the available
labels from `GET /health` before setting it.

The reference package is `dev.example.dialhelper`. Replace it with your package name in
all commands. Grant the runtime permissions:

```sh
adb shell pm grant dev.example.dialhelper android.permission.CALL_PHONE
adb shell pm grant dev.example.dialhelper android.permission.READ_PHONE_STATE
adb shell pm grant dev.example.dialhelper android.permission.READ_PHONE_NUMBERS
adb shell pm grant dev.example.dialhelper android.permission.ANSWER_PHONE_CALLS
adb shell pm grant dev.example.dialhelper android.permission.POST_NOTIFICATIONS
```

`POST_NOTIFICATIONS` exists only on Android 13 and newer; skip that command on older
builds. `READ_PHONE_STATE` is needed to enumerate phone accounts; Android 14 and newer
gate `TelecomManager.getPhoneAccount()` behind `READ_PHONE_NUMBERS`, which is needed to
read the SIM/account label. `ANSWER_PHONE_CALLS` is only for best-effort hangup and
inbound answering. None of these permissions grants call audio or call recording. See
`docs/dial-helper.md` for the full permission rationale.

Grant the battery exemption. The first command adds the package to the device-idle
whitelist; the second opens the system confirmation dialog for the same setting:

```sh
adb shell cmd deviceidle whitelist +dev.example.dialhelper
adb shell am start -a android.settings.REQUEST_IGNORE_BATTERY_OPTIMIZATIONS \
  -d package:dev.example.dialhelper
```

Tap Allow on the phone if the dialog appears. Verify:

```sh
adb shell cmd deviceidle whitelist | grep dialhelper
adb shell dumpsys deviceidle whitelist | grep dialhelper
```

On Samsung, also set Settings → Apps → Dial helper → Battery → Unrestricted. The adb
whitelist and the Samsung UI are separate controls; apply both.

The launcher activity is an alternative to the `pm grant` commands: it requests the
runtime permissions above and starts the service in one step:

```sh
adb shell am start -n dev.example.dialhelper/.MainActivity
```

`MainActivity` requests the runtime permissions above and starts `DialService`. It shows
the package name, listening port, and start status, with a **Start helper** button to
retry; it never displays the token. You can also tap the **Dial Helper** app icon on the
phone.

For headless provisioning, use the `pm grant` commands above and start the exported
service directly instead:

```sh
adb shell am start-foreground-service -n dev.example.dialhelper/.DialService
```

Then check it from the computer:

```sh
curl -sS http://192.168.1.50:8765/health
# {"status":"ok","device":"SM-A235F","sim":"Jio"}
```

The manifest exports the service so adb can start it; for hardening, set
`android:exported="false"` and rely on the launcher activity. If the shell cannot start a
background foreground service, the activity is the fallback. The `sim` value shows the
selected account label, or all available labels when none is configured.

The manifest also registers `BootReceiver` for `BOOT_COMPLETED`, so the service tries to
start after a reboot. This is best effort: Android may reject a foreground-service start
from the boot context, and vendor battery management can stop it later. Verify after a
reboot; if the service is not listening, open the launcher activity or start it with adb.
Do not treat the boot path as reliable on this build until you have tested it.

## Verify audio routing during a call

Place a call through the helper, answer it on the test phone, and inspect the audio state
while the call is active:

```sh
adb shell dumpsys audio | grep -i -E "communication|bluetooth"
adb shell dumpsys bluetooth_manager | grep -i -E "hfp|headset|sco"
```

The exact output varies by One UI build. Look for a communication route that uses
Bluetooth and for an HFP/headset entry for the host. If only the earpiece or speaker
appears, the route did not switch. Confirm the phone's Bluetooth details show **Calls and
audio**, and run the host-side check from [Bluetooth HFP](bluetooth-hfp.md) section 7.

Also check charging:

```sh
adb shell dumpsys battery | grep -E "status|powered|level"
```

`status: 2` means charging. At the Protect battery ceiling the phone may stop charging by
design; the charger should still be connected. Confirm the far end hears the SCO uplink
and the host records the downlink.

## Known quirks

### SIM picker on dual SIM

If `EXTRA_PHONE_ACCOUNT_HANDLE` is missing or does not match an enabled account, One UI
may show a "Call with SIM 1 / SIM 2" picker, including over the lock screen. The call
does not connect until someone taps it. Configure the agent SIM label, check `sim` in
`/health`, and verify the caller ID seen by the callee. A wrong handle sends the call on
the other SIM, with the wrong caller ID and possibly without a plan.

### Samsung killing foreground services

Samsung may stop the helper despite a foreground service. Check Sleeping apps, Deep
sleeping apps, Put unused apps to sleep, and the app's battery setting after every One UI
or carrier update. Useful checks:

```sh
adb shell dumpsys activity services dev.example.dialhelper | head -40
adb shell dumpsys deviceidle whitelist | grep dialhelper
```

Locking the app in the recent-apps view may help on some builds; verify on your device.
The host should alert when `/health` stops responding.

### Bluetooth disconnects

Bluetooth can drop during call setup on a weak link, especially at the start of the first
call after a reboot. The phone may remain paired but disconnected. Check
`bluetoothctl info <address>` on the host and reconnect with
`bluetoothctl connect <address>`; confirm **Calls and audio** is still enabled. The
validated run at about -87 dBm needed one manual reconnect after the first call dropped.

### Charging

The phone charges from its own USB-C charger, independent of the audio link. If charging
is slow or absent, try another cable and charger, clean the port with a non-metallic tool,
and confirm the battery setting. A 25 W charger only helps if the cable and port support
it.

### Wireless debugging is not durable

Pairing and the connect port can be lost across reboots, Wi-Fi changes, and Wireless
debugging toggles. Do not build automation on adb. Use it for provisioning, then turn it
off. The helper's LAN endpoint is the production path.

## Security

- Keep the helper on the LAN. Do not port-forward port 8765, enable UPnP for it, or put
  the phone in a DMZ.
- Use a strong Wi-Fi passphrase. The helper uses cleartext HTTP on the LAN, protected only
  by its bearer token.
- Keep the APK and its token private; the token is embedded in the APK and can be
  extracted.
- Turn off Wireless debugging after provisioning. Paired adb keys are powerful.
- Keep the screen lock enabled and test locked-screen dialing and answering; treat physical
  access to the phone as access to the SIM and the helper token.

## Verification checklist

- Phase 0 passes: a call routes over Bluetooth HFP, both SCO nodes exist, the far end hears
  the tone, and the phone charges during the call.
- **Calls and audio** is enabled for the host, and the phone reconnects after a reboot.
- Protect battery is ON; auto restart, Play Store auto-update, Galaxy Store auto-update,
  and software update auto-download are OFF.
- SIM manager uses the agent SIM for calls; VoLTE is ON if the carrier supports it.
- The helper is excluded from Samsung sleeping lists and set to Unrestricted.
- `adb devices` shows the phone over Wi-Fi, and `adb shell am start` places a test call.
- The helper is installed, runtime permissions are granted, and
  `cmd deviceidle whitelist` lists the package.
- `GET /health` returns the expected device and SIM label.
- A call placed while the phone is locked reaches the callee with the agent SIM's caller
  ID and no SIM picker.
- After a reboot, the service is either listening or the boot path is known to be
  unreliable on this build.

## Known limitations

- One UI menu labels and paths vary by version and carrier; wireless debugging is a
  development path, not an unattended control channel.
- Samsung battery management can still stop the helper after an update or under memory
  pressure.
- Bluetooth routing and codec negotiation are device-specific. The link may fall back to
  CVSD at 8 kHz instead of mSBC at 16 kHz. Verify on your device.
- The A23's onboard Bluetooth is controlled by the phone; the host cannot force a
  reconnect if the phone refuses or the adapter is reset.
- Protect battery reduces runtime on battery. During a power cut the phone has less
  capacity than a full charge would provide, and the host may be offline.
