# Runbook

Procedures for a single operator when a call fails, audio is wrong, or the host or phone
is unreachable. Each incident lists the symptom, impact, diagnostic commands, fix, and
prevention.

The reference deployment runs one long-lived daemon systemd **user** service for inbound
and outbound calls, a one-shot outbound client triggered by a user timer, and a helper on
port 8765. The client submits one call to the daemon; the daemon owns the audio path and
serializes calls. Call control uses the token in the host environment file; `/health` is
unauthenticated. Confirm paths before use, and verify device-specific behavior on your
device.

```bash
export PHONE_HOST="192.168.1.50"    # DHCP reservation; verify
export HOST="192.168.1.10"          # Linux host; verify
export BT_ADDRESS="AA:BB:CC:DD:EE:FF"  # Phone A; verify
export REPO="$HOME/android-telephone-agent"  # repository checkout; verify
export PROVIDER_HOST="api.example.com"  # replace
export PROVIDER_HEALTH_URL="https://$PROVIDER_HOST/health"
```

Related: [operations](./operations.md), [troubleshooting](./troubleshooting.md),
[Bluetooth HFP](./bluetooth-hfp.md), [Inbound calls](./inbound.md),
[Android setup](./android-setup.md), the
[daemon user service](../scripts/systemd/telephone-agent-daemon.service), the
[outbound client service](../scripts/systemd/telephone-agent-bluetooth.service), and the
[Bluetooth timer](../scripts/systemd/telephone-agent-bluetooth.timer).

## First 5 minutes

1. Confirm the symptom and whether a call is active. End a stuck call before diagnosing
   anything else.
2. Check power: host powered, phone charging indicator.
3. From the LAN, ping the host and query the phone helper.
4. Run `systemctl --user --failed`,
   `journalctl --user -p err -b --no-pager | tail -n 40`, and
   `systemctl --user status telephone-agent-daemon.service --no-pager`.
5. Run `./scripts/bluetooth-check.sh --idle "$BT_ADDRESS"` and, if a call is active,
   `./scripts/bluetooth-check.sh "$BT_ADDRESS"`.
6. Run `timedatectl`; it must show synchronized and `Asia/Kolkata`.
7. Check the provider status page, balance, and key expiry. Record observations and change
   one thing at a time.

## When to give up and reboot

Reboot when two restarts of the affected user unit fail with the same error, when the host
does not answer ping or SSH for five minutes on known-good power, when the phone UI or
modem is unresponsive after an airplane-mode toggle, or when a call cannot be ended and
audio is stuck.

Reboot the phone first for a stuck call, the host for a hung PipeWire or network. Allow one
reboot per incident; if the fault returns immediately, stop and move to a spare or
reinstall. Never reboot the host during an active call unless the call cannot be ended and
the audio path is already unusable.

## 1. No call placed at the scheduled time

**Symptom.** No outbound call at the slot and no post-call record.
**Impact.** Missed check-in; S1 after three consecutive days.
**Diagnose.**

```bash
date -Is; timedatectl
systemctl --user status telephone-agent-daemon.service --no-pager
systemctl --user status telephone-agent-bluetooth.service --no-pager
systemctl --user list-timers --all | grep -i telephone
journalctl --user -u telephone-agent-daemon.service --since "today 00:00" --no-pager | tail -n 120
journalctl --user -u telephone-agent-bluetooth.service --since "today 00:00" --no-pager | tail -n 60
curl -fsS --max-time 3 "http://$PHONE_HOST:8765/health"
```

Look for `call_skipped`, `missed_slot`, `duplicate_slot`, `busy`, timer activation lines,
and `inactive (dead)`. The client log shows whether the timer fired and whether the
request reached the daemon; the daemon log shows call setup, audio, and the result.

