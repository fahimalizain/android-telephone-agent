# Runbook

Procedures for a single operator when a call fails, audio is wrong, or the Pi or phone is unreachable. Each incident lists the symptom, impact, diagnostic commands, fix, and prevention.

The reference deployment runs `telephone-agent.service`, `uac-gadget.service`, and a helper on port
8765. Call control uses the token in `/etc/telephone-agent.env`; `/health` is unauthenticated. Confirm
paths before use, and verify device-specific behavior on your device.

```bash
export PHONE_HOST="192.168.1.50"    # DHCP reservation; verify
export REPO="$HOME/android-telephone-agent"  # repository checkout; verify
export PROVIDER_HOST="api.example.com"  # replace
export PROVIDER_HEALTH_URL="https://$PROVIDER_HOST/health"
```

Related: [operations](./operations.md), [troubleshooting](./troubleshooting.md), [Raspberry Pi setup](./raspberry-pi-setup.md), [Android setup](./android-setup.md),
[hardware](./hardware.md), and the [service unit](../scripts/systemd/telephone-agent.service).

For the Bluetooth HFP alternative, start with [Bluetooth HFP](./bluetooth-hfp.md). The
audio incident sections below (USB gadget, ALSA, UAC) apply to the USB path; for Bluetooth,
use `scripts/bluetooth-check.sh <mac>` and `scripts/bluetooth-audio-test.sh list` during
the call, then inspect the WirePlumber journal.

## First 5 minutes

1. Confirm the symptom and whether a call is active. End a stuck call before diagnosing anything else.
2. Check power: hub LED, Pi LED, phone charging indicator. Do not unplug anything yet.
3. From the LAN, ping the Pi and query the phone helper.
4. Run `sudo systemctl --failed` and `sudo journalctl -p err -b --no-pager | tail -n 40`.
5. Check `cat /sys/kernel/config/usb_gadget/agent/UDC` and `cat /proc/asound/cards`.
6. Run `timedatectl`; it must show synchronized and `Asia/Kolkata`.
7. Check the provider status page, balance, and key expiry. Record observations and change one thing at a time.

## When to give up and reboot

Reboot when two restarts of the affected unit fail with the same error, when the Pi does not answer ping or SSH for five minutes on known-good power, when the phone UI or modem is unresponsive after an
airplane-mode toggle, or when a call cannot be ended and audio is stuck.

Reboot the phone first for a stuck call, the Pi for a hung gadget or network. Allow one reboot per incident; if the fault returns immediately, stop and move to reflash or spare hardware. Never reboot the
Pi during an active call unless the call cannot be ended and the audio path is already unusable.

## 1. No call placed at the scheduled time

**Symptom.** No outbound call at the slot and no post-call record.
**Impact.** Missed check-in; S1 after three consecutive days.
**Diagnose.**

```bash
date -Is; timedatectl
sudo systemctl status telephone-agent.service --no-pager
systemctl list-timers --all | grep -i telephone
sudo journalctl -u telephone-agent.service --since "today 00:00" --no-pager | tail -n 120
curl -fsS --max-time 3 "http://$PHONE_HOST:8765/health"
```

Look for `call_skipped`, `missed_slot`, `duplicate_slot`, and timer activation lines.

**Fix.** Fix the first unit error. If the timer did not fire, correct its expression or time zone, then
`sudo systemctl daemon-reload` and check `systemctl list-timers --all`. If the slot is inside the grace
window, trigger the documented test path; otherwise record the miss, alert, and wait.

**Prevent.** Keep `Persistent=false`, enforce a grace window, and alert when no record exists two minutes after a slot.

## 2. Call connects but the callee hears silence

**Symptom.** The callee answers, but no agent speech reaches them.
**Impact.** The call is unusable; S2, or S1 if repeated.
**Diagnose.** Run the loopback test only when no call is active.

```bash
cat /proc/asound/cards
aplay -l; arecord -l
cat /sys/kernel/config/usb_gadget/agent/UDC
sudo systemctl status uac-gadget.service --no-pager
sudo journalctl -u telephone-agent.service -n 120 --no-pager | grep -iE 'playback|tts|alsa|underrun|error'
cd "$REPO" && ./scripts/audio-loopback-test.sh
```

On the phone, confirm call volume is up, Bluetooth is off for the USB path, and Disable USB audio routing is OFF.

**Fix.** Reseat the USB-C cable, restart `uac-gadget.service`, and place a supervised test call. Match the playback device in configuration to `aplay -l`. If UAC1 fails, try UAC2. Reboot the phone if its audio route is stuck.

