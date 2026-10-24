# Troubleshooting

Setup-phase and early-operation problems, grouped by layer. Each entry gives the symptom, likely cause,
a check, and a fix. Run commands on the Pi unless stated otherwise, and verify device-specific behavior
on your device.

Related: [Raspberry Pi setup](./raspberry-pi-setup.md), [Android setup](./android-setup.md),
[hardware](./hardware.md), [operations](./operations.md), [runbook](./runbook.md), and the
[service unit](../scripts/systemd/telephone-agent.service).

For the Bluetooth HFP alternative, start with [Bluetooth HFP](./bluetooth-hfp.md). The
USB enumeration and ALSA sections below apply to the UAC1 gadget; Bluetooth failures are
diagnosed with `bluetoothctl`, `pw-dump`, and the WirePlumber journal instead.

Work from the bottom of the stack upward: power, USB enumeration, ALSA, Android routing, helper, network,
agent. A fault at a lower layer produces misleading errors at every layer above it. Change one thing at
a time and re-test with a loopback or a supervised call after each change.

## 1. Hardware and power

| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| Pi does not boot; no green activity | Insufficient power, bad card, broken boot file | Red power LED, green LED, console output, `vcgencmd get_throttled` at console | Use a 5 V/2 A supply on the PWR port, reseat or reflash the card, restore `config.txt` |
| `Under-voltage detected` in `dmesg` | Hub port or cable cannot supply peak current | `vcgencmd get_throttled` (bit 0), `dmesg \| grep -i voltage` | Power the Pi from PWR, shorten the cable, use a rated hub or supply |
| Hub passes power but no data | Charge-only hub or cable, or wrong hub port | Phone does not show the gadget; test a USB-C earphone through the hub | Replace with a data-capable hub, use the data port, try another cable |
| Phone says "USB device not supported" | Gadget descriptor or UAC profile not accepted, or power fault | `dmesg` on the Pi, phone notification, try UAC2 | Verify the gadget script and UAC1 profile; test UAC2; try another cable and port |

The Pi Zero 2 W draws roughly 150-300 mA typical and up to about 400 mA peak. A hub port that is fine
for a keyboard may still brown out during Wi-Fi transmission. If the Pi reboots when the radio is busy,
suspect power before software. The phone is a power sink and never sources VBUS to the gadget.

The phone expects a USB audio class device. A charge-only cable or a hub that does not pass data
downstream will never enumerate, no matter how the gadget is configured. Test the hub with a known USB
device before changing gadget settings.

## 2. USB enumeration

| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| `UDC` missing or empty | Gadget unit failed, `dwc2` overlay absent, configfs error | `ls /sys/kernel/config/usb_gadget/`, `cat .../agent/UDC`, `systemctl status uac-gadget.service` | Restart the unit; confirm `dtoverlay=dwc2,dr_mode=peripheral` and `modules-load=dwc2,libcomposite` |
| Phone sees a device but no audio card | UAC function not bound, descriptor rejected | `cat /proc/asound/cards`, `dmesg \| grep -iE 'uac\|g_audio\|configfs'` | Re-run the gadget script, check the function name, reboot and retest |
| UAC1 works on one phone, not another | Vendor UAC support differs | Test the same gadget on both phones; try UAC2 | Keep UAC1 as default; use UAC2 only if the phone enumerates and routes audio |
| ALSA card number changes between boots | Multiple sound cards or enumeration order | `arecord -l`, `aplay -l` | Address the card by name with `plughw:CARD=<name>,DEV=0`, never `hw:1,0` |

UAC1 is preferred because Android compatibility is broader. UAC2 is a fallback, not an upgrade: some
phones enumerate it but route call audio incorrectly or not at all. Verify both playback and capture on
your device before changing the default.

Card order is not stable when other USB audio devices, HDMI audio, or a codec HAT are present. A
configuration that uses `hw:1,0` can silently capture from the wrong device after a reboot. Use the
stable card name shown by `arecord -l` and confirm it after every kernel update.

## 3. ALSA

| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| `arecord: device busy` | Agent holds the device, or PulseAudio/PipeWire is using it | `sudo fuser -v /dev/snd/*`, `sudo lsof /dev/snd/*` | Stop the agent before testing; do not run two captures; disable audio servers on a headless image |
| Capture opens but contains silence | Wrong card or device, muted input, phone route wrong | `arecord -l`, `amixer -c <card> controls`, loopback test | Correct the device name, unmute if a control exists, reseat and retest |
| Playback runs but callee hears nothing | Playback sent to the wrong PCM, or phone did not route the call | `aplay -l`, loopback test, phone call route | Match the gadget playback device; confirm the phone uses USB audio for the call |
| Silence in one direction | Routing inversion, wrong PCM, or phone route | Loopback test, `arecord` level, phone call route | Correct the device, confirm the phone uses USB audio, and test each direction separately |
| Echo or feedback | Local monitoring loop, speaker coupling, or duplicate audio path | Check for `arecord \| aplay`, phone speaker volume, open streams | Remove local monitoring, use the headset route, lower gain, enable echo cancellation |

