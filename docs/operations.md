# Operations

This document covers running android-telephone-agent after setup: the operating model, daily
timeline, health checks, scheduling, alerting, logging, maintenance, and recovery. The reference
deployment is one Raspberry Pi Zero 2 W, one Android phone, one SIM, and one household. Confirm
names and paths against your installation; verify device-specific behavior on your device.

Related: [hardware](./hardware.md), [Raspberry Pi setup](./raspberry-pi-setup.md),
[Android setup](./android-setup.md), and
[telephone-agent.service](../scripts/systemd/telephone-agent.service).

## 1. Operating model

- **One call at a time.** The gadget has one playback and one capture stream, and Android routes
  one cellular call to it. Never start a second call or run a loopback test during a live call.
- **Scheduled daily.** The reference schedule places one call per day. Extra slots are allowed if
  the previous call has ended and the phone is idle.
- **Unattended.** The operator does not touch the Pi during a call. The callee answers an ordinary
  voice call. Intervention is limited to alerts and maintenance.
- **Single household.** One owner, one agent phone, one callee group. The design is not multi-tenant.

The Pi is the phone's headset, not a modem. The agent plays audio to the gadget, which the phone
treats as headset microphone input, and captures what the phone would send to the headset earpiece.
The runtime is a `Type=oneshot` service triggered by a daily systemd timer. Enable the hardware
watchdog so a hung kernel reboots the Pi; a watchdog reboot during a slot still means a missed call.
Do not add `Restart=always`: restarting a unit that dials would redial. See the
[design brief](./design-brief.md) for the architecture constraints.

For the Bluetooth HFP alternative, the audio path is the host's HFP SCO link with Phone A
rather than the USB gadget. Scheduling, the LAN helper, provider calls, alerting, and the
one-call-at-a-time rule are unchanged. The USB-specific checks below (gadget enumeration
and UAC card) do not apply; use the adapter roles, phone connection, and SCO nodes from
[Bluetooth HFP](./bluetooth-hfp.md) instead. The agent must run in the PipeWire user
session, so use the Bluetooth user units rather than the root USB unit.

## 2. Daily call timeline

| Time | Stage | Action | Failure handling |
|---|---|---|---|
| T-60 s | Health gate | Run every hard check in section 3 | Skip, record `call_skipped`, alert |
| T-0 | Slot claim | Claim the idempotency key | Treat as duplicate; do not dial |
| T-0 | Dial | Submit callee number and agent SIM to helper | Retry at most twice with backoff, inside the grace window |
| T-0 to timeout | Ringing | Wait for answer, default 45 s | End attempt, record `no_answer`, alert |
| Answer | Conversation | STT, LLM, TTS with barge-in and endpointing | Hang up on hard limit or prolonged silence |
| End | Hangup | Request call end, release audio devices | Ask the callee to hang up and wait for idle |
| End + 5 s | Post-call | Write call record and daily heartbeat | Alert on a missing record |

The T-60 gate exists because a call with a missing gadget or offline phone connects with no audio,
which is worse than a skipped call. A skipped call is visible and recoverable; a silent call wastes the
callee's time and may look like a nuisance call.

The conversation has a hard maximum duration, a silence timeout, and barge-in. Hangup is best effort:
`TelecomManager.endCall()` requires privileged access on modern Android, so the helper may be unable
to end the call. The agent then stops playback, releases the devices, and waits for the remote party
or network to end it. The next slot must not start until the call ends or the hard timeout expires.

## 3. Health checks

```bash
export PHONE_HOST="192.168.1.50"   # DHCP reservation; verify
export PROVIDER_HEALTH_URL="https://api.example.com/health"  # replace
```

