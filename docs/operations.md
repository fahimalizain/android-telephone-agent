# Operations

This document covers running android-telephone-agent after setup: the operating model,
daily timeline, health checks, scheduling, alerting, logging, maintenance, and recovery.
The reference deployment is one Linux host, one Android phone, one SIM, and one household.
Confirm names and paths against your installation; verify device-specific behavior on your
device.

Related: [Bluetooth HFP](./bluetooth-hfp.md), [Android setup](./android-setup.md),
[Inbound calls](./inbound.md), the
[daemon user service](../scripts/systemd/telephone-agent-daemon.service), the
[outbound client service](../scripts/systemd/telephone-agent-bluetooth.service), and the
[Bluetooth timer](../scripts/systemd/telephone-agent-bluetooth.timer).

## 1. Operating model

- **One call at a time.** HFP exposes one SCO source and one SCO sink, and Android routes
  one cellular call to them. Never start a second call or run an audio test during a live
  call.
- **Scheduled daily.** The reference schedule places one call per day. Extra slots are
  allowed if the previous call has ended and the phone is idle.
- **Unattended.** The operator does not touch the host during a call. The callee answers
  an ordinary voice call. Intervention is limited to alerts and maintenance.
- **Single household.** One owner, one agent phone, one callee group. The design is not
  multi-tenant.

The host is the phone's Bluetooth headset, not a modem. The agent plays audio to the SCO
sink, which Phone A treats as headset microphone input, and captures the SCO source, which
is what Phone A would send to the headset earpiece. The runtime is one long-lived systemd
**user** service, `telephone-agent-daemon.service`. It owns the audio path, serves
outbound calls from `run` over a local Unix socket, and runs the inbound watcher in the
same process. A user timer triggers a `Type=oneshot` client service,
`telephone-agent-bluetooth.service`, that submits one call to the daemon and waits. Do not
add `Restart=always` to the client: restarting it would submit another call. The daemon
itself uses `Restart=always` and re-arms safely because it serves calls rather than dialing
at startup. The daemon requires `[realtime].kind = "grok"` because it always runs the
inbound watcher; the text pipeline has no inbound audio path. `serve` (MCP/A2A) is a
separate control plane that also owns the audio path; do not run it at the same time as
the daemon.

The agent must run in the same user session as PipeWire and WirePlumber. A system service
running as another user has a different session bus and `XDG_RUNTIME_DIR` and cannot see
the SCO nodes. Enable lingering on a headless host. See the
[design brief](./design-brief.md) for the architecture constraints.

## 2. Daily call timeline

| Time | Stage | Action | Failure handling |
|---|---|---|---|
| T-60 s | Health gate | Run every hard check in section 3 | Skip, record `call_skipped`, alert |
| T-0 | Slot claim | Claim the idempotency key | Treat as duplicate; do not dial |
| T-0 | Dial | Submit callee number and agent SIM to helper | Retry at most twice with backoff, inside the grace window |
| T-0 to timeout | Ringing | Wait for answer, default 45 s | End attempt, record `no_answer`, alert |
| Answer | Conversation | STT, LLM, TTS with barge-in and endpointing | Hang up on hard limit or prolonged silence |
| End | Hangup | Request call end, release audio streams | Ask the callee to hang up and wait for idle |
| End + 5 s | Post-call | Write call record and daily heartbeat | Alert on a missing record |

The T-60 gate exists because a call with a missing Bluetooth link or an offline phone
connects with no audio, which is worse than a skipped call. A skipped call is visible and
recoverable; a silent call wastes the callee's time and may look like a nuisance call.

The conversation has a hard maximum duration, a silence timeout, and barge-in. Hangup is
best effort: `TelecomManager.endCall()` requires privileged access on modern Android, so
the helper may be unable to end the call. The agent then stops playback, releases the
streams, and waits for the remote party or network to end it. The next slot must not start
until the call ends or the hard timeout expires.

## 3. Health checks

```bash
export PHONE_HOST="192.168.1.50"   # DHCP reservation; verify
export BT_ADDRESS="AA:BB:CC:DD:EE:FF"  # Phone A; verify
export PROVIDER_HEALTH_URL="https://api.example.com/health"  # replace
```

