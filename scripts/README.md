# Scripts

Operational scripts for the Raspberry Pi gadget. Run them from a repository
checkout unless a path says otherwise. The setup guide is
`docs/raspberry-pi-setup.md`.

| File | Purpose | Usage |
|---|---|---|
| `uac-gadget.sh` | Create or remove the UAC1 gadget through configfs. | `sudo ./uac-gadget.sh start` / `sudo ./uac-gadget.sh stop` |
| `install.sh` | Install the gadget helper and systemd units; enable the gadget unit. The timer is installed but not enabled. | `sudo ./install.sh` |
| `install.sh --uninstall` | Disable and remove the installed helper and units; retain configuration. | `sudo ./install.sh --uninstall` |
| `audio-loopback-test.sh` | Test both audio directions while Phone A has an active call. | `./audio-loopback-test.sh list` |
| `audio-loopback-test.sh record` | Capture Phone A's earpiece / call downlink. | `./audio-loopback-test.sh record [seconds] [outfile]` |
| `audio-loopback-test.sh play` | Send a file to Phone A's microphone input / call uplink. | `./audio-loopback-test.sh play <file>` |
| `bluetooth-setup.sh` | Install or remove the per-user WirePlumber HFP Hands-Free config. The host is HF; Phone A is the AG. | `./bluetooth-setup.sh` / `./bluetooth-setup.sh --uninstall` |
| `bluetooth-check.sh` | Check HFP adapter roles, phone state, and active-call SCO nodes. | `./bluetooth-check.sh [--idle] <MAC>` |
| `bluetooth-audio-test.sh` | List, record, play, or inject a tone on the active-call SCO nodes. | `./bluetooth-audio-test.sh list` |
| `dial.sh` | Place a call through the Phone A helper, or check its health. | `./dial.sh +91XXXXXXXXXX` / `./dial.sh --health` |
| `systemd/uac-gadget.service` | Boot-time gadget creation and clean teardown. | Installed to `/etc/systemd/system/` by `install.sh`. |
| `systemd/telephone-agent.service` | One outbound call per activation (`Type=oneshot`). | Installed by `install.sh`; triggered by the timer, never restarted on exit. |
| `systemd/telephone-agent.timer` | Daily call schedule. | Edit `OnCalendar`, then `sudo systemctl enable --now telephone-agent.timer`. |
| `systemd/telephone-agent-bluetooth.service` | One outbound call for the Bluetooth backend, as a systemd **user** unit so it shares PipeWire. | Copy to `~/.config/systemd/user/`; see `docs/bluetooth-hfp.md`. |
| `systemd/telephone-agent-bluetooth.timer` | Daily call schedule for the Bluetooth user unit. | Copy to `~/.config/systemd/user/`, edit `OnCalendar`, then `systemctl --user enable --now telephone-agent-bluetooth.timer`. |

## Bluetooth HFP alternative

The canonical path remains the USB Audio Class gadget. As a supported
alternative, the Linux host can join Phone A over Bluetooth HFP as the
Hands-Free (HF) role while Phone A is the Audio Gateway (AG). This was
validated with BlueZ 5.85, PipeWire 1.6.2, WirePlumber 0.5.13, and a Samsung
Galaxy A23. HFP carries call audio only; dialing and hangup remain with the
existing LAN helper (`dial.sh`).

### Prerequisites

- A desktop PipeWire/WirePlumber session for the user who runs these scripts.
  Check it with `systemctl --user status pipewire wireplumber`.
- `bluetoothctl`, `pw-dump`, `pw-record`, `pw-play`, and `python3`. On
  Raspberry Pi OS or Debian, BlueZ and the PipeWire tools come from packages
  such as `bluez`, `pipewire-bin`, and `wireplumber`. Python 3 is standard.
- Phone A paired, trusted, connected, and allowed to use this host for calls.
  Pairing must be confirmed on the phone; the scripts do not pair automatically.
- No root for any Bluetooth script. WirePlumber configuration is per-user.

### Setup

```sh
./scripts/bluetooth-setup.sh
```

The script writes
`${XDG_CONFIG_HOME:-$HOME/.config}/wireplumber/wireplumber.conf.d/51-bt-headset.conf`,
backs up an existing file, and restarts the user WirePlumber service. It
refuses to run as root because it is a user-level configuration. `--uninstall`
removes only that file and leaves backups alone.

Pair Phone A next, using the phone's Bluetooth settings or `bluetoothctl`. Then
trust the host and enable "Calls and audio" for it on the phone:

```sh
bluetoothctl trust AA:BB:CC:DD:EE:FF
bluetoothctl connect AA:BB:CC:DD:EE:FF
```

`AA:BB:CC:DD:EE:FF` is a placeholder; use Phone A's address. After WirePlumber
restarts, the adapter should advertise Headset (UUID 0x1108) and Handsfree
(UUID 0x111e).

