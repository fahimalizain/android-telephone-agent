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
| `dial.sh` | Place a call through the Phone A helper, or check its health. | `./dial.sh +91XXXXXXXXXX` / `./dial.sh --health` |
| `systemd/uac-gadget.service` | Boot-time gadget creation and clean teardown. | Installed to `/etc/systemd/system/` by `install.sh`. |
| `systemd/telephone-agent.service` | One outbound call per activation (`Type=oneshot`). | Installed by `install.sh`; triggered by the timer, never restarted on exit. |
| `systemd/telephone-agent.timer` | Daily call schedule. | Edit `OnCalendar`, then `sudo systemctl enable --now telephone-agent.timer`. |

## Notes

- `uac-gadget.sh` and `install.sh` require root. The other scripts run as a normal
  user with access to the ALSA devices.
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
