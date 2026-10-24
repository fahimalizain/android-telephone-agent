# Android Setup

Status: provisioning guide for the reference build
Applies to: Samsung Galaxy A23 4G (SM-A235F), One UI, Android 13-15

## Purpose

This guide turns a spare Galaxy A23 4G into the agent's phone: a charged, locked-down
device that places calls on command and routes their audio to a USB-C headset. It covers
the Phase 0 hardware check, the settings that keep the phone alive and charging, wireless
debugging for provisioning, helper installation, and audio-routing verification.

It does not cover the Raspberry Pi, the USB audio gadget, or the agent runtime. Those are
in `docs/raspberry-pi-setup.md` and `docs/voice-agent.md`.

## Prerequisites

| Item | Requirement |
|---|---|
| Phone | Samsung Galaxy A23 4G, model SM-A235F, One UI, agent SIM active |
| SIM | A voice-capable SIM with credit and a known number; SMS not required |
| Charger | Samsung 25 W USB-C PD charger |
| Hub | USB-C hub with PD passthrough and at least one USB data port |
| Headset | USB-C earphones with a microphone (USB audio class), for Phase 0 |
| Test phone | A second phone you can call and answer |
| Computer | macOS, Linux, or Windows with `adb` 30 or newer |
| Network | Phone and computer on the same private LAN, with client isolation off |

Do not start with the Pi connected. Phase 0 validates the phone, hub, charger, and
headset path first. If it fails, the rest of the build cannot work.

## Phase 0: audio routing and charging

Android routes call audio to a USB audio device only when the device enumerates as a USB
headset and USB audio routing is enabled. The Pi will later present itself as exactly
such a headset through the hub. Phase 0 proves that this phone and this hub will do it
with a known-good headset.

1. Insert the agent SIM and complete any carrier provisioning.
2. Disable the SIM PIN (Settings → Security and privacy → More security settings → SIM
   card lock), or accept that a reboot will leave the SIM locked until you unlock it.
3. Charge the phone above 50 percent.
4. Connect the charger to the hub's PD input, the hub to the phone's USB-C port, and the
   USB-C headset to a data port on the hub. Use the exact hub and charger you plan to
   deploy.
5. Confirm the phone reports charging. On the lock screen, the battery line should say
   charging; Settings → Battery and device care → Battery shows the same.
6. Confirm a USB audio notification appears. One UI may label it "USB audio connected",
   "USB audio device connected", or similar; labels vary by version.
7. Call the test phone and answer it. Speak into the headset microphone and confirm the
   far end hears you. Listen and confirm the far end is in the headset, not the phone
   earpiece or speaker.
8. Keep the call open for two to three minutes. Confirm the battery percentage is not
   falling, or check `adb shell dumpsys battery` after wireless debugging is enabled. The
   phone must charge while the headset is in use.
9. Move the phone and cable gently, then repeat. A loose hub connection can re-enumerate
   the audio device mid-call and drop the route.

Pass criteria: two-way call audio through the headset, and charging continues for the
duration of the call. Both must hold with the hub in the path.

If it fails:

- Audio in the phone earpiece: check Developer options → Disable USB audio routing is
  OFF, try the headset directly in the phone, then try another hub.
- No microphone audio: the headset may be output-only, or the hub may not pass the
  microphone endpoint. Try a known headset with a microphone.
- Charging stops when the headset is connected: the hub is not passing PD and data at the
  same time, or its power budget is too small. Replace the hub.
- Nothing enumerates: try another cable and hub port; not every USB-C port carries data.

Do not proceed to the Pi until Phase 0 passes. The Pi gadget can only be as reliable as
this path.

## Enable Developer options

1. Open Settings → About phone → Software information.
2. Tap Build number seven times.
3. Enter the screen lock PIN, pattern, or password when prompted.
4. Return to the main Settings screen. Developer options appears near the bottom.

Menu labels vary by One UI version and carrier. If a path is missing, use the search
icon in Settings and search for the setting name.

## Enable Wireless debugging

The phone's USB-C port is the host port for the hub and the Pi. Android cannot be a USB
host and a USB device on the same port at the same time, so a laptop cannot attach as a
USB device while the hub is connected. Wireless debugging is the only adb path during
normal operation.

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
| USB audio routing | Settings → Developer options → Disable USB audio routing | OFF (default) | ON would force call audio to the phone speaker |
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