### Check

```sh
./scripts/bluetooth-check.sh AA:BB:CC:DD:EE:FF
./scripts/bluetooth-check.sh --idle AA:BB:CC:DD:EE:FF
BT_ADDRESS=AA:BB:CC:DD:EE:FF ./scripts/bluetooth-check.sh
```

The full check requires the adapter UUIDs, a connected phone, and both SCO
nodes. SCO nodes exist only while a call is active, so run it during a call.
`--idle` checks only the adapter roles and phone state. With no address, the
script lists connected devices and exits 2. A failed check exits 1; usage
errors exit 2.

### Audio test

```sh
./scripts/bluetooth-audio-test.sh list
./scripts/bluetooth-audio-test.sh record 10 /tmp/downlink.wav
./scripts/bluetooth-audio-test.sh play /tmp/downlink.wav
./scripts/bluetooth-audio-test.sh tone 2
```

`list` shows the discovered SCO nodes and exits 1 when no call is active.
`record` writes WAV (default 10 seconds and
`bluetooth-downlink-YYYYmmdd-HHMMSS.wav` in the current directory). `play`
sends a WAV to the SCO sink. `tone` injects a quiet 440 Hz tone for a safe
uplink test. Set `BT_ADDRESS` when more than one phone has SCO nodes.

On PipeWire 1.6.2, `pw-record --sample-count` returns status 1 after writing a
complete WAV. The script validates the WAV and keeps it, printing a note instead
of treating that known status as a failure. An empty or invalid file still
fails.

Direction reminder:

- `bluez_input.*` is the SCO source and carries the downlink. Recording it
  captures Phone A's earpiece, meaning the remote party's voice, not this
  host's microphone.
- `bluez_output.*` is the SCO sink and carries the uplink. Playing to it sends
  audio toward Phone A's microphone path, so the remote party hears it and this
  host does not.
- The `.0`/`.1` node suffix is a SPA object id and can change. The scripts
  discover nodes by `api.bluez5.address` and `factory.name`, not by suffix.

Make targets are available too:

```sh
make bluetooth-setup
make bluetooth-check BT_ADDRESS=AA:BB:CC:DD:EE:FF
make bluetooth-audio-test BT_AUDIO_TEST_ARGS="record 10"
```

### systemd user service

The Bluetooth backend must run in the same user session as PipeWire and
WirePlumber. The `systemd/telephone-agent-bluetooth.service` and
`systemd/telephone-agent-bluetooth.timer` files are user-unit examples. Copy
them to `~/.config/systemd/user/`, edit the repository paths and `OnCalendar`,
then enable the timer:

```sh
mkdir -p ~/.config/systemd/user
cp scripts/systemd/telephone-agent-bluetooth.service ~/.config/systemd/user/
cp scripts/systemd/telephone-agent-bluetooth.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now telephone-agent-bluetooth.timer
```

For a headless host with no logged-in desktop, enable the user manager at boot
and start the audio session for that user:

```sh
sudo loginctl enable-linger "$USER"
systemctl --user enable --now pipewire wireplumber
```

The Bluetooth unit is separate from the USB gadget unit. Do not enable both for
the same phone and call slot.

## Notes

- `uac-gadget.sh` and `install.sh` require root. The other scripts run as a normal
  user with access to the ALSA devices.
- The Bluetooth scripts are user-level and do not use sudo. `bluetooth-setup.sh`
  writes only the WirePlumber drop-in under the current user's config directory.
  HFP SCO nodes appear only while a call is active.
- `audio-loopback-test.sh` auto-detects the `UAC1Gadget` card. Override detection with
  `CARD=<id-or-number>`, for example
  `CARD=1 ./audio-loopback-test.sh record 10 /tmp/downlink.wav`.
- The loopback script uses `plughw:CARD=...,DEV=0` with 48 kHz, S16_LE, stereo.
- `dial.sh` reads `TELEPHONE_AGENT_DIALER_URL` and `TELEPHONE_AGENT_DIALER_TOKEN`, or
  `[dialer] url` and `[dialer] token` from the TOML file named by
  `TELEPHONE_AGENT_CONFIG`. The URL is the helper's base URL; the script posts to
  `/call` and checks `/health`. The double-underscore runtime spellings
  `TELEPHONE_AGENT_DIALER__URL` and `TELEPHONE_AGENT_DIALER__TOKEN` are also read.
- The units assume `/usr/local/sbin/uac-gadget.sh`, `/opt/telephone-agent`, and
  `/etc/telephone-agent.env`. If you install elsewhere, edit the unit paths.
- The shipped service passes `--config /etc/telephone-agent.toml`. Adjust the unit if
  your configuration lives elsewhere.
- `telephone-agent.service` ships with `User=pi`; change it to your account.
- `RuntimeWatchdogSec=15` is set in `/etc/systemd/system.conf`, not in a service unit.
