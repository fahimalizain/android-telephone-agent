# Raspberry Pi Setup

This guide prepares a Raspberry Pi Zero 2 W as the USB audio gadget and runtime host
for android-telephone-agent: operating system, USB device mode, the UAC1 gadget, the
ALSA loopback test, the reference runtime, networking, and basic hardening. It targets
Raspberry Pi OS Lite 64-bit (Bookworm or later). Where behavior depends on the kernel,
the Android build, or the hub, verify it on your device.

## Purpose and prerequisites

When complete, the Pi enumerates on Phone A as a USB audio headset, exposes a 48 kHz,
16-bit, stereo UAC1 path in both directions, runs the reference agent as a systemd
service, and starts the gadget at boot. This guide does not configure Phone A, the
dial helper, or speech providers; see `docs/android-setup.md`, `docs/dial-helper.md`,
and `docs/voice-agent.md`.

| Item | Requirement |
|---|---|
| Board | Raspberry Pi Zero 2 W (quad-core Cortex-A53, 512 MB RAM, 2.4 GHz Wi-Fi). |
| Alternative | Raspberry Pi 4 Model B 2 GB. Power it through the GPIO header so the USB-C port stays available for device mode. The Pi 5 cannot act as a USB device. |
| Storage | 32 GB microSD, A1 or A2. |
| Hub | USB-C hub with PD passthrough and at least one USB-A data port. |
| Charger | 25 W USB-C PD charger. |
| Cables | USB-C to USB-C, 60 W, charger to hub; USB-A to micro-USB, hub to Pi. Short and strain-relieved. |
| Workstation | Computer with Raspberry Pi Imager, an SSH client, and file-copy tools. |
| Phone A | The Android phone that holds the agent SIM. |
| Network | 2.4 GHz Wi-Fi with DHCP; a DHCP reservation for the Pi is recommended. |

## 1. Flash Raspberry Pi OS Lite

In Raspberry Pi Imager, select **Raspberry Pi OS Lite (64-bit)**, Bookworm or later.
Before writing, open the customization dialog and set:

- hostname `telephone-pi` (or another name you will remember);
- a non-root user. The examples use `pi` as a placeholder; replace it with yours;
- a 2.4 GHz Wi-Fi SSID, password, and the correct country;
- locale and time zone `Asia/Kolkata`, and SSH enabled with your public key or a
  strong password.

Write the image and boot the Pi. The Zero 2 W has a 2.4 GHz radio only; it cannot see a
5 GHz-only SSID. Connect with `ssh <user>@telephone-pi.local`, or use the address from
your router if mDNS is unavailable.

## 2. First boot

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y alsa-utils curl python3-venv
sudo reboot
```

After reboot, confirm Bookworm or later with `cat /etc/os-release`, and Python 3.11 or
later with `python3 --version`.

## 3. Enable USB device mode

On Bookworm and later the boot files are under `/boot/firmware/`; older releases used
`/boot/config.txt` and `/boot/cmdline.txt`. Add the overlay under `[all]` in
`/boot/firmware/config.txt`:

```ini
[all]
dtoverlay=dwc2,dr_mode=peripheral
```

If a `dtoverlay=dwc2` line already exists, replace it rather than adding a second.
Conflicting overlays can leave the controller in host mode.

`/boot/firmware/cmdline.txt` must remain a single line. Append the modules to that
same line; do not insert a newline:

```bash
sudo cp /boot/firmware/cmdline.txt /boot/firmware/cmdline.txt.bak
if ! grep -q 'modules-load=dwc2,libcomposite' /boot/firmware/cmdline.txt; then
  sudo sed -i '1s/$/ modules-load=dwc2,libcomposite/' /boot/firmware/cmdline.txt