**Fix.** Start or fix the daemon first: the client exits with status 1 and "start
telephone-agent-daemon.service" when no daemon is listening. If the client reports the
daemon rejected the call as busy, an inbound or other outbound call was already active;
treat the slot as skipped and do not retry into the active call. If the timer did not
fire, correct its expression or time zone, then `systemctl --user daemon-reload` and check
`systemctl --user list-timers --all`. If the slot is inside the grace window, trigger the
documented test path; otherwise record the miss, alert, and wait.

**Prevent.** Keep `Persistent=false`, enforce a grace window, and alert when no record
exists two minutes after a slot. Verify the user manager survives logout by enabling
lingering.

## 2. Call connects but the callee hears silence

**Symptom.** The callee answers, but no agent speech reaches them.
**Impact.** The call is unusable; S2, or S1 if repeated.
**Diagnose.** Run the checks during a supervised call, not a production one.

```bash
./scripts/bluetooth-check.sh "$BT_ADDRESS"
./scripts/bluetooth-audio-test.sh list
./scripts/bluetooth-audio-test.sh tone 2
journalctl --user -u telephone-agent-daemon.service -n 120 --no-pager | grep -iE 'playback|tts|sco|pipewire|error'
pw-dump | grep -E 'bluez_(input|output)\.|api\.bluez5\.sco'
```

On the phone, confirm the call screen shows the Bluetooth route, the media volume is up,
and **Calls and audio** is enabled for the host.

**Fix.** Play the tone to the SCO sink; if the remote party hears it, the audio path is
fine and the fault is in TTS or the agent's playback logic. If not, reconnect the phone,
confirm the SCO node mapping, and restart the user service for a supervised call. Do not
switch the phone to the earpiece: that breaks the headset path.

**Prevent.** Check the idle Bluetooth state at T-60, run a supervised tone test weekly, and
alert when the full SCO check fails during a call.

## 3. One-way audio

**Symptom.** The agent hears the callee but the callee does not hear the agent, or the
reverse.
**Impact.** The conversation cannot proceed; S2, or S1 if recurring.
**Diagnose.**

```bash
./scripts/bluetooth-check.sh "$BT_ADDRESS"
./scripts/bluetooth-audio-test.sh record 5 /tmp/downlink.wav
./scripts/bluetooth-audio-test.sh tone 2
journalctl --user -u telephone-agent-daemon.service -n 120 --no-pager | grep -iE 'capture|playback|rms|silence|sco'
```

`record` tests the downlink (callee to agent); `tone` tests the uplink (agent to callee).
If `record` produces digital silence, the phone is not routing call audio to Bluetooth or
the source node is wrong. If the tone is inaudible at the far end, the sink node or the
phone route is wrong. Confirm `media.class` and the node names with `pw-dump`.

**Fix.** Reconnect the phone, confirm **Calls and audio**, and restart PipeWire/WirePlumber
or the user service as needed. If the phone keeps the call on the earpiece, toggle
Bluetooth on the call screen. If only one PipeWire direction is missing, restart
WirePlumber and place a new supervised call.

**Prevent.** Keep the host and phone close, run a monthly two-way test, and alert on the
one-way heuristic.

## 4. Host unreachable after a reboot

**Symptom.** No SSH, no ping, no heartbeat after a power cycle or update.
**Impact.** No calls until restored; S1 if it lasts into a slot.
**Diagnose.** From another LAN host:

```bash
ping -c 3 "$HOST"
arp -a | grep -i "$HOST"
ssh "$USER@$HOST" 'uptime; systemctl --user --failed --no-pager'
```

At a console, or after access returns:

```bash
ip -brief addr
systemctl --failed
journalctl -b -p err --no-pager
df -h /; df -i /
```

Check router DHCP leases and whether the host booted at all.

**Fix.** Restore the reserved address, the network configuration, and any edited boot
files. If the disk is full or corrupt, free space or restore from backup. If the Bluetooth
adapter is missing after boot, check `dmesg` and `bluetoothctl list`.

**Prevent.** Use a DHCP reservation, back up configuration before editing, keep a
known-good host snapshot, and avoid remote boot-file edits without a rollback path.