**Prevent.** Check enumeration and the sound card at T-60 and run a loopback test weekly.

## 3. One-way audio

**Symptom.** The agent hears the callee but the callee does not hear the agent, or the reverse.
**Impact.** The conversation cannot proceed; S2, or S1 if recurring.
**Diagnose.**

```bash
cat /proc/asound/cards
arecord -D "plughw:CARD=UAC1Gadget,DEV=0" -f S16_LE -r 48000 -c 2 -d 3 /tmp/agent-cap.wav
dmesg | tail -n 40 | grep -iE 'xrun|underrun|overrun|usb|audio'
sudo journalctl -u telephone-agent.service -n 120 --no-pager | grep -iE 'capture|playback|rms|silence'
```

Stop the agent before the capture; this tests the path, not a live call. Use the card name from `arecord -l` if it differs, and confirm no Bluetooth headset is connected or call routed to the earpiece.

**Fix.** Restart the gadget and place a supervised test call, reseat the cable, and try another hub port. Test the phone with USB-C earphones to separate a phone fault from a Pi fault. Try UAC2 if UAC1 fails, and reboot the phone
if the route remains on the earpiece.

**Prevent.** Keep a known-good cable and hub, run a monthly two-way test, and alert on the one-way heuristic.

## 4. Pi unreachable after a reboot

**Symptom.** No SSH, no ping, no heartbeat after a power cycle or update.
**Impact.** No calls until restored; S1 if it lasts into a slot.
**Diagnose.** From another LAN host:

```bash
ping -c 3 raspberrypi.local
arp -a | grep -i -E 'raspberry|b8:27:eb|dc:a6:32|e4:5f:01'
```

At a console, or after access returns:

```bash
ip -brief addr
sudo systemctl --failed
sudo journalctl -b -p err --no-pager
vcgencmd get_throttled
```

Check router DHCP leases and the LED pattern: solid red power plus blinking green activity is normal.

**Fix.** Restore the reserved address and any edited boot files. Reseat the microSD card and power cycle with a known-good supply. If the card is corrupt, reflash and restore configuration.

**Prevent.** Use a DHCP reservation, back up boot files before editing, keep a prepared card, and avoid remote boot-file edits without a rollback path.

## 5. USB gadget missing or UAC card absent

**Symptom.** `UDC` is empty or missing and `/proc/asound/cards` has no UAC card.
**Impact.** Calls may connect with no audio; S1 if it blocks a slot.
**Diagnose.**

```bash
cat /sys/kernel/config/usb_gadget/agent/UDC
cat /proc/asound/cards
sudo systemctl status uac-gadget.service --no-pager
sudo journalctl -u uac-gadget.service -b --no-pager | tail -n 80
dmesg | grep -iE 'dwc2|libcomposite|uac|g_audio|configfs'
grep -E 'dtoverlay=dwc2|modules-load' /boot/firmware/config.txt /boot/firmware/cmdline.txt
```

**Fix.** Restart `uac-gadget.service`; if it fails, run `sudo /usr/local/sbin/uac-gadget.sh start` and read the error. Confirm `dtoverlay=dwc2,dr_mode=peripheral` and `modules-load=dwc2,libcomposite` are
present and spelled correctly. Reseat the data cable, try another hub port, and reboot. Test UAC2 if UAC1 persists.

**Prevent.** Enable the gadget unit at boot, check UDC and the card at T-60, and keep a known-good image.

## 6. Phone not charging or battery dropping

**Symptom.** Battery level falls over days, or the phone reports slow or no charging.
**Impact.** The phone may shut down and miss calls; S2.
**Diagnose.** On the phone, check Settings > Battery, or run `adb shell dumpsys battery` over wireless
debugging, for charging state. Test the original 25 W charger directly, then the hub path. Inspect the USB-C port for lint and cable fraying.

**Fix.** Replace the cable, then the hub or charger. Clean the port with a non-metallic tool. Power the Pi through its PWR port with 5 V/2 A so the hub budget goes to the phone. Confirm PD passthrough and the
charger rating. Remove the case and improve airflow.

**Prevent.** Use the specified charger and a data-capable PD hub, strain-relieve the cable, inspect the port monthly, and keep Protect battery enabled.

## 7. Helper app killed by One UI