fi
cat /boot/firmware/cmdline.txt
wc -l /boot/firmware/cmdline.txt
```

There must be one command line. A single trailing newline is normal; a second line is
not. Reboot, then verify with `lsmod | grep -E 'dwc2|libcomposite'` and
`ls /sys/class/udc`.

`/sys/class/udc` should contain the controller, for example a name ending in `.usb`.
An empty directory means the Pi is not in peripheral mode, the overlay did not apply,
or the cable is in the power-only port. The controller name varies by board and
kernel; verify it on your device.

## 4. Install the gadget

From a repository checkout on the Pi:

```bash
sudo install -d /usr/local/sbin
sudo install -m 0755 scripts/uac-gadget.sh /usr/local/sbin/uac-gadget.sh
sudo install -m 0644 scripts/systemd/uac-gadget.service /etc/systemd/system/uac-gadget.service
sudo systemctl daemon-reload
sudo systemctl enable --now uac-gadget.service
systemctl status uac-gadget.service --no-pager
journalctl -u uac-gadget.service -b --no-pager
```

`scripts/install.sh` copies the files and enables the gadget unit without starting it.
Use either the commands above or the installer, not both. `uac-gadget.sh start`
creates the gadget if absent and does nothing if it is already bound; `stop` unbinds
the UDC before removing configuration objects.

## 5. Gadget attributes

The gadget is built through configfs at `/sys/kernel/config/usb_gadget/agent`.

| Attribute | Value | Meaning |
|---|---|---|
| `idVendor` | `0x1d6b` | Linux Foundation development vendor ID. |
| `idProduct` | `0x0104` | Development product ID for this gadget. |
| `bcdUSB` | `0x0200` | USB 2.0 descriptors; UAC1 remains compatible with USB 1.1 and 2.0 hosts. |
| `bcdDevice` | `0x0100` | Device release number; informational. |
| `strings/0x409/serialnumber` | `agent-01` | Serial string. Keep it unique if another gadget is present. |
| `strings/0x409/manufacturer` | `android-telephone-agent` | Manufacturer string shown by the host. |
| `strings/0x409/product` | `Telephone Agent UAC1` | Product string; Android may use it in the USB notification. |
| `configs/c.1/strings/0x409/configuration` | `UAC1 audio` | Configuration string. |
| `functions/uac1.0` | directory | One instance of `f_uac1`. The `.0` suffix is an instance name, not a version. |
| `p_srate` / `c_srate` | `48000` | Declared sample rates for playback and capture. The phone may resample to its cellular codec. |
| `p_chmask` / `c_chmask` | `0x3` | Channel bitmask; bits 0 and 1 select left and right, giving stereo. |
| `p_ssize` / `c_ssize` | `2` | Sample size in bytes; 2 means 16-bit samples. |
| `configs/c.1/MaxPower` | `250` | Power request; the descriptor reports 500 mA (2 mA units). |
| `configs/c.1/uac1.0` | symlink | Links the function into configuration `c.1`; unlinked functions are not exposed. |
| `UDC` | controller name | Writing the name binds the gadget and creates the ALSA card; empty unbinds it. |

The `p_*` and `c_*` prefixes are host-perspective names: playback is the host's
playback direction and capture is the host's capture direction. They do not match the
Pi's local `aplay` and `arecord` names on this gadget. Use the next section as the
mapping and verify on your device.

## 6. The ALSA inversion

The mapping is inverted relative to a normal USB sound card, and it is the most common
source of one-way audio. Read this before connecting the agent.

| Operation on the Pi | ALSA device | Reaches on Phone A | Result |
|---|---|---|---|
| `aplay file.wav` | `plughw:CARD=UAC1Gadget,DEV=0` | Microphone input, call uplink | The remote party or IVR hears the file; nothing plays on the Pi. |
| `arecord out.wav` | `plughw:CARD=UAC1Gadget,DEV=0` | Earpiece / call downlink | The Pi records the remote party; this is not the Pi's microphone. |

In short, `aplay` is the agent's mouth and `arecord` is its ear; a normal sound card
would reverse those roles, which is why intuition fails here.

```bash
# Far end hears this through Phone A's microphone input.
aplay -D plughw:CARD=UAC1Gadget,DEV=0 message.wav
# Record what Phone A would play in its earpiece.
arecord -D plughw:CARD=UAC1Gadget,DEV=0 -f S16_LE -r 48000 -c 2 -d 10 downlink.wav
```

`plughw` converts when necessary. Keep the gadget at 48 kHz, 16-bit, stereo;
resampling twice adds latency and can introduce artifacts. If your directions differ,
stop and correct the runtime mapping rather than changing the USB descriptors blindly.

## 7. Verification

With the Pi connected to Phone A through the hub and the gadget active:

```bash
ls /sys/class/udc
aplay -l
arecord -l
sudo dmesg | tail -n 30
cat /sys/kernel/config/usb_gadget/agent/UDC
systemctl status uac-gadget.service --no-pager
```

Expect `/sys/class/udc` to contain the controller; `aplay -l` and `arecord -l` to list
`UAC1Gadget`, device 0 (ALSA assigns the card number); `dmesg` to show gadget binding
and USB enumeration; Phone A to show a USB audio or headset notification; and `UDC`
to contain the controller name. Wording and kernel messages vary; verify on your
device. If the card is missing, fix the gadget before installing the agent. See
section 12.

## 8. Loopback test

`scripts/audio-loopback-test.sh` proves both directions before agent code is involved.
It detects the card by searching `arecord -l` and `aplay -l` for `UAC1Gadget`; set
`CARD=` to override detection.

1. Call voicemail, an IVR, or a second phone from Phone A. Keep the call connected and
   confirm its audio is routed to USB; speakerphone can override USB routing.
2. List devices: `./scripts/audio-loopback-test.sh list`.
3. Record with `./scripts/audio-loopback-test.sh record 15 /tmp/downlink.wav`. This
   captures the greeting, IVR prompts, or the other person, not your voice near Phone
   A. Copy the file to a workstation to listen:
   `scp <user>@telephone-pi.local:/tmp/downlink.wav .`.
4. Send audio the other way: `./scripts/audio-loopback-test.sh play /tmp/downlink.wav`.
   The far end hears it, not the Pi. With voicemail, retrieve the message; with a
   second phone, the person can confirm. Playing the downlink back is a true loopback:
   the far end hears its own delayed voice. Keep the clip short.

| Symptom | Likely cause | Check |
|---|---|---|
| `record` is silent | Call audio not routed to USB, wrong card, or downlink muted. | Check the phone notification, raise in-call volume, run `arecord -l`, try `CARD=`. |
| `play` is inaudible at the far end | Uplink not routed to USB, phone microphone muted, or direction applied backwards. | Re-read the table, test with a second phone, confirm `aplay` exited successfully. |
| Both directions are silent | Gadget unbound, service inactive, or phone did not enumerate. | `systemctl status uac-gadget`; `cat /sys/kernel/config/usb_gadget/agent/UDC`; `aplay -l`. |
| Recording is noise or hum | Cable or hub problem, very low volume, or a cellular codec issue. | Try a short direct cable; confirm the call is audible on another phone; check the hub. |

Do not connect the agent until both directions behave as described.

## 9. Reference runtime

The unit assumes the runtime is at `/opt/telephone-agent`. The repository may live
elsewhere; copying it prevents a later checkout move from breaking the service.

```bash
AGENT_USER="${SUDO_USER:-$USER}"
sudo install -d -o "$AGENT_USER" -g "$AGENT_USER" /opt/telephone-agent
sudo cp -a agent/. /opt/telephone-agent/
sudo -u "$AGENT_USER" python3 -m venv /opt/telephone-agent/.venv
sudo -u "$AGENT_USER" /opt/telephone-agent/.venv/bin/pip install -e /opt/telephone-agent
```

The package uses only the Python standard library. For development, from the
repository root, run `python3 -m venv .venv` and `.venv/bin/pip install -e ./agent`.

Configuration: the shipped unit passes `--config /etc/telephone-agent.toml`, so the
file lives outside the installation directory and survives a reinstall:

```bash
AGENT_USER="${SUDO_USER:-$USER}"
sudo install -m 0640 -o root -g "$AGENT_USER" config/config.example.toml /etc/telephone-agent.toml
sudo install -m 0640 -o root -g "$AGENT_USER" /dev/null /etc/telephone-agent.env
sudoedit /etc/telephone-agent.toml
sudoedit /etc/telephone-agent.env
```

Set `[audio].card` and `[dialer].url`/`token` in the TOML file. The service also
loads `/etc/telephone-agent.env`; runtime overrides use double underscores, such
as `TELEPHONE_AGENT_DIALER__TOKEN`. Do not put `TELEPHONE_AGENT_CONFIG`,
`TELEPHONE_AGENT_DIALER_URL`, or `TELEPHONE_AGENT_DIALER_TOKEN` in that file: the
runtime rejects them as unknown overrides.

`scripts/dial.sh` posts to `/call` and checks `/health`; it reads the single-
underscore variables or `[dialer] url` and `[dialer] token` from the TOML file
named by `TELEPHONE_AGENT_CONFIG`; set it to `/etc/telephone-agent.toml` for manual use.

Install the units:

```bash
sudo install -m 0644 scripts/systemd/telephone-agent.service /etc/systemd/system/
sudo install -m 0644 scripts/systemd/telephone-agent.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