## 5. Bluetooth disconnected or SCO nodes missing

**Symptom.** The phone is paired but not connected, or a call connects with no SCO
source/sink.
**Impact.** Calls may connect with no audio; S1 if it blocks a slot.
**Diagnose.**

```bash
bluetoothctl show
bluetoothctl info "$BT_ADDRESS"
./scripts/bluetooth-check.sh --idle "$BT_ADDRESS"
# During a call:
./scripts/bluetooth-check.sh "$BT_ADDRESS"
journalctl --user -u wireplumber -b --no-pager | tail -n 120
pw-dump | grep -E 'bluez_(input|output)\.|api\.bluez5\.sco'
```

**Fix.** If the adapter does not advertise Headset (0x1108) and Handsfree (0x111e), install
or repair the WirePlumber fragment and restart the user service. If the phone is
disconnected, `bluetoothctl connect "$BT_ADDRESS"` and confirm **Calls and audio**. If the
phone is connected but SCO nodes are missing during a call, confirm the call route is
Bluetooth, then restart WirePlumber and place a new supervised call. A stale bond may need
to be removed on both sides and re-paired.

**Prevent.** Trust the device, run the idle check at T-60, enable lingering, and keep the
host and phone within range. Treat a weak link (the validated run was about -87 dBm) as a
known failure mode.

## 6. Phone not charging or battery dropping

**Symptom.** Battery level falls over days, or the phone reports slow or no charging.
**Impact.** The phone may shut down and miss calls; S2.
**Diagnose.** On the phone, check Settings > Battery, or run `adb shell dumpsys battery` over
wireless debugging, for charging state. Test the charger and cable directly. Inspect the
USB-C port for lint and cable fraying.

**Fix.** Replace the cable, then the charger. Clean the port with a non-metallic tool.
Remove the case and improve airflow. Confirm the charger rating and that the port is not
damaged. Bluetooth does not use the USB data pins, so a charge-only cable is acceptable.

**Prevent.** Use a suitable charger and cable, strain-relieve the cable, inspect the port
monthly, and keep Protect battery enabled.

## 7. Helper app killed by One UI

**Symptom.** The phone is on the LAN but `/health` fails, or the helper stops after hours of
idling.
**Impact.** No call can be placed; S1 if it blocks a slot.
**Diagnose.** Run `ping -c 3 "$PHONE_HOST"` and
`curl -fsS "http://$PHONE_HOST:8765/health"`. On the phone, check the foreground
notification, Settings > Battery > Background usage limits, and the app's battery setting.

**Fix.** Set battery usage to Unrestricted, remove the app from Sleeping and Deep sleeping
apps, allow background activity, and disable auto-disable for unused apps. Open the app,
confirm its notification, and lock it in recents. Reboot the phone and retest.

**Prevent.** Keep the battery exemption, use a foreground service, alert on health failure,
and recheck settings after each One UI update.

## 8. Wrong SIM used on a dual-SIM phone

**Symptom.** The call uses the personal SIM, the caller ID is wrong, or a SIM picker blocks
the call.
**Impact.** Wrong caller ID, possible cost, failed unattended call; S2.
**Diagnose.** Check the call log's SIM badge and SIM manager's default calling SIM, and run
`curl -fsS "http://$PHONE_HOST:8765/health"` to read the `sim` label. Compare with the
configured SIM.

**Fix.** Set the default calling SIM to the agent SIM. Ensure the helper passes
`TelecomManager.EXTRA_PHONE_ACCOUNT_HANDLE`; without it One UI can show a picker. Correct
the configured agent SIM label, restart the helper, and place a supervised test call.

**Prevent.** Record the agent SIM label, check it before dialing, label the SIM trays, and
alert when the reported label differs.

## 9. Call stuck, cannot hang up

**Symptom.** The call stays active, the agent has stopped, and the phone UI does not return
to idle.
**Impact.** The phone is busy and the next slot cannot run; S2, or S1 if persistent.
**Diagnose.**