| Check | Command | Pass | Class |
|---|---|---|---|
| Bluetooth adapter and phone | `./scripts/bluetooth-check.sh --idle "$BT_ADDRESS"` | exit 0 | Hard |
| SCO nodes during a call | `./scripts/bluetooth-check.sh "$BT_ADDRESS"` | exit 0 | Hard |
| Phone reachable | `curl -fsS --max-time 3 "http://$PHONE_HOST:8765/health"` | HTTP 200 | Hard |
| Daemon active | `systemctl --user is-active telephone-agent-daemon.service` | `active` | Hard |
| Outbound timer active | `systemctl --user is-active telephone-agent-bluetooth.timer` | `active` when scheduled calls are enabled | Warn |
| Time synchronized | `timedatectl show -p NTPSynchronized --value` | `yes` | Hard |
| Time zone | `timedatectl show -p Timezone --value` | `Asia/Kolkata` | Hard |
| Provider reachable | `curl -fsS --max-time 4 -o /dev/null -w '%{http_code}\n' "$PROVIDER_HEALTH_URL"` | 2xx | Hard |
| Disk | `df -P / \| awk 'NR==2 {print $5}'` | < 85% | Warn, hard at 95% |

A hard failure aborts the call; a warning is logged and included in the heartbeat. The
provider probe may be an unauthenticated status endpoint or the agent's authenticated
check; never put an API key on a command line where local users can read it from the
process list.

The idle check covers the adapter roles, the bond, and the phone connection. The full
check adds the two SCO nodes and can only pass during an active call. `scripts/bluetooth-check.sh`
covers all of these; see [Bluetooth HFP](./bluetooth-hfp.md). Do not treat an idle check
as proof that call audio will work.

Time sync matters because a host without a battery-backed clock, or one that has just
booted after a power cut, can have the wrong time until NTP succeeds. Certificate
validation and provider request signing then fail in ways that look like expired keys or
provider outages. The helper's health response reports liveness, device model, and SIM
label; it is unauthenticated, so keep port 8765 on the LAN.

## 4. Scheduling

Use a systemd **user** timer. It logs through journald, is inspectable with
`systemctl --user list-timers`, can order after PipeWire and WirePlumber, and controls
catch-up explicitly. Cron has one-minute granularity and inherits the system time zone;
`CRON_TZ` support varies.

Set the system time zone to `Asia/Kolkata` and also state the zone in the timer. Verify
with `systemd-analyze calendar '*-*-* 08:00:00 Asia/Kolkata'` before enabling it.

The shipped units are
[telephone-agent-daemon.service](../scripts/systemd/telephone-agent-daemon.service), the
long-lived daemon that owns calls and audio,
[telephone-agent-bluetooth.service](../scripts/systemd/telephone-agent-bluetooth.service),
a `Type=oneshot` client that submits one call to the daemon and waits, and
[telephone-agent-bluetooth.timer](../scripts/systemd/telephone-agent-bluetooth.timer),
which triggers the client. Scheduling stays outside the runtime. Claim the slot before
dialing so a timer retry cannot place a duplicate call.

```ini
# ~/.config/systemd/user/telephone-agent-bluetooth.timer (shipped example)
[Timer]
OnCalendar=*-*-* 08:00:00 Asia/Kolkata
AccuracySec=1s
Persistent=false
Unit=telephone-agent-bluetooth.service
[Install]
WantedBy=timers.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now telephone-agent-bluetooth.timer
systemctl --user list-timers --all | grep telephone
```

India has no daylight saving time, but the code must still use IANA zones through
`zoneinfo`, never a fixed `+05:30` offset. Store slot identifiers with an explicit offset.

**Idempotency.** Derive a key per slot from the slot start and a hash of the phone and SIM
identifiers. Claim it in the state directory before dialing. A restart, overlapping timer,
or operator retry must reuse the key and observe that it is claimed; retries increment an
attempt number under that key.

**Missed runs.** Set `Persistent=false` and enforce a grace window, for example 10 minutes.
If the host, network, or phone was unavailable, do not call when service returns outside
the window. Record `missed_slot` and alert. Quiet hours, for example 21:00 to 07:00 local,
override any catch-up logic.

**Inbound and outbound.** The daemon serializes inbound and outbound calls on the one
phone call slot, so the outbound timer can stay enabled alongside inbound answering. If a
call is ringing or active at the slot, the helper rejects `/call` with `409` and the `run`
client request is rejected as busy; record the slot as skipped instead of retrying into
the active call. Do not start `serve` or `run --direct` while the daemon is active,
because each would open a second audio owner.

## 5. Alerting

Use ntfy or a Telegram bot. Both are outbound-only, need no inbound port, and reach a
phone. Keep the topic or bot token in a user-readable environment file, not the
repository. Send a test alert at installation and after credential rotation.

| Event | Severity |
|---|---|
| No call record within 2 minutes of the slot | Page |
| Answered call under 20 seconds | Page |
| One-way audio heuristic | Page |
| Bluetooth adapter missing, phone disconnected > 5 minutes in the call window, or SCO nodes missing during a call | Page |
| Daemon inactive when calls are expected | Page |
| Provider authentication or 5xx error at T-60 | Page |
| SIM out of credit or no cellular signal | Page |
| Two consecutive skipped or failed calls | Page |
| Disk > 85%, host temperature high, or storage errors | Warn |
| Daily heartbeat: call placed, duration, outcome | Info |