| Check | Command | Pass | Class |
|---|---|---|---|
| Gadget enumerated | `test -n "$(cat /sys/kernel/config/usb_gadget/agent/UDC)" && echo ok` | `ok` | Hard |
| Gadget sound card | `grep -qi uac /proc/asound/cards && echo ok` | `ok` | Hard |
| Phone reachable | `curl -fsS --max-time 3 "http://$PHONE_HOST:8765/health"` | HTTP 200 | Hard |
| Time synchronized | `timedatectl show -p NTPSynchronized --value` | `yes` | Hard |
| Time zone | `timedatectl show -p Timezone --value` | `Asia/Kolkata` | Hard |
| Provider reachable | `curl -fsS --max-time 4 -o /dev/null -w '%{http_code}\n' "$PROVIDER_HEALTH_URL"` | 2xx | Hard |
| Disk | `df -P / \| awk 'NR==2 {print $5}'` | < 85% | Warn, hard at 95% |
| Temperature | `vcgencmd measure_temp` | < 80 C | Warn, hard at 85 C |
| Throttling | `vcgencmd get_throttled` | `0x0` | Warn; hard if active |

A hard failure aborts the call; a warning is logged and included in the heartbeat. The provider probe
may be an unauthenticated status endpoint or the agent's authenticated check; never put an API key on
a command line where local users can read it from the process list.

For `audio.backend = "pipewire"`, replace the two gadget checks with the Bluetooth HFP checks:
the adapter advertises Headset (0x1108) and Handsfree (0x111e), Phone A is connected, and
both SCO nodes exist during a call. `scripts/bluetooth-check.sh <mac>` covers all three;
see [Bluetooth HFP](./bluetooth-hfp.md). The helper, time, provider, disk, temperature,
and throttling checks are unchanged.

Time sync matters because the Pi Zero 2 W has no battery-backed clock. After a power cut, provider
request signing and TLS validation can fail until NTP succeeds. The reference helper's health response
reports liveness, device model, and SIM label; it is unauthenticated, so keep port 8765 on the LAN.

## 4. Scheduling

Use a systemd timer. It logs through journald, is inspectable with `systemctl list-timers`, can order
after network and gadget units, and controls catch-up explicitly. Cron has one-minute granularity and
inherits the system time zone; `CRON_TZ` support varies.

Set the system time zone to `Asia/Kolkata` and also state the zone in the timer. Verify with
`systemd-analyze calendar '*-*-* 08:00:00 Asia/Kolkata'` before enabling it.

The shipped units are [telephone-agent.service](../scripts/systemd/telephone-agent.service), a
`Type=oneshot` unit that places one call and exits, and
[telephone-agent.timer](../scripts/systemd/telephone-agent.timer), which triggers it. Scheduling stays
outside the runtime. Claim the slot before dialing so a timer retry cannot place a duplicate call.

```ini
# /etc/systemd/system/telephone-agent.timer (shipped example)
[Timer]
OnCalendar=*-*-* 08:00:00 Asia/Kolkata
AccuracySec=1s
Persistent=false
Unit=telephone-agent.service
[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now telephone-agent.timer
systemctl list-timers --all | grep telephone
```

India has no daylight saving time, but the code must still use IANA zones through `zoneinfo`, never a
fixed `+05:30` offset. Store slot identifiers with an explicit offset.

**Idempotency.** Derive a key per slot from the slot start and a hash of the phone and SIM identifiers.
Claim it in the state directory before dialing. A restart, overlapping timer, or operator retry must
reuse the key and observe that it is claimed; retries increment an attempt number under that key.

**Missed runs.** Set `Persistent=false` and enforce a grace window, for example 10 minutes. If the Pi,
network, or phone was unavailable, do not call when service returns outside the window. Record
`missed_slot` and alert. Quiet hours, for example 21:00 to 07:00 local, override any catch-up logic.

## 5. Alerting

Use ntfy or a Telegram bot. Both are outbound-only, need no inbound port, and reach a phone. Keep the
topic or bot token in a root-readable environment file, not the repository. Send a test alert at
installation and after credential rotation.

| Event | Severity |
|---|---|
| No call record within 2 minutes of the slot | Page |
| Answered call under 20 seconds | Page |
| One-way audio heuristic | Page |
| Gadget missing, or phone offline > 5 minutes in the call window | Page |
| Provider authentication or 5xx error at T-60 | Page |
| SIM out of credit or no cellular signal | Page |
| Two consecutive skipped or failed calls | Page |
| Disk > 85%, temperature > 80 C, active throttling | Warn |
| Daily heartbeat: call placed, duration, outcome | Info |