```bash
curl -fsS --max-time 3 "http://$PHONE_HOST:8765/health"
read -rsp 'Helper token: ' HELPER_TOKEN; echo
curl -fsS --max-time 3 -H "Authorization: Bearer $HELPER_TOKEN" \
  "http://$PHONE_HOST:8765/state"
unset HELPER_TOKEN
journalctl --user -u telephone-agent-daemon.service -n 120 --no-pager | grep -iE 'hangup|end_call|max_duration'
./scripts/bluetooth-check.sh "$BT_ADDRESS" || true
```

Check whether `TelecomManager.endCall()` returned a permission error; it requires
privileged access.

**Fix.** End the call from the phone UI. If it is unresponsive, toggle airplane mode for
ten seconds and wait for the modem to register; restart the helper if hung, and reboot the
phone if the modem stays stuck. The SCO nodes disappear when the call ends.

**Prevent.** Enforce a hard maximum duration, implement best-effort end-call, and make the
agent release audio and mark the call failed even when the remote end is connected.

## 10. Agent stutters or latency exceeds 2 seconds

**Symptom.** Long pauses, cut-off TTS, or stage durations above the latency budget.
**Impact.** The conversation feels broken and callees may hang up; S2.
**Diagnose.**

```bash
ping -c 20 1.1.1.1
curl -o /dev/null -s -w 'dns=%{time_namelookup} connect=%{time_connect} tls=%{time_appconnect} first=%{time_starttransfer} total=%{time_total}\n' "https://$PROVIDER_HOST/"
top -b -n1 | head -n 15
iw dev wlan0 link 2>/dev/null || true
journalctl --user -u telephone-agent-daemon.service -n 200 --no-pager | grep -iE 'latency|stt|llm|tts|turn|timeout'
```

Compare with the latency budget in the [design brief](./design-brief.md); check loss,
signal, and host load. Confirm whether the call negotiated mSBC or CVSD.

**Fix.** Move the host and phone closer or change the Wi-Fi channel. Stop background
updates during call windows, switch to a lower-latency provider region, and reduce host
load. Restart the provider session, or the agent only for a supervised test call, if it is
stuck. Use Ethernet for the host where possible; Bluetooth remains the audio link.

**Prevent.** Track p95 stage latency, alert above 2 seconds, avoid concurrent work, and
keep the Bluetooth link strong.

## 11. Power outage recovery

**Symptom.** Mains power was lost; the host and router rebooted, and the phone may have
stayed up.
**Impact.** The current or next call may be missed; S1 if a slot is lost.
**Diagnose.**

```bash
ping -c 3 "$PHONE_HOST"
uptime
timedatectl
systemctl --user --failed
./scripts/bluetooth-check.sh --idle "$BT_ADDRESS"
curl -fsS --max-time 3 "http://$PHONE_HOST:8765/health"
```

**Fix.** Wait for the router and host to boot, then confirm time sync before any call.
Connect the Bluetooth link if it did not re-establish, confirm the phone is charging and
reachable, and skip a missed slot with an alert.

**Prevent.** Put the host and router on a UPS if a missed slot is unacceptable, boot the
router first, keep the timer enabled, and run the T-60 gate before every call.

## 12. Provider outage or expired API key

**Symptom.** STT, LLM, or TTS fails; logs show 401, 403, 429, or 5xx, or the health probe
fails.
**Impact.** The agent cannot hold a conversation; S1 without a fallback.
**Diagnose.**

```bash
curl -fsS -o /dev/null -w '%{http_code}\n' "$PROVIDER_HEALTH_URL"
journalctl --user -u telephone-agent-daemon.service -n 200 --no-pager | grep -iE '401|403|429|5[0-9][0-9]|auth|quota'
date -Is
getent hosts "$PROVIDER_HOST"
```

Check the status page, balance, key expiry, and clock. A skewed clock can look like an
auth failure.