**Symptom.** The phone is on the LAN but `/health` fails, or the helper stops after hours of idling.
**Impact.** No call can be placed; S1 if it blocks a slot.
**Diagnose.** Run `ping -c 3 "$PHONE_HOST"` and `curl -fsS "http://$PHONE_HOST:8765/health"`. On the
phone, check the foreground notification, Settings > Battery > Background usage limits, and the app's battery setting.

**Fix.** Set battery usage to Unrestricted, remove the app from Sleeping and Deep sleeping apps, allow background activity, and disable auto-disable for unused apps. Open the app, confirm its notification,
and lock it in recents. Reboot the phone and retest.

**Prevent.** Keep the battery exemption, use a foreground service, alert on health failure, and recheck settings after each One UI update.

## 8. Wrong SIM used on a dual-SIM phone

**Symptom.** The call uses the personal SIM, the caller ID is wrong, or a SIM picker blocks the call.
**Impact.** Wrong caller ID, possible cost, failed unattended call; S2.
**Diagnose.** Check the call log's SIM badge and SIM manager's default calling SIM, and run
`curl -fsS "http://$PHONE_HOST:8765/health"` to read the `sim` label. Compare with the configured SIM.

**Fix.** Set the default calling SIM to the agent SIM. Ensure the helper passes `TelecomManager.EXTRA_PHONE_ACCOUNT_HANDLE`; without it One UI can show a picker. Correct the configured
agent SIM label, restart the helper, and place a supervised test call.

**Prevent.** Record the agent SIM label, check it before dialing, label the SIM trays, and alert when the reported label differs.

## 9. Call stuck, cannot hang up

**Symptom.** The call stays active, the agent has stopped, and the phone UI does not return to idle.
**Impact.** The phone is busy and the next slot cannot run; S2, or S1 if persistent.
**Diagnose.**

```bash
curl -fsS --max-time 3 "http://$PHONE_HOST:8765/health"
sudo journalctl -u telephone-agent.service -n 120 --no-pager | grep -iE 'hangup|end_call|max_duration'
```

Check whether `TelecomManager.endCall()` returned a permission error; it requires privileged access.

**Fix.** End the call from the phone UI. If it is unresponsive, toggle airplane mode for ten seconds and
wait for the modem to register; restart the helper if hung, and reboot the phone if the modem stays stuck.

**Prevent.** Enforce a hard maximum duration, implement best-effort end-call, and make the agent release audio and mark the call failed even when the remote end is connected.

## 10. Agent stutters or latency exceeds 2 seconds

**Symptom.** Long pauses, cut-off TTS, or stage durations above the latency budget.
**Impact.** The conversation feels broken and callees may hang up; S2.
**Diagnose.**

```bash
ping -c 20 1.1.1.1
curl -o /dev/null -s -w 'dns=%{time_namelookup} connect=%{time_connect} tls=%{time_appconnect} first=%{time_starttransfer} total=%{time_total}\n' "https://$PROVIDER_HOST/"
top -b -n1 | head -n 15
vcgencmd measure_temp; vcgencmd get_throttled
iw dev wlan0 link
sudo journalctl -u telephone-agent.service -n 200 --no-pager | grep -iE 'latency|stt|llm|tts|turn|timeout'
```

Compare with the latency budget in the [design brief](./design-brief.md); check loss, signal, and heat.

**Fix.** Move the Pi closer to the access point or change channel. Use Ethernet on a Pi 4 where possible. Stop background updates during call windows, switch to a lower-latency provider region, and confirm the
power supply. Restart the provider session, or the agent only for a supervised test call, if it is stuck.

**Prevent.** Track p95 stage latency, alert above 2 seconds, avoid concurrent work, and keep thermal headroom.

## 11. Power outage recovery

**Symptom.** Mains power was lost; the Pi and router rebooted, and the phone may have stayed up.
**Impact.** The current or next call may be missed; S1 if a slot is lost.
**Diagnose.**

```bash
ping -c 3 "$PHONE_HOST"
uptime
timedatectl
sudo systemctl --failed
test -n "$(cat /sys/kernel/config/usb_gadget/agent/UDC)" && echo gadget-ok
curl -fsS --max-time 3 "http://$PHONE_HOST:8765/health"
```

**Fix.** Wait for the router and Pi to boot, then confirm time sync before any call. Restart the gadget if
it did not enumerate, confirm the phone is charging and reachable, and skip a missed slot with an alert.

**Prevent.** Put the hub and router on a mini UPS, boot the router first, keep the timer enabled and the hardware watchdog active, and run the T-60 gate before every call.

