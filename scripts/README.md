# Scripts

Operational scripts for the Linux host and the Android helper. Run them from a repository
checkout unless a path says otherwise. The primary setup guide is
`docs/bluetooth-hfp.md`.

| File | Purpose | Usage |
|---|---|---|
| `bluetooth-setup.sh` | Install or remove the per-user WirePlumber HFP Hands-Free config. The host is HF; Phone A is the AG. | `./bluetooth-setup.sh` / `./bluetooth-setup.sh --uninstall` |
| `bluetooth-check.sh` | Check HFP adapter roles, phone state, and active-call SCO nodes. | `./bluetooth-check.sh [--idle] <MAC>` |
| `bluetooth-audio-test.sh` | List, record, play, or inject a tone on the active-call SCO nodes. | `./bluetooth-audio-test.sh list` |
| `dial.sh` | Place a call through the Phone A helper, or check its health. | `./dial.sh +91XXXXXXXXXX` / `./dial.sh --health` |
| `systemd/telephone-agent-bluetooth.service` | One outbound call for the Bluetooth backend, as a systemd **user** unit so it shares PipeWire. | Copy to `~/.config/systemd/user/`; see `docs/bluetooth-hfp.md`. |
| `systemd/telephone-agent-bluetooth.timer` | Daily call schedule for the Bluetooth user unit. | Copy to `~/.config/systemd/user/`, edit `OnCalendar`, then `systemctl --user enable --now telephone-agent-bluetooth.timer`. |
| `systemd/telephone-agent-inbound.service` | Long-lived inbound call watcher for the Bluetooth backend, as a systemd **user** unit sharing the user's PipeWire session. Uses the repository root `.venv`. | Copy to `~/.config/systemd/user/`, then `systemctl --user enable --now telephone-agent-inbound.service`; see `docs/inbound.md`. |

Bluetooth HFP is the only supported audio transport. The host joins Phone A as the
Hands-Free (HF) role while Phone A is the Audio Gateway (AG). This was validated with
BlueZ 5.85, PipeWire 1.6.2, WirePlumber 0.5.13, and a Samsung Galaxy A23. HFP carries
call audio only; dialing and hangup remain with the LAN helper (`dial.sh`).

## Prerequisites

- A Linux host with a desktop or user PipeWire/WirePlumber session for the user who runs
  these scripts. Check it with `systemctl --user status pipewire wireplumber`.
- `bluetoothctl`, `pw-dump`, `pw-record`, `pw-play`, and `python3`. On Debian and Ubuntu,
  BlueZ and the PipeWire tools come from packages such as `bluez`, `pipewire-bin`, and
  `wireplumber`. Python 3 is standard.
- Phone A paired, trusted, connected, and allowed to use this host for calls. Pairing must
  be confirmed on the phone; the scripts do not pair automatically.
- No root for any Bluetooth script. WirePlumber configuration is per-user.

## Setup

```sh
./scripts/bluetooth-setup.sh
```

The script writes
`${XDG_CONFIG_HOME:-$HOME/.config}/wireplumber/wireplumber.conf.d/51-bt-headset.conf`,
backs up an existing file, and restarts the user WirePlumber service. It refuses to run as
root because it is a user-level configuration. `--uninstall` removes only that file and
leaves backups alone.

Pair Phone A next, using the phone's Bluetooth settings or `bluetoothctl`. Then trust the
host and enable **Calls and audio** for it on the phone:

```sh
bluetoothctl trust AA:BB:CC:DD:EE:FF
bluetoothctl connect AA:BB:CC:DD:EE:FF
```

`AA:BB:CC:DD:EE:FF` is a placeholder; use Phone A's address. After WirePlumber restarts,
the adapter should advertise Headset (UUID 0x1108) and Handsfree (UUID 0x111e).

## Check

```sh
./scripts/bluetooth-check.sh AA:BB:CC:DD:EE:FF
./scripts/bluetooth-check.sh --idle AA:BB:CC:DD:EE:FF
BT_ADDRESS=AA:BB:CC:DD:EE:FF ./scripts/bluetooth-check.sh
```

