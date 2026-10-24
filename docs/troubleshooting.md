# Troubleshooting

Setup-phase and early-operation problems, grouped by layer. Each entry gives the symptom,
likely cause, a check, and a fix. Run commands on the Linux host unless stated otherwise,
and verify device-specific behavior on your device.

Related: [Bluetooth HFP](./bluetooth-hfp.md), [Android setup](./android-setup.md),
[Inbound calls](./inbound.md), [operations](./operations.md), [runbook](./runbook.md), the
[Bluetooth user service](../scripts/systemd/telephone-agent-bluetooth.service), and the
[inbound user service](../scripts/systemd/telephone-agent-inbound.service).

For inbound-answer failures, including `/answer` 403/409/500, ringing with no SCO nodes,
and the inbound service not starting, see the table in
[Inbound calls](./inbound.md#limits-and-troubleshooting) and the inbound rows in
[Bluetooth HFP](./bluetooth-hfp.md#11-troubleshooting).

Work from the bottom of the stack upward: power, Bluetooth adapter, HFP link and SCO
nodes, PipeWire, Android routing, helper, network, agent. A fault at a lower layer produces
misleading errors at every layer above it. Change one thing at a time and re-test with the
Bluetooth check, an audio test, or a supervised call after each change.

## 1. Hardware and power

| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| Host does not boot | Power, disk, or boot configuration | Power LED, console output, `df -h /`, `journalctl -b -p err` | Restore power, free disk space, or restore the host from backup |
| Bluetooth adapter missing | Adapter disabled, unplugged, or driver not loaded | `bluetoothctl list`, `dmesg \| grep -i blue`, `rfkill list` | Enable the adapter, unblock rfkill, or use another adapter |
| Phone battery falls over days | Charger, cable, or port problem | Phone battery screen, `adb shell dumpsys battery`, inspect the port | Replace the cable or charger, clean the port, remove the case |
| Phone shuts down overnight | Battery health or charging failure | Battery health screen, charging state over a day | Replace the battery or phone; do not rely on a failing device |

The phone is a power sink and charges from its own USB charger. Bluetooth is the audio
link, so the USB port is not occupied by an audio device. Use a charger and cable that the phone
accepts; a charge-only cable is fine.

## 2. Bluetooth link

| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| `bluetoothctl show` has no Headset or Handsfree UUID | WirePlumber fragment not loaded, wrong path, or overridden | `systemctl --user status wireplumber`, `journalctl --user -u wireplumber -b` | Install the fragment with `scripts/bluetooth-setup.sh`, restart WirePlumber, re-check |
| Phone pairs but does not connect | Device not trusted, out of range, or adapter reset | `bluetoothctl info <address>`, `bluetoothctl connect <address>` | Trust the device, move closer, reconnect, and verify before the next slot |
| Phone connects but **Calls and audio** is off | HFP call-audio profile not enabled | Phone Bluetooth device details | Enable **Calls and audio**, toggle Bluetooth, or re-pair |
| Call audio stays on the earpiece | Phone selected the wrong route, or HFP is not connected | Phone call screen, `bluetoothctl info <address>`, `pw-dump` | Select Bluetooth on the call screen, reconnect, retry the call |
| SCO nodes missing during a call | Call is not routed to Bluetooth, HFP link is down, or agent is in another user session | `scripts/bluetooth-check.sh <address>`, `pw-dump` | Fix the route or pairing; run the agent in the PipeWire user session |
| Codec is CVSD, not mSBC | Phone or adapter did not negotiate wideband | `scripts/bluetooth-check.sh <address>` during a call, `wpctl inspect <node-id>` | No fix if unsupported; the call still works at 8 kHz |
| Link drops during setup | Weak signal, 2.4 GHz interference, or adapter reset | RSSI in `bluetoothctl info`, `iw dev wlan0 link`, distance | Move the phone and host closer, change the Wi-Fi channel, reconnect manually |
| Pairing lost after phone reboot | Device not trusted, or the phone did not reconnect | `bluetoothctl info <address>` | Trust the device and connect once before the next scheduled call |

SCO nodes exist only while a call is active; their absence while idle is normal. A stale
bond can keep the old profile set after the WirePlumber fragment changes. Remove the pairing
on both sides and pair again.

## 3. PipeWire

| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| `pw-record` or `pw-play` not found | PipeWire tools not installed | `command -v pw-record pw-play pw-dump` | Install the distribution's PipeWire tools package (for example `pipewire-bin`) |
| `pw-dump` cannot connect | Agent runs as root or another user; no user session | `id`, `systemctl --user status pipewire`, `echo "$XDG_RUNTIME_DIR"` | Run as the PipeWire user, use a user service, enable lingering |
| Node target not found after reconnect | The `.<id>` suffix changed | `pw-dump`, `wpctl status` | Discover by `api.bluez5.address` and `factory.name`; do not hard-code `.0` or `.1` |
| One direction has no node | Incomplete SCO set, or the phone route changed | `scripts/bluetooth-audio-test.sh list` | Reconnect, confirm the call route, and retry during the call |
| `pw-record` exits nonzero after `--sample-count` | Known PipeWire 1.6.2 behavior when the WAV is complete | Validate the WAV, check its size and duration | Keep the valid file; the audio test script already handles this |
| Stream plays to the host speaker instead of the call | Wrong target node or missing explicit target | `media.class` and node name in `pw-dump` | Target the `bluez_output` SCO sink for uplink; do not use the default sink |

PipeWire and WirePlumber run per user. A system service, a different account, or a root
shell does not share that session and cannot see the SCO nodes.

## 4. Android routing

| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| Call audio comes from the earpiece | Phone did not route to Bluetooth, or HFP is not connected | Phone call screen, `bluetoothctl info <address>`, `dumpsys audio` | Confirm **Calls and audio**, reconnect, reboot the phone if the route is stuck |
| Samsung SIM picker appears | Helper did not pass the phone account handle, or no default SIM | SIM manager, helper logs, `/health` SIM label | Set the default calling SIM and pass `EXTRA_PHONE_ACCOUNT_HANDLE` |
| Helper notification is missing | Android 13+ notification permission denied | Settings > Apps > helper > Notifications | Grant notification permission; the foreground service needs a visible notification |
| Helper is killed after hours | Battery optimization or One UI sleeping apps | Settings > Battery > Background usage limits | Set Unrestricted, remove from sleeping lists, lock in recents |
| No cellular signal or SIM out of credit | Coverage, account balance, SIM disabled | Phone status bar, carrier balance or USSD, SIM manager | Move the phone near a window, top up, enable the SIM, retry a supervised call |
| Phone rings but no SCO audio | SCO nodes appear only after answer, or the route stayed local | `GET /state`, phone call screen, `pw-dump` | Answer through `POST /answer`, confirm **Calls and audio**, check during the call |

On Samsung dual-SIM devices, the SIM picker is a routing failure, not a cosmetic prompt. An
unattended call cannot answer it. Pass the correct `TelecomManager` account handle and
verify the caller ID on a supervised test call. Behavior varies by One UI version; verify
on your device.

## 5. Helper app

| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| `/health` unreachable | Helper killed, wrong IP, Wi-Fi down, firewall | `ping "$PHONE_HOST"`, `curl -v http://$PHONE_HOST:8765/health` | Restart the app, confirm the reserved address, check the phone's Wi-Fi |
| HTTP 401 on call control | Token mismatch after rotation | Agent log, host environment file, app settings | Update the token on both sides, restart the helper and agent |
| Port 8765 already in use | Old helper instance or another service | `ss -tlnp \| grep 8765` on a Linux helper host, or the app's diagnostics screen | Stop the old instance; or rebuild for another port and update the agent URL |
| Helper works, calls do not | `CALL_PHONE` denied, SIM account invalid | App permissions, SIM manager, helper logs | Grant the permission, correct the SIM label, retest |
| `/call` returns `409` | A call is ringing or active, inbound or outbound | `GET /state`, phone UI | End the call or wait for idle; inbound and outbound are serialized |
| `/answer` returns `403` | `ANSWER_PHONE_CALLS` missing or Android denied the answer | Helper log, app permissions | Grant the permission, unlock the phone, retry a supervised call |
| `/answer` returns `409` | The call stopped ringing or was answered manually | `GET /state` | Expected race; do not retry |
| `/answer` returns `500` | Android threw while answering | Helper log, `adb logcat -s DialHelper` | Unlock the phone, check vendor restrictions |

The helper is a foreground service with a battery exemption. If the phone reports the app
as inactive, the endpoint will time out even though the phone is on the network. Treat a
health failure as a helper problem until ping and HTTP prove otherwise.

Port 8765 is the reference helper's fixed listening port. If another service uses it, stop
the old instance, or rebuild the helper on a free port and update the agent URL; the two
must match.

## 6. Scheduling

| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| Call fires at the wrong hour | Wrong system time zone or a fixed offset in code | `timedatectl`, `systemd-analyze calendar '*-*-* 08:00:00 Asia/Kolkata'` | Set `Asia/Kolkata`, use IANA zones in code, reload the timer |
| Timer never fires | Unit disabled, bad `OnCalendar`, timer not started, or user manager stopped | `systemctl --user list-timers --all \| grep telephone`, `systemctl --user status <timer>` | Fix the expression, `systemctl --user daemon-reload`, enable and start the timer |
| Timer fires only while logged in | Lingering not enabled for the user | `loginctl show-user "$USER" \| grep Linger` | `sudo loginctl enable-linger "$USER"` |
| Call fires late after boot | `Persistent=true` or catch-up logic | Timer unit, application state | Set `Persistent=false`, enforce a grace window and quiet hours |
| Clock is wrong after a power cut | NTP skew; host has no RTC | `timedatectl`, `timedatectl timesync-status` | Wait for sync before calls; enable a time-sync wait; check router NTP |

A host without a battery-backed clock, or one that has just booted after a power cut, can be
minutes off until NTP succeeds. Certificate validation and provider request signing then
fail in ways that look like expired keys or provider outages.

Do not compensate with a fixed `+05:30` offset. India has no daylight saving time today,
but the code must remain correct for other zones and for future rule changes. Use
`zoneinfo` and store log timestamps with an explicit offset.

## 7. Agent and network

| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| Wi-Fi drops during calls | Weak signal, 2.4 GHz interference, power save | `iw dev wlan0 link`, `ping -c 100 <gateway>`, `dmesg` | Move closer to the AP, change channel, disable power save, use Ethernet for the host |
| Provider unreachable | DNS, egress firewall, wrong endpoint, expired key | `getent hosts`, `curl -v`, `date -Is` | Fix DNS or egress, correct the endpoint, rotate the key |
| Calls connect but the agent is slow | Provider latency, packet loss, CPU load, Bluetooth retransmission | `top`, `ping`, journal stage durations, `scripts/bluetooth-check.sh` | Reduce load, switch region, improve Wi-Fi or Bluetooth proximity, check host cooling |
| Port 8765 in use on the host | Local control service conflict | `sudo ss -tlnp \| grep 8765` | Stop the conflicting process or change the port in both configs |
| Agent exits at startup | Configuration error, missing token, or missing audio tools | `telephone-agent doctor`, journal | Fix the named key, token, or package |
| Agent cannot see SCO nodes | Running outside the PipeWire user session | `systemctl --user status pipewire wireplumber`, `echo "$XDG_RUNTIME_DIR"` | Use the user service and enable lingering |

Wi-Fi and Bluetooth share the 2.4 GHz band. Packet loss or SCO retransmission shows up as
STT retries, clipped TTS, or dropouts, not as an obvious network error. Measure before
changing provider settings.

If the clock is wrong, fix time before rotating keys. A valid key can fail with a signature
error when the local clock is outside the provider's tolerance. Confirm `timedatectl` shows
synchronized and the correct zone.

## 8. Audio quality

| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| Echo on the call | Far-end speakerphone or a network echo path | Ask the far end to use a handset, check the phone route and open streams | Use a handset or headset at the far end; keep the barge-in guard |
| Clipping or distortion | Playback gain too high or capture overloaded | Record the downlink, inspect RMS/dBFS with `selftest` or `sox ... stat` | Reduce TTS or playback gain, add headroom |
| Dropouts and crackle | Bluetooth retransmission, Wi-Fi coexistence, CPU starvation | `pw-top`, `iw dev wlan0 link`, `top` | Move closer, change channel, stop background work |
| Volume too low | Phone in-call volume or SCO gain | Phone call volume, `wpctl` volume for the SCO node | Raise the phone call volume, adjust gain |
| One direction silent | Wrong node mapping or phone route | `scripts/bluetooth-audio-test.sh record` and `tone` | Fix the route or target; downlink is `bluez_input`, uplink is `bluez_output` |

The cellular codec caps fidelity regardless of the SCO rate. Do not chase hi-fi; chase
intelligibility and stable turn-taking. A clean 16 kHz speech path sounds better on a call
than a clipped or delayed one.

Echo is usually acoustic at the far end or a network reflection. Confirm that the host is
not playing its capture back to the SCO sink and that the phone is not using its speaker
alongside the Bluetooth route.

## Collect diagnostics

Run this on the host and attach the output to an issue. It is designed to avoid tokens,
audio, and full phone numbers, but review it before sharing; do not add `env`, configuration
files, or the environment file.

```bash
#!/bin/sh
out="${HOME}/telephone-agent-diag-$(date +%Y%m%d-%H%M%S).txt"
{
  echo "== date =="; date -Is
  echo "== os =="; cat /etc/os-release; uname -a
  echo "== uptime =="; uptime
  echo "== disk =="; df -h /; df -i /
  echo "== memory =="; free -h
  echo "== network =="; ip -brief addr; ip route
  echo "== wifi =="; iw dev wlan0 link 2>&1
  echo "== time =="; timedatectl
  echo "== failed units =="; systemctl --user --failed --no-pager
  echo "== bluetooth =="; bluetoothctl show; bluetoothctl info "${BT_ADDRESS}"
  echo "== hfp =="; ./scripts/bluetooth-check.sh --idle "${BT_ADDRESS}" 2>&1
  echo "== sco =="; pw-dump 2>&1 | grep -E 'bluez_(input|output)\.|api\.bluez5\.sco' | head -n 40
  echo "== pipewire =="; systemctl --user status pipewire wireplumber --no-pager
  echo "== agent =="; systemctl --user status telephone-agent-bluetooth.service --no-pager
  echo "== agent log =="; journalctl --user -u telephone-agent-bluetooth.service -n 300 --no-pager
  echo "== inbound =="; systemctl --user status telephone-agent-inbound.service --no-pager
  echo "== timers =="; systemctl --user list-timers --all --no-pager
  echo "== ports =="; sudo ss -tlnp
} >"$out" 2>&1
echo "Wrote $out"
```

Also record the phone model, One UI version, helper version, Bluetooth adapter model, and
whether the failure happens on every call or only some calls. If adb is available, collect
`adb logcat -s DialHelper` separately, with numbers and tokens removed.

## Related documents

[Operations](./operations.md), [runbook](./runbook.md),
[Bluetooth HFP](./bluetooth-hfp.md), [Inbound calls](./inbound.md),
[Android setup](./android-setup.md), the
[Bluetooth user service](../scripts/systemd/telephone-agent-bluetooth.service), and the
[inbound user service](../scripts/systemd/telephone-agent-inbound.service).