## 12. Provider outage or expired API key

**Symptom.** STT, LLM, or TTS fails; logs show 401, 403, 429, or 5xx, or the health probe fails.
**Impact.** The agent cannot hold a conversation; S1 without a fallback.
**Diagnose.**

```bash
curl -fsS -o /dev/null -w '%{http_code}\n' "$PROVIDER_HEALTH_URL"
sudo journalctl -u telephone-agent.service -n 200 --no-pager | grep -iE '401|403|429|5[0-9][0-9]|auth|quota'
date -Is
getent hosts "$PROVIDER_HOST"
```

Check the status page, balance, key expiry, and clock. A skewed clock can look like an auth failure.

**Fix.** Rotate the key in the root-readable environment file, restart the agent only as part of a supervised test call, and confirm the request. Switch to the configured secondary provider if available. If neither works, skip the call and
alert rather than retrying a failing endpoint.

**Prevent.** Alert on balance and key expiry, configure two providers where possible, and run the T-60 provider check.

## 13. Inbound service not answering

**Symptom.** A call to the agent SIM rings but the agent never answers, or the inbound service is stopped.
**Impact.** Missed inbound calls; the phone may stay offhook and block the next call; S2.
**Diagnose.** Run the user-unit commands as the account that owns the PipeWire session:

```bash
systemctl --user status telephone-agent-inbound.service --no-pager
journalctl --user -u telephone-agent-inbound.service -n 120 --no-pager
read -rsp 'Helper token: ' HELPER_TOKEN; echo
curl -fsS --max-time 3 -H "Authorization: Bearer $HELPER_TOKEN" \
  "http://$PHONE_HOST:8765/state"
unset HELPER_TOKEN
```

Expect `active (running)`, no `inbound requires realtime.kind = 'grok'` error, and a
`{"state":"..."}` response. A call should move from `ringing` to `offhook` within about one
poll interval after the daemon answers. For a supervised check, call the agent SIM from
another handset and watch `/state` and the journal through answer, greeting, and hangup.
Confirm `[realtime].kind = "grok"` and that the service environment has `XAI_API_KEY`.

**Fix.** Start or restart the unit with `systemctl --user restart
telephone-agent-inbound.service`. If `/answer` returns `403`, grant `ANSWER_PHONE_CALLS`
and unlock the phone: `acceptRingingCall()` is best effort for a non-default dialer. A `409`
means the call stopped ringing or was answered manually. A `500` means Android threw; check
the helper log. If the unit cannot see PipeWire, run it in the PipeWire user session and
enable lingering. If the state is `offhook` but the caller hears silence, check the SCO
nodes with `scripts/bluetooth-check.sh <mac>` during the call.

**Prevent.** Run one supervised inbound call after each phone or One UI update, keep the
unit enabled with `Restart=always`, and alert when the unit is inactive or the helper has
not reached `idle` after a call.

## 14. Transcript retention and cleanup

Local transcripts are opt-in and contain full phone numbers and verbatim conversation
text. Keep a documented retention period, for example 30 days, and delete files on the
device rather than copying them into tickets, chat, or backups that do not cover call
content.

```bash
export TRANSCRIPT_DIR="${TRANSCRIPT_DIR:-$REPO/runtime/transcripts}"
ls -ld "$TRANSCRIPT_DIR"
find "$TRANSCRIPT_DIR" -maxdepth 1 -name '*.jsonl' -printf '%m %u:%g %p\n'
du -sh "$TRANSCRIPT_DIR"
find "$TRANSCRIPT_DIR" -maxdepth 1 -name '*.jsonl' -mtime +30 -print
```

The directory should be mode `700` and files mode `600`, owned by the service user. Review
the `find` output before deleting; then append `-delete` to the same command. Deletion on
an SD card is not a secure erase, so do not use production conversations for testing. If
`[transcript].enabled` is false, neither the directory nor any files should exist.

## After an incident

Record the times, slot, error, action, and test result. Update this runbook if a command was wrong. Do not close an S1 until a real two-way call succeeds; review alerts for noise that hid the signal.

## Related documents

[Operations](./operations.md), [troubleshooting](./troubleshooting.md),
[Bluetooth HFP](./bluetooth-hfp.md), [Inbound calls](./inbound.md),
[hardware](./hardware.md), [Raspberry Pi setup](./raspberry-pi-setup.md),
[Android setup](./android-setup.md), and the
[service unit](../scripts/systemd/telephone-agent.service).