Edit the `OnCalendar` line in `/etc/systemd/system/telephone-agent.timer`, then
enable the timer:

```bash
sudo systemctl enable --now telephone-agent.timer
systemctl list-timers telephone-agent.timer
journalctl -u telephone-agent.service -f
```

The service is `Type=oneshot`: each timer activation places one call and exits.
Do not add `Restart=always`; a restart would redial. Retries and slot idempotency
belong in the runtime and the dispatcher described in the operations guide.

The shipped unit uses `User=pi`; change it to your account, then run
`sudo systemctl daemon-reload`. `WorkingDirectory` and `ExecStart` point at
`/opt/telephone-agent`; change both if installed elsewhere.
`/etc/telephone-agent.env` is optional; the unit uses `-`, so it starts without it.
Logs go to journald, which is volatile in the reference hardening described below.

## 10. Networking and time

- Use 2.4 GHz Wi-Fi. The Zero 2 W cannot see 5 GHz networks.
- Reserve a static DHCP lease for the Wi-Fi MAC in the router. Find the MAC with
  `ip link show wlan0` and the address with `hostname -I`. A router reservation is
  more reliable than a static address on the Pi.
- Set the time zone and enable NTP:

```bash
sudo timedatectl set-timezone Asia/Kolkata && sudo timedatectl set-ntp true
timedatectl
```