Do not alert on a single retry still in progress, a Wi-Fi blip under a minute, a normal
completed call, or a callee hanging up after a full conversation. Send one daily summary
rather than every heartbeat. Suppress duplicate alerts for the same incident key for six
hours and send a recovery notice after the next success. Alerts must not contain
transcripts, audio, full phone numbers, tokens, or SIM identifiers.

One-way audio detection is heuristic: for example, agent speech was produced but no
meaningful inbound energy arrived for several seconds after the callee was expected to
speak, or the reverse. Codec and network behavior can cause false positives, so the alert
asks for confirmation with a test call.

## 6. Logging

Write one JSON object per line to stdout and let journald collect it. Each record carries
`ts`, `level`, `event`, `call_id`, `slot_id`, `stage`, `duration_ms`, `attempt`,
`outcome`, `error_code`, and `phone_masked` (for example `+91******3210`, showing at most
the last four digits). No audio, transcripts, raw phone numbers, tokens, or SIM ICCIDs. If
logs may be attached to public issues, mask all but the last four digits and prefer
omitting the number entirely.

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

Volatile storage is optional. On a desktop it reduces disk writes; on any host it means
logs disappear on reboot or power loss, when a post-mortem is most useful. Ship the
heartbeat and alert summaries off-box and keep a short call-outcome record in the state
directory. For a specific crash, enable persistent storage temporarily, reproduce, then
revert.

### Local call transcripts

The agent can write a separate transcript for each call without putting transcripts in
journald. Set `[transcript].enabled = true` in the agent configuration; the code default
is off, and when off neither the directory nor a file is created.
`[transcript].directory` defaults to `runtime/transcripts` relative to the process working
directory.

Each call gets one append-only, UTF-8 JSONL file named
`YYYYmmddTHHMMSS-<short-id>.jsonl`. The directory is created with mode `0700` and files
are mode `0600`. Every line has `ts` (ISO 8601 local wall-clock time with milliseconds),
`call_id`, and `event`; current events are `call_start` (direction, full number, objective
when non-empty, audio_backend, realtime_kind, and realtime model/voice for Grok),
`transcript` (role `callee`, verbatim text), `reply` (role `agent`, verbatim text), and
`call_end` (state, reason, turns, duration_s). Audio is never written.

Unlike operational logs, the local transcript is intentionally complete and **not
redacted**: it contains the full phone number, full conversation text, and call metadata.
Operational stdout and journald logging remain masked. Treat transcript files as personal
data. Keep them on the local device, readable only by the service user, and apply a
retention or deletion policy appropriate to the deployment. They are excluded from git by
the `runtime/` ignore rule, but do not include them in off-box backups unless the backup
and retention policy explicitly covers call content. An I/O failure disables transcript
writes for that call and logs a warning; it does not interrupt the call.

## 7. Disk and host maintenance

Keep enough free space for the OS, the runtime, and the journal. Reduce writes where it
matters:

- Keep journald volatile if the host uses a small SSD or SD card; otherwise a normal
  persistent journal is fine.
- Set `PYTHONDONTWRITEBYTECODE=1`, write state once per slot, never per audio frame, and
  never write audio to disk.
- Avoid persistent metrics databases and container images on a small host. Watch `dmesg`
  for storage or I/O errors.

Do not disable logging entirely; the goal is fewer writes, not blindness. Back up
configuration, not logs or audio.

## 8. Updates

Keep the Linux distribution, BlueZ, PipeWire, and WirePlumber on a known release. Apply
security updates on a regular schedule and treat kernel, BlueZ, and PipeWire updates as
maintenance-window work:

```bash
sudo apt update && sudo apt full-upgrade
sudo reboot
```

After any Bluetooth, PipeWire, WirePlumber, or kernel update, re-test adapter roles, phone
pairing, both SCO nodes during a call, and one real call. Role names and configuration
syntax can change; a configuration that worked before an update is not guaranteed to work
after it. Verify on your device.

Keep the previous known-good package versions or a host snapshot where possible. If an
update breaks the SCO link, roll back and investigate offline.

## 9. Security review

Run at installation, quarterly, and after suspected exposure.

| Item | Check | Expected |
|---|---|---|
| SSH keys only | `sudo sshd -T \| grep -E 'passwordauthentication\|permitrootlogin\|pubkeyauthentication'` | `no`, `no`, `yes` |
| Default passwords | `getent passwd`, `sudo passwd -S <user>` | No unused default; active accounts locked or strong |
| Listening ports | `sudo ss -tlnp` | SSH and agent control only |
| Firewall | `sudo ufw status verbose` or `sudo nft list ruleset` | Inbound denied except admin SSH |
| LAN only | Query helper from outside the LAN | No response; no port forward |
| Token permissions | `stat -c '%a %U:%G' <env-file>` | `600` or `640`, owner-only |
| User services | `systemctl --user list-unit-files \| grep telephone` | Only the intended daemon, outbound client, and timer enabled |