Do not alert on a single retry still in progress, a Wi-Fi blip under a minute, a normal completed call,
or a callee hanging up after a full conversation. Send one daily summary rather than every heartbeat.
Suppress duplicate alerts for the same incident key for six hours and send a recovery notice after the
next success. Alerts must not contain transcripts, audio, full phone numbers, tokens, or SIM identifiers.

One-way audio detection is heuristic: for example, agent speech was produced but no meaningful inbound
energy arrived for several seconds after the callee was expected to speak, or the reverse. Codec and
network behavior can cause false positives, so the alert asks for confirmation with a test call.

## 6. Logging

Write one JSON object per line to stdout and let journald collect it; do not write application logs to
the SD card. Each record carries `ts`, `level`, `event`, `call_id`, `slot_id`, `stage`, `duration_ms`,
`attempt`, `outcome`, `error_code`, and `phone_masked` (for example `+91******3210`, showing at most the
last four digits). No audio, transcripts, raw phone numbers, tokens, or SIM ICCIDs. If logs may be
attached to public issues, mask all but the last four digits and prefer omitting the number entirely.

```ini
# /etc/systemd/journald.conf.d/volatile.conf
[Journal]
Storage=volatile
RuntimeMaxUse=64M
RateLimitIntervalSec=30s
RateLimitBurst=1000
```

```bash
sudo systemctl restart systemd-journald
```

The trade-off is explicit: volatile logs disappear on reboot or power loss, when a post-mortem is most
useful. Ship the heartbeat and alert summaries off-box and keep a short call-outcome record in the state
directory. For a specific crash, enable persistent storage temporarily, reproduce, then revert.

## 7. SD card wear

The card is the most likely consumable. Reduce writes:

- Keep journald volatile (RAM-only), disable swap (`sudo systemctl disable --now dphys-swapfile`; confirm
  `swapon --show`), and use `noatime` on the root file system. If swap is unavoidable, use zram and
  `vm.swappiness=10`.
- Set `PYTHONDONTWRITEBYTECODE=1`, write state once per slot, never per audio frame, and never write
  audio to disk.
- Avoid persistent metrics databases and container images; use a reputable A1/A2 card and watch
  `dmesg` for `mmc` I/O errors.

Do not disable logging entirely; the goal is fewer writes, not blindness. Back up configuration, not
logs or audio.

## 8. Updates

Pin Raspberry Pi OS Lite 64-bit to a known release and record the kernel and firmware versions. Do not
run `rpi-update` on production. Use a manual monthly window:
`sudo apt update && sudo apt full-upgrade && sudo reboot`.

After any kernel or firmware update, re-test gadget enumeration, the ALSA card, the audio loopback, and
a real call. The `dwc2` controller and UAC gadget can change behavior across kernel versions; a
configuration that worked before an update is not guaranteed to work after it. Verify on your device.
Keep a known-good SD image; if an update breaks enumeration, swap in the previous card and investigate
offline. Apply security updates more frequently, but treat kernel and firmware updates as
maintenance-window work.

## 9. Security review

Run at installation, quarterly, and after suspected exposure.

| Item | Check | Expected |
|---|---|---|
| SSH keys only | `sudo sshd -T \| grep -E 'passwordauthentication\|permitrootlogin\|pubkeyauthentication'` | `no`, `no`, `yes` |
| Default passwords | `getent passwd pi`, `sudo passwd -S <user>` | No unused default; active accounts locked or strong |
| Listening ports | `sudo ss -tlnp` | SSH and agent control only |
| Firewall | `sudo ufw status verbose` or `sudo nft list ruleset` | Inbound denied except admin SSH |
| LAN only | Query helper from outside the LAN | No response; no port forward |
| Token permissions | `sudo stat -c '%a %U:%G' /etc/telephone-agent.env` | `640 root:<service-user>` |