The Pi has no battery-backed real-time clock. After a power cut it learns the time
from the network; scheduling depends on the wall clock. If scheduling is critical,
enable `systemd-time-wait-sync.service` and confirm `System clock synchronized: yes`.

## 11. Hardening and SD-card wear

`RuntimeWatchdogSec` is a global systemd manager setting, not a per-service setting.
Edit `/etc/systemd/system.conf`:

```ini
[Manager]
RuntimeWatchdogSec=15
```
Reboot and verify with `systemctl show -p RuntimeWatchdogUSec` and
`ls -l /dev/watchdog`. If the device is absent, enable `dtparam=watchdog=on` in
`/boot/firmware/config.txt`, reboot, and check again. The watchdog reboots a hung Pi
after 15 seconds; that can interrupt a call during a hard hang.

For journald, edit `/etc/systemd/journald.conf`:

```ini
[Journal]
Storage=volatile
RuntimeMaxUse=64M
```
Then run `sudo systemctl restart systemd-journald`. Logs live in memory and are lost
on reboot; use `journalctl -b` while debugging, and switch to persistent storage only
temporarily.

If `dphys-swapfile` is present, disabling it reduces SD writes: run
`sudo dphys-swapfile swapoff` and `sudo systemctl disable --now dphys-swapfile`, then
check `free -h` and `swapon --show`. The trade-off is real on a 512 MB board: without
swap, memory pressure kills processes sooner. If zram or another swap device is
present, `swapon --show` still lists it; decide from measured memory use.