**Fix.** Rotate the key in the user environment file, restart the agent only as part of a
supervised test call, and confirm the request. Switch to the configured secondary provider
if available. If neither works, skip the call and alert rather than retrying a failing
endpoint.

**Prevent.** Alert on balance and key expiry, configure two providers where possible, and
run the T-60 provider check.

## 13. Inbound calls not answered

**Symptom.** A call to the agent SIM rings but the agent never answers, or the daemon is
stopped.
**Impact.** Missed inbound calls; the phone may stay offhook and block the next call; S2.
**Diagnose.** Run the user-unit commands as the account that owns the PipeWire session:

```bash
systemctl --user status telephone-agent-daemon.service --no-pager
journalctl --user -u telephone-agent-daemon.service -n 120 --no-pager
read -rsp 'Helper token: ' HELPER_TOKEN; echo
curl -fsS --max-time 3 -H "Authorization: Bearer $HELPER_TOKEN" \
  "http://$PHONE_HOST:8765/state"
unset HELPER_TOKEN
```

Expect `active (running)`, no `daemon requires realtime.kind = 'grok'` error, and a
`{"state":"..."}` response. A call should move from `ringing` to `offhook` within about one
poll interval after the daemon answers. For a supervised check, call the agent SIM from
another handset and watch `/state` and the journal through answer, greeting, and hangup.
Confirm `[realtime].kind = "grok"` and that the service can resolve a credential
(`[realtime].api_key_command`, `XAI_API_KEY`, or `[realtime].api_key`).

**Fix.** Start the daemon with
`systemctl --user start telephone-agent-daemon.service`. Use `restart` only when no call is
active: a restart cancels the active call. If `/answer` returns `403`, grant
`ANSWER_PHONE_CALLS` and unlock the phone: `acceptRingingCall()` is best effort for a
non-default dialer. A `409` means the call stopped ringing or was answered manually. A
`500` means Android threw; check the helper log. If the daemon cannot see PipeWire, run it
in the PipeWire user session and enable lingering. If the state is `offhook` but the caller
hears silence, check the SCO nodes with `./scripts/bluetooth-check.sh "$BT_ADDRESS"` during
the call.

**Prevent.** Run one supervised inbound call after each phone or One UI update, keep the
daemon enabled with `Restart=always`, and alert when it is inactive or the helper has not
reached `idle` after a call.

## 14. Transcript retention and cleanup

Local transcripts are opt-in and contain full phone numbers and verbatim conversation
text. Keep a documented retention period, for example 30 days, and delete files on the
host rather than copying them into tickets, chat, or backups that do not cover call
content.

```bash
export TRANSCRIPT_DIR="${TRANSCRIPT_DIR:-$REPO/runtime/transcripts}"
ls -ld "$TRANSCRIPT_DIR"
find "$TRANSCRIPT_DIR" -maxdepth 1 -name '*.jsonl' -printf '%m %u:%g %p\n'
du -sh "$TRANSCRIPT_DIR"
find "$TRANSCRIPT_DIR" -maxdepth 1 -name '*.jsonl' -mtime +30 -print
```

The directory should be mode `700` and files mode `600`, owned by the service user. Review
the `find` output before deleting; then append `-delete` to the same command. Deletion from
a normal file system is not a secure erase, so do not use production conversations for
testing. If `[transcript].enabled` is false, neither the directory nor any files should
exist.

## After an incident

Record the times, slot, error, action, and test result. Update this runbook if a command
was wrong. Do not close an S1 until a real two-way call succeeds; review alerts for noise
that hid the signal.

## Related documents

[Operations](./operations.md), [troubleshooting](./troubleshooting.md),
[Bluetooth HFP](./bluetooth-hfp.md), [Inbound calls](./inbound.md),
[Android setup](./android-setup.md), the
[daemon user service](../scripts/systemd/telephone-agent-daemon.service), the
[outbound client service](../scripts/systemd/telephone-agent-bluetooth.service), and the
[Bluetooth timer](../scripts/systemd/telephone-agent-bluetooth.timer).