Rotate the token with `openssl rand -hex 32`, update the phone app and Pi environment, restart both, and
confirm the old token is rejected. Rotate immediately if a laptop or backup containing it is lost. Keep
the helper on the LAN; the reference APK listens on all interfaces, so firewall port 8765 so only the Pi
can reach it. Bind it to the private interface where the platform allows. Do not port-forward or tunnel
it, and do not expose it over IPv6 without reviewing the firewall. Plain HTTP is acceptable only on a
trusted LAN; use a separate VLAN or TLS on a shared network.

## 10. Monthly maintenance

| Task | Method | Pass |
|---|---|---|
| Phone battery | Remove case, check for swelling or back-panel lift | No swelling |
| Cable and port | Inspect for fraying, lint, bent pins | Clean, firm, no play |
| Dust | Compressed air around vents and ports | Vents clear |
| Logs | Heartbeat history; `sudo journalctl -u telephone-agent.service -b --no-pager \| grep -iE 'error\|warn'` | No recurring errors |
| Disk | `df -h /`, `df -i /` | Below 80% |
| Provider balance | Provider console | Credit and expiry adequate |
| Test call | Documented test path | Two-way audio, clean hangup |

Record each result. A maintenance log turns "it worked last month" into evidence.

## 11. Capacity and scale

One Pi controls one phone and one call at a time: the gadget has a single audio function and the phone
routes one cellular call to it. Parallel calls on one gadget produce mixed or missing audio.

To call multiple numbers, use sequential slots with enough separation for idle, or one phone and one Pi
per number, each with its own SIM and caller ID. A PSTN or SIP gateway is outside scope. The Pi Zero 2 W
runs the audio bridge and a cloud-backed pipeline but not comfortable local speech models. A Pi 4 Model B
2 GB is the supported alternative for Ethernet and local experiments; the Pi 5 cannot act as a USB
device. Do not centralize audio from multiple households on one Pi; centralize only alerts and summaries.

## 12. Incident severity

| Severity | Definition | Examples | Target |
|---|---|---|---|
| S1 | No successful call for 3 consecutive days, or safety impact | Dead gadget, offline phone, provider outage, repeated silent calls | Acknowledge 4 h, restore 24 h |
| S2 | Degraded or repeated single-day failure | One-way audio, latency > 2 s, wrong SIM, call < 20 s | Acknowledge 24 h, fix 72 h |
| S3 | Cosmetic or non-urgent | Log formatting, docs, one transient warning | Next monthly window |

Use the [runbook](./runbook.md). For S1, make a manual call from an ordinary phone if the agent cannot
be restored quickly.

## 13. Backup and recovery

Back up configuration, not logs or audio: `/etc/telephone-agent.toml`, `/etc/telephone-agent.env` (encrypt the archive; it holds the helper token and API keys), systemd units, and OS, kernel, and firmware versions.

```bash
sudo tar -C / -czf /root/telephone-agent-config-$(date +%F).tar.gz \
  etc/telephone-agent.toml etc/telephone-agent.env \
  etc/systemd/system/telephone-agent.service \
  etc/systemd/system/telephone-agent.timer \
  etc/systemd/system/uac-gadget.service
```

Keep the archive off the Pi and out of git; it contains the token and possibly API keys. Take a full SD
image after a successful test call, label it with date and kernel, and store a spare card with the same
image. Confirm the card device with `lsblk` before reading or writing it.

Spares: one USB-C hub with PD passthrough, one USB-C to USB-C cable, one USB-A to micro-USB cable, one
25 W charger, and one prepared microSD card.

Recovery: power off; reflash or replace the card with the known-good image; restore configuration and token; boot
and wait for time sync; confirm gadget enumeration, the UAC card, and phone reachability; place a test
call with two-way audio; record the date and cause. With a prepared card this takes under 30 minutes.

## Related documents

[Hardware](./hardware.md), [Raspberry Pi setup](./raspberry-pi-setup.md),
[Bluetooth HFP](./bluetooth-hfp.md), [Android setup](./android-setup.md), [runbook](./runbook.md),
[troubleshooting](./troubleshooting.md), the
[service unit](../scripts/systemd/telephone-agent.service), and the
[timer](../scripts/systemd/telephone-agent.timer).