Unattended upgrades are disabled for reproducibility. If the package is installed, run
`sudo systemctl disable --now unattended-upgrades`. Apply security updates manually on
a schedule you control, after taking a backup image. Leaving the system unpatched is a
security risk.

## 12. Troubleshooting

| Symptom | Likely cause | Check and fix |
|---|---|---|
| No gadget directory after start | configfs not mounted, `libcomposite` missing, or unit failed. | `systemctl status uac-gadget`; `lsmod \| grep -E 'dwc2\|libcomposite'`; `mount \| grep configfs`; fix boot files and reboot. |
| `/sys/class/udc` empty or UDC write fails | Controller in host mode, wrong overlay or command line, or cable in PWR IN. | `dmesg \| grep -i dwc2`; check the active `config.txt` and single-line `cmdline.txt`; use the USB data port; reboot. |
| Phone says unsupported device, or no notification | Charge-only cable, hub not passing data, UAC1 rejected, or Android USB audio routing disabled. | Check `dmesg` and the phone notification; try a data cable and another hub port; confirm Developer options -> Disable USB audio routing is OFF. Verify on your device. UAC2 is the fallback. |
| One-way audio | Inversion applied backwards, one stream not open, or call not routed to USB. | Repeat both loopback directions with a second phone; check in-call volume and the USB notification. |
| Phone does not charge | Charger in wrong hub port, no PD passthrough, weak cable, or battery protection. | Use the hub's PD input and the 25 W charger; try a 60 W cable; confirm Protect battery is intentional. |
| Hub drops under load, or Pi reboots | Bus-powered hub, low 5 V, long cables, or peak current. | Use the 25 W charger and a powered hub; short cables; power the Pi via PWR with 5 V/2 A; check `vcgencmd get_throttled`. |
| ALSA xrun or overrun | CPU or SD contention, Wi-Fi load, or small buffers. | Use Lite, stop unrelated services, keep 48 kHz/S16_LE/stereo, increase `--buffer-time`; watch `top` and `dmesg`. |
| Agent fails after reboot | Missing env file, wrong ownership, wrong venv path, or gadget not ready. | `systemctl status`; `journalctl -u telephone-agent -b`; check env permissions and `/opt/telephone-agent/.venv/bin/python`; start the gadget first. |

## 13. Known limitations

- The gadget is fixed at 48 kHz, 16-bit, stereo; the cellular codec (AMR-NB,
  AMR-WB, or EVS) caps fidelity. UAC1 is preferred, but some OEM builds route call
  audio differently; verify on your device, with UAC2 as a fallback.
- USB carries audio only. Dialing and hangup require the helper app, wireless
  debugging, or a no-code tool. One phone and SIM are assumed, with no failover or
  inbound support. The Zero 2 W has 512 MB RAM and no battery-backed clock, so local
  speech models are impractical and scheduling depends on network time sync.
- The unit assumes `/opt/telephone-agent`; moving the runtime requires editing the
  unit. journald is volatile, so logs do not survive a reboot. Carrier voicemail may
  not behave like a test IVR.
- This is a reference build, not a finished appliance; use a case, strain relief,
  and a UPS for the hub and router.

## 14. Next steps

1. Configure Phone A, install the dial helper, and configure the agent:
   `docs/android-setup.md`, `docs/dial-helper.md`, `docs/voice-agent.md`.
2. Repeat the loopback test after any kernel, cable, hub, or phone change, and review
   `docs/operations.md` and `docs/runbook.md` before unattended operation.