Remember the direction inversion: `aplay` to the gadget sends audio to the phone's microphone input, so
the remote party hears it. `arecord` from the gadget receives what the phone would play in its earpiece.
A test that assumes a normal sound card will diagnose the wrong direction.

`arecord: device busy` during setup usually means the agent is already running. Stop
`telephone-agent.service` before a manual loopback, and start it again afterward. If no agent is running,
find the holder with `fuser` or `lsof` before killing anything.

## 4. Android routing

| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| Call audio comes from the earpiece | Phone did not route to USB, or enumerated after the call | Phone call screen, `cat /proc/asound/cards`, Bluetooth state | Connect the gadget before dialing, disable Bluetooth for the USB path, confirm USB audio routing is enabled, reboot the phone |
| Samsung SIM picker appears | Helper did not pass the phone account handle, or no default SIM | SIM manager, helper logs, `/health` SIM label | Set the default calling SIM and pass `EXTRA_PHONE_ACCOUNT_HANDLE` |
| Helper notification is missing | Android 13+ notification permission denied | Settings > Apps > helper > Notifications | Grant notification permission; the foreground service needs a visible notification |
| Helper is killed after hours | Battery optimization or One UI sleeping apps | Settings > Battery > Background usage limits | Set Unrestricted, remove from sleeping lists, lock in recents |
| No cellular signal or SIM out of credit | Coverage, account balance, SIM disabled | Phone status bar, carrier balance or USSD, SIM manager | Move the phone near a window, top up, enable the SIM, retry a supervised call |

Developer options > Disable USB audio routing must be OFF. If it is ON, Android ignores the USB audio
device for calls even though the gadget enumerates. The setting is easy to toggle while debugging and
easy to forget.

On Samsung dual-SIM devices, the SIM picker is a routing failure, not a cosmetic prompt. An unattended
call cannot answer it. Pass the correct `TelecomManager` account handle and verify the caller ID on a
supervised test call. Behavior varies by One UI version; verify on your device.

## 5. Helper app

| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| `/health` unreachable | Helper killed, wrong IP, Wi-Fi down, firewall | `ping "$PHONE_HOST"`, `curl -v http://$PHONE_HOST:8765/health` | Restart the app, confirm the reserved address, check the phone's Wi-Fi |
| HTTP 401 on call control | Token mismatch after rotation | Agent log, `/etc/telephone-agent.env`, app settings | Update the token on both sides, restart the helper and agent |
| Port 8765 already in use | Old helper instance or another service | `ss -tlnp \| grep 8765` on a Linux helper host, or the app's diagnostics screen | Stop the old instance; or rebuild for another port and update the agent URL |
| Helper works, calls do not | `CALL_PHONE` denied, SIM account invalid | App permissions, SIM manager, helper logs | Grant the permission, correct the SIM label, retest |

The helper is a foreground service with a battery exemption. If the phone reports the app as inactive,
the endpoint will time out even though the phone is on the network. Treat a health failure as a helper
problem until ping and HTTP prove otherwise.

Port 8765 is the reference helper's fixed listening port. If another service uses it, stop the old
instance, or rebuild the helper on a free port and update the agent URL; the two must match.

## 6. Scheduling

| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| Call fires at the wrong hour | Wrong system time zone or a fixed offset in code | `timedatectl`, `systemd-analyze calendar '*-*-* 08:00:00 Asia/Kolkata'` | Set `Asia/Kolkata`, use IANA zones in code, reload the timer |
| Timer never fires | Unit disabled, bad `OnCalendar`, timer not started | `systemctl list-timers --all \| grep telephone`, `systemctl status <timer>` | Fix the expression, `daemon-reload`, enable and start the timer |
| Call fires late after boot | `Persistent=true` or catch-up logic | Timer unit, application state | Set `Persistent=false`, enforce a grace window and quiet hours |
| Clock is wrong after a power cut | NTP skew; no RTC | `timedatectl`, `timedatectl timesync-status` | Wait for sync before calls; enable a time-sync wait; check router NTP |

The Pi Zero 2 W has no battery-backed clock. A boot after a power cut can be minutes off until NTP
succeeds. Certificate validation and provider request signing then fail in ways that look like expired
keys or provider outages.

Do not compensate with a fixed `+05:30` offset. India has no daylight saving time today, but the code
must remain correct for other zones and for future rule changes. Use `zoneinfo` and store log timestamps
with an explicit offset.

## 7. Agent and network

| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| Wi-Fi drops during calls | Weak signal, 2.4 GHz interference, power save | `iw dev wlan0 link`, `ping -c 100 <gateway>`, `dmesg` | Move closer to the AP, change channel, disable power save, use Ethernet on a Pi 4 |
| Provider unreachable | DNS, egress firewall, wrong endpoint, expired key | `getent hosts`, `curl -v`, `date -Is` | Fix DNS or egress, correct the endpoint, rotate the key |
| Calls connect but the agent is slow | Provider latency, packet loss, CPU load | `top`, `ping`, journal stage durations | Reduce load, switch region, improve Wi-Fi, check cooling |
| Port 8765 in use on the Pi | Local control service conflict | `sudo ss -tlnp \| grep 8765` | Stop the conflicting process or change the port in both configs |

A 2.4 GHz-only Pi Zero 2 W is sensitive to microwave ovens, cordless phones, and crowded channels.
Packet loss shows up as STT retries and clipped TTS, not as an obvious network error. Measure before
changing provider settings.

If the clock is wrong, fix time before rotating keys. A valid key can fail with a signature error when
the local clock is outside the provider's tolerance. Confirm `timedatectl` shows synchronized and the
correct zone.

## 8. Audio quality

| Symptom | Likely cause | Check | Fix |
|---|---|---|---|
| Echo on the call | Acoustic coupling or a local playback-to-capture loop | Look for `arecord \| aplay`, check speaker volume and open streams | Remove monitoring, use the headset route, lower gain, enable echo cancellation |
| Clipping or distortion | Playback gain too high or capture overloaded | `arecord` a sample, inspect with `sox ... stat` if available | Reduce TTS or playback gain, add headroom, avoid boosting in ALSA |
| Dropouts and crackle | ALSA buffer underruns, CPU starvation, power | `dmesg \| grep -i xrun`, `top`, `vcgencmd get_throttled` | Increase buffers, stop background work, fix power or cooling |
| Volume too low | Phone in-call volume or gadget gain | Phone volume, `amixer` controls, test recording level | Raise the phone call volume, adjust gain, confirm the correct device |

The cellular codec caps fidelity regardless of the gadget's 48 kHz rate. Do not chase hi-fi; chase
intelligibility and stable turn-taking. A clean 16 kHz speech path sounds better on a call than a
clipped 48 kHz one.

Echo is usually acoustic or a monitoring loop, not a codec defect. Confirm that the Pi is not playing
its capture back to the gadget and that the phone is not using its speaker alongside the USB route.

## Collect diagnostics

Run this on the Pi and attach the output to an issue. It is designed to avoid tokens, audio, and full phone
numbers, but review it before sharing; do not add `env`, configuration files, or the environment file. Run
`sudo -v` first so a password prompt does not corrupt the capture.

```bash
#!/bin/sh
out="${HOME}/telephone-agent-diag-$(date +%Y%m%d-%H%M%S).txt"
{
  echo "== date =="; date -Is
  echo "== os =="; cat /etc/os-release; uname -a
  echo "== uptime =="; uptime
  echo "== power =="; vcgencmd get_throttled; vcgencmd measure_temp
  echo "== disk =="; df -h /; df -i /
  echo "== memory =="; free -h
  echo "== network =="; ip -brief addr; ip route
  echo "== wifi =="; iw dev wlan0 link 2>&1
  echo "== time =="; timedatectl
  echo "== failed units =="; systemctl --failed --no-pager
  echo "== agent =="; systemctl status telephone-agent.service --no-pager
  echo "== gadget unit =="; systemctl status uac-gadget.service --no-pager
  echo "== udc =="; cat /sys/kernel/config/usb_gadget/agent/UDC 2>&1
  echo "== cards =="; cat /proc/asound/cards
  echo "== pcm =="; aplay -l; arecord -l
  echo "== dmesg =="; dmesg | tail -n 120
  echo "== agent log =="; sudo journalctl -u telephone-agent.service -n 300 --no-pager
  echo "== gadget log =="; sudo journalctl -u uac-gadget.service -b -n 120 --no-pager
  echo "== timers =="; systemctl list-timers --all --no-pager
  echo "== ports =="; sudo ss -tlnp
  echo "== modules =="; lsmod | grep -E 'dwc2|libcomposite|u_audio|snd_usb_audio'
} >"$out" 2>&1
echo "Wrote $out"
```

Also record the phone model, One UI version, helper version, and whether the failure happens on every
call or only some calls. Those details are not available from the Pi because the phone is the USB host
and `adb` is unavailable over the gadget cable.

## Related documents

[Operations](./operations.md), [runbook](./runbook.md), [Raspberry Pi setup](./raspberry-pi-setup.md),
[Android setup](./android-setup.md), [Bluetooth HFP](./bluetooth-hfp.md), [hardware](./hardware.md), and the
[service unit](../scripts/systemd/telephone-agent.service).