## Physical setup

- Remove the case. The phone charges continuously and benefits from airflow.
- Stand it upright in a ventilated spot, away from direct sun and soft surfaces.
- Strain-relieve the USB-C cable: loop it once and anchor it so the connector carries no
  weight.
- Do not bend the connector sideways. A worn USB-C port can re-enumerate the hub and drop
  the audio route mid-call.
- Keep the hub and charger off the floor and away from the phone's speaker and
  microphone openings.
- Check the battery for swelling monthly. Stop using a swollen battery.

## Install the helper

Build a signed APK as described in `android-helper/README.md`, then install it over the
wireless debugging connection:

```sh
adb install -r dial-helper.apk
```

The reference package is `dev.example.dialhelper`. Replace it with your package name in
all commands. Grant the runtime permissions:

```sh
adb shell pm grant dev.example.dialhelper android.permission.CALL_PHONE
adb shell pm grant dev.example.dialhelper android.permission.READ_PHONE_STATE
adb shell pm grant dev.example.dialhelper android.permission.ANSWER_PHONE_CALLS
adb shell pm grant dev.example.dialhelper android.permission.POST_NOTIFICATIONS
```

`READ_PHONE_STATE` is needed to enumerate phone accounts and read the SIM label;
`ANSWER_PHONE_CALLS` is only for best-effort hangup. Neither grants call audio or call
recording. See `docs/dial-helper.md` for the full permission rationale.

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

Start the service and check it from the computer:

```sh
adb shell am start-foreground-service -n dev.example.dialhelper/.DialService
curl -sS http://192.168.1.50:8765/health
# {"status":"ok","device":"SM-A235F","sim":"Jio"}
```

The reference manifest exports the service so adb can start it; for hardening, set
`android:exported="false"` and use a launcher activity. If the shell cannot start a
background foreground service, the same activity is the fallback. The `sim` value shows
the selected account label, or all available labels when none is configured.

## Verify audio routing during a call

Place a call through the helper, answer it on the test phone, and inspect the audio
state while the call is active:

```sh
adb shell dumpsys audio | grep -i usb
adb shell dumpsys audio | grep -i -E "communication|usb"
adb shell dumpsys usb | grep -i -E "audio|headset"
```

The exact output varies by One UI build. Look for a USB audio device in the device list
and for a communication route that uses it. A line mentioning `USB` in the communication
strategy is the signal you want. If only the phone earpiece or speaker appears, the route
did not switch.

Also check the phone's notification shade for the USB audio notification, and check
charging:

```sh
adb shell dumpsys battery | grep -E "status|powered|level"
```

`status: 2` means charging and `USB powered: true` confirms power. At the Protect battery
ceiling the phone may stop charging by design; USB power should still be present. Confirm
the far end still hears the headset microphone and you hear the far end in the headset.

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
The Pi should alert when `/health` stops responding.

### USB port orientation and wear

USB-C is reversible, but the port and cable are not indestructible. A worn port, lint, or
side load can cause re-enumeration. When the gadget disappears, call audio may fall back
to the phone's earpiece or speaker. Clean the port with a non-metallic tool, use a short
strain-relieved cable, and check `dumpsys usb` after moving the phone.

### Charging negotiation

The hub must pass data and charger power at the same time. Some hubs pass only USB 2.0
data and a limited charge, or fail to negotiate the phone's fastest charging mode. The
phone may charge slowly or not at all during a call. Check `dumpsys battery` and the
battery screen, and keep the phone below the Protect battery ceiling. A 25 W charger does
not guarantee 25 W through an arbitrary hub; verify on your device.

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
- Keep the screen lock enabled and test locked-screen dialing; treat physical access to
  the USB port as access to the gadget and charging path.

## Verification checklist

- Phase 0 passes, and a helper-placed call shows USB audio in `dumpsys audio`; the far
  end hears the headset microphone and the phone charges during the call.
- Developer options shows Stay awake ON and Disable USB audio routing OFF.
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
- USB audio routing, hub PD behavior, and charging speed are device- and hub-specific.
  Verify on your device; do not assume a spec sheet applies.
- The A23's USB-C port is the only data port. There is no separate debug port, and the
  phone cannot be a USB host and a USB device simultaneously.
- Protect battery reduces runtime on battery. During a power cut the phone has less
  capacity than a full charge would provide.