The full check requires the adapter UUIDs, a connected phone, and both SCO nodes. SCO
nodes exist only while a call is active, so run it during a call. `--idle` checks only the
adapter roles and phone state. With no address, the script lists connected devices and
exits 2. A failed check exits 1; usage errors exit 2.

## Audio test

```sh
./scripts/bluetooth-audio-test.sh list
./scripts/bluetooth-audio-test.sh record 10 /tmp/downlink.wav
./scripts/bluetooth-audio-test.sh play /tmp/downlink.wav
./scripts/bluetooth-audio-test.sh tone 2
```

`list` shows the discovered SCO nodes and exits 1 when no call is active. `record` writes
WAV (default 10 seconds and `bluetooth-downlink-YYYYmmdd-HHMMSS.wav` in the current
directory). `play` sends a WAV to the SCO sink. `tone` injects a quiet 440 Hz tone for a
safe uplink test. Set `BT_ADDRESS` when more than one phone has SCO nodes.

On PipeWire 1.6.2, `pw-record --sample-count` returns status 1 after writing a complete
WAV. The script validates the WAV and keeps it, printing a note instead of treating that
known status as a failure. An empty or invalid file still fails.

Direction reminder:

- `bluez_input.*` is the SCO source and carries the downlink. Recording it captures Phone
  A's earpiece, meaning the remote party's voice, not this host's microphone.
- `bluez_output.*` is the SCO sink and carries the uplink. Playing to it sends audio
  toward Phone A's microphone path, so the remote party hears it and this host does not.
- The `.0`/`.1` node suffix is a SPA object id and can change. The scripts discover nodes
  by `api.bluez5.address` and `factory.name`, not by suffix.

Make targets are available too:

```sh
make bluetooth-setup
make bluetooth-check BT_ADDRESS=AA:BB:CC:DD:EE:FF
make bluetooth-audio-test BT_AUDIO_TEST_ARGS="record 10"
```

## systemd user services

The Bluetooth backend must run in the same user session as PipeWire and WirePlumber. The
`systemd/telephone-agent-bluetooth.service` and
`systemd/telephone-agent-bluetooth.timer` files are user-unit examples. Copy them to
`~/.config/systemd/user/`, edit the repository paths and `OnCalendar`, then enable the
timer:

```sh
mkdir -p ~/.config/systemd/user
cp scripts/systemd/telephone-agent-bluetooth.service ~/.config/systemd/user/
cp scripts/systemd/telephone-agent-bluetooth.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now telephone-agent-bluetooth.timer
```

For a headless host with no logged-in desktop, enable the user manager at boot and start
the audio session for that user:

```sh
sudo loginctl enable-linger "$USER"
systemctl --user enable --now pipewire wireplumber
```

The Bluetooth unit is the only outbound scheduler. Do not enable it together with the
inbound watcher for the same phone and call slot.

### Inbound watcher user service

`systemd/telephone-agent-inbound.service` runs the long-lived `inbound` command for the
Bluetooth backend. It is also a systemd **user** unit, so it shares the user's
PipeWire/WirePlumber session and can see the SCO nodes after a call is answered. It uses
the repository root `.venv/bin/telephone-agent`, while the Bluetooth outbound unit uses
the `agent/.venv` path. Make sure the unit you install points at a virtual environment
that exists on your host.

```sh
mkdir -p ~/.config/systemd/user
install -m644 scripts/systemd/telephone-agent-inbound.service \
  ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now telephone-agent-inbound.service
journalctl --user -u telephone-agent-inbound.service -n 100 --no-pager
```

Enable lingering as above on a headless host. Inbound and outbound calls are mutually
exclusive on one phone: do not enable this unit together with the Bluetooth timer or a
`run` timer. The watcher answers every ringing call on the agent SIM, with no caller ID.
Read `docs/inbound.md` before enabling it.

## Notes

- The Bluetooth scripts are user-level and do not use sudo. `bluetooth-setup.sh` writes
  only the WirePlumber drop-in under the current user's config directory. HFP SCO nodes
  appear only while a call is active.