Rotate the token with `openssl rand -hex 32`, update the phone app and host environment,
restart both, and confirm the old token is rejected. Rotate immediately if a laptop or
backup containing it is lost. Keep the helper on the LAN; the reference APK listens on all
interfaces, so firewall port 8765 so only the host can reach it. Bind it to the private
interface where the platform allows. Do not port-forward or tunnel it, and do not expose
it over IPv6 without reviewing the firewall. Plain HTTP is acceptable only on a trusted
LAN; use a separate VLAN or TLS on a shared network.

## 10. Monthly maintenance

| Task | Method | Pass |
|---|---|---|
| Phone battery | Remove case, check for swelling or back-panel lift | No swelling |
| Charging cable and port | Inspect for fraying, lint, bent pins | Clean, firm, no play |
| Bluetooth link | `./scripts/bluetooth-check.sh --idle "$BT_ADDRESS"` | Both roles and phone connected |
| Dust | Compressed air around host vents and phone port | Vents clear |
| Logs | `journalctl --user -u telephone-agent-daemon.service -b --no-pager \| grep -iE 'error\|warn'` | No recurring errors |
| Daemon service | `systemctl --user status telephone-agent-daemon.service` | Active |
| Disk | `df -h /`, `df -i /` | Below 80% |
| Provider balance | Provider console | Credit and expiry adequate |
| Test call | Documented test path | Two-way audio, clean hangup |

Record each result. A maintenance log turns "it worked last month" into evidence.

## 11. Capacity and scale

One host controls one phone and one call at a time: the HFP link is point-to-point and the
phone routes one cellular call to it. Parallel calls on one phone produce mixed or missing
audio.

To call multiple numbers, use sequential slots with enough separation for idle, or one
phone and one host per number, each with its own SIM and caller ID. A PSTN or SIP gateway
is outside scope. The host runs the audio bridge and a cloud-backed pipeline. Local speech
models need more CPU and RAM than the reference laptop and remain a stretch goal. Do not
centralize audio from multiple households on one host; centralize only alerts and
summaries.

## 12. Incident severity

| Severity | Definition | Examples | Target |
|---|---|---|---|
| S1 | No successful call for 3 consecutive days, or safety impact | Dead Bluetooth adapter, offline phone, provider outage, repeated silent calls | Acknowledge 4 h, restore 24 h |
| S2 | Degraded or repeated single-day failure | One-way audio, latency > 2 s, wrong SIM, call < 20 s | Acknowledge 24 h, fix 72 h |
| S3 | Cosmetic or non-urgent | Log formatting, docs, one transient warning | Next monthly window |

Use the [runbook](./runbook.md). For S1, make a manual call from an ordinary phone if the
agent cannot be restored quickly.

## 13. Backup and recovery

Back up configuration, not logs or audio: the agent TOML, the environment file (encrypt the
archive; it holds the helper token and API keys), the systemd user units, and the installed
BlueZ, PipeWire, and WirePlumber versions.

```bash
tar -C "$HOME" -czf "$HOME/telephone-agent-config-$(date +%F).tar.gz" \
  android-telephone-agent/config/config.toml \
  android-telephone-agent/.env \
  .config/systemd/user/telephone-agent-daemon.service \
  .config/systemd/user/telephone-agent-bluetooth.service \
  .config/systemd/user/telephone-agent-bluetooth.timer \
  .config/wireplumber/wireplumber.conf.d/51-bt-headset.conf
```

Keep the archive off the host and out of git; it contains the token and possibly API keys.
Also record the Bluetooth address, the phone model, and the adapter model so a replacement
host can be paired.

Spares: one phone charger and USB-C cable, and a second phone if the agent SIM can be moved
quickly. A spare host is optional but reduces recovery time.

Recovery: restore the configuration and token; install BlueZ, PipeWire, and WirePlumber;
install the WirePlumber fragment; pair and trust the phone; enable the daemon and the
outbound timer; wait for time sync; run the idle Bluetooth check, then place a supervised
test call with two-way audio. Record the date and cause.

## Related documents

[Bluetooth HFP](./bluetooth-hfp.md), [Android setup](./android-setup.md),
[Inbound calls](./inbound.md), [runbook](./runbook.md),
[troubleshooting](./troubleshooting.md), the
[daemon user service](../scripts/systemd/telephone-agent-daemon.service), the
[outbound client service](../scripts/systemd/telephone-agent-bluetooth.service), and the
[Bluetooth timer](../scripts/systemd/telephone-agent-bluetooth.timer).