- `dial.sh` reads `TELEPHONE_AGENT_DIALER_URL` and `TELEPHONE_AGENT_DIALER_TOKEN`, or
  `[dialer] url` and `[dialer] token` from the TOML file named by
  `TELEPHONE_AGENT_CONFIG`. The URL is the helper's base URL; the script posts to
  `/call` and checks `/health`. The double-underscore runtime spellings
  `TELEPHONE_AGENT_DIALER__URL` and `TELEPHONE_AGENT_DIALER__TOKEN` are also read.
- The outbound user unit assumes the repository is at `~/android-telephone-agent` and
  uses `agent/.venv`. The inbound unit uses the repository root `.venv`. Edit the
  `%h`-based paths and the console-script path if your layout differs.
- The shipped Bluetooth service passes `--config ~/android-telephone-agent/config/config.toml`.
  Adjust the unit if your configuration lives elsewhere.
- `RuntimeWatchdogSec=15` belongs in `/etc/systemd/system.conf`, not in a user service
  unit, and is optional on a desktop.

## Android helper build and provisioning

The Gradle project lives in `android-helper/`. These host-side scripts build the APK,
install it over adb, and exercise the LAN endpoint. They do not modify the Android sources
and never print the bearer token.

| File | Purpose | Usage |
|---|---|---|
| `build-helper.sh` | Build and verify the helper APK with the Gradle wrapper. | `./scripts/build-helper.sh [--debug or --release] [--clean]` |
| `install-helper.sh` | Install the newest APK, grant permissions, whitelist device idle, and start `DialService`. | `./scripts/install-helper.sh [--apk PATH] [--serial SERIAL] [--no-start] [--health URL]` |
| `helper-smoke-test.sh` | Check `/health` and reject an invalid token; optionally call or hang up. | `./scripts/helper-smoke-test.sh [--url URL] [--token-file PATH] [--call +E164] [--hangup]` |

### Build

```sh
./scripts/build-helper.sh
./scripts/build-helper.sh --debug --clean
./scripts/build-helper.sh --release
```

By default, the script builds the debug APK and also builds release when
`android-helper/keystore.properties` exists. `--release` requires that file and errors
without it. The script uses `JAVA_HOME`, `ANDROID_HOME`, and `GRADLE_USER_HOME` when they
are set, otherwise `/usr/lib/jvm/java-21-openjdk-amd64`, `/data/Android`, and
`/scratch/gradle`. Each APK's path, size, SHA-256, `apksigner` verification, and `aapt2`
badging summary are printed.

### Install

```sh
./scripts/install-helper.sh
./scripts/install-helper.sh --apk /path/to/app-debug.apk
./scripts/install-helper.sh --serial R58M123456A --no-start
./scripts/install-helper.sh --health http://192.168.1.50:8765
```

Without `--apk`, the newest release or debug APK is installed. The script requires
exactly one authorized adb device unless `--serial` is given. Use a USB cable or wireless
debugging; see `docs/android-setup.md`. It then grants `CALL_PHONE`, `READ_PHONE_STATE`,
`ANSWER_PHONE_CALLS`, and `POST_NOTIFICATIONS`, adds `dev.example.dialhelper` to the
device-idle whitelist, and starts `dev.example.dialhelper/.DialService`. `--no-start` skips
the service start. The script prints the launcher command and the phone's `wlan0`
address; permission grants and the whitelist are best effort.

### Smoke test

```sh
./scripts/helper-smoke-test.sh --url http://192.168.1.50:8765
./scripts/helper-smoke-test.sh --url http://192.168.1.50:8765 \
  --token-file /path/to/token
./scripts/helper-smoke-test.sh --url http://192.168.1.50:8765 \
  --token-file /path/to/token --call +91XXXXXXXXXX
./scripts/helper-smoke-test.sh --url http://192.168.1.50:8765 \
  --token-file /path/to/token --hangup
```

The URL comes from `--url` or `TELEPHONE_AGENT_DIALER_URL`. The token comes from
`--token-file`, `TELEPHONE_AGENT_DIALER_TOKEN`, or `TELEPHONE_AGENT_DIALER__TOKEN`, in
that order; it is never printed. The default run is safe: it checks `/health` for
`"status":"ok"` and a `"device"` field, then posts an invalid token to `/call` and expects
HTTP 401. No real call is placed unless `--call` is supplied. `--call` and `--hangup`
require a token. The script uses curl with a timeout and exits non-zero if any check fails.
