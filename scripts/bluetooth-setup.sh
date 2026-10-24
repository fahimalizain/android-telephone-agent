#!/usr/bin/env bash
#
# Install the per-user WirePlumber configuration that makes this Linux host
# the HFP Hands-Free (HF) side of a Bluetooth link, with Phone A as the Audio
# Gateway (AG). This does not pair the phone; the phone UI must confirm.
#
# Run as the desktop user. Do not use sudo: WirePlumber configuration and the
# PipeWire session are per-user.
#
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly SCRIPT_DIR
REPO_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd -P)
readonly REPO_ROOT
readonly CONFIG_SOURCE="${REPO_ROOT}/config/wireplumber/51-bt-headset.conf"
readonly CONFIG_HOME="${XDG_CONFIG_HOME:-${HOME}/.config}"
readonly WIREPLUMBER_DIR="${CONFIG_HOME}/wireplumber/wireplumber.conf.d"
readonly CONFIG_DEST="${WIREPLUMBER_DIR}/51-bt-headset.conf"

usage() {
  cat <<'EOF'
Usage: bluetooth-setup.sh [--uninstall]

Install the user WirePlumber configuration that exposes this host as an HFP
Hands-Free (HF) headset for Phone A, the Audio Gateway (AG).

  (no option)   Install the configuration, back up an existing file, and
                restart the user WirePlumber service.
  --uninstall   Remove only the installed configuration file and restart
                WirePlumber. Backups and other files are left untouched.
  -h, --help    Show this help.

This script must run as the desktop user, not root. It writes:

  ${XDG_CONFIG_HOME:-$HOME/.config}/wireplumber/wireplumber.conf.d/51-bt-headset.conf

It does not pair or connect the phone. Pairing must be confirmed on the phone.
HFP carries call audio only; dialing and hangup still use the LAN helper.

Next steps after setup:
  1. Pair Phone A in its Bluetooth settings, trust this host, and enable
     "Calls and audio" for it.
  2. Check the static link state: ./scripts/bluetooth-check.sh --idle <PHONE_MAC>
  3. During a call, run: ./scripts/bluetooth-check.sh <PHONE_MAC>
     and: ./scripts/bluetooth-audio-test.sh list
EOF
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

require_user() {
  if ((EUID == 0)); then
    die "run as the desktop user, not root; this is a per-user WirePlumber config, so do not use sudo"
  fi
}

require_bluetoothctl() {
  command -v bluetoothctl >/dev/null 2>&1 ||
    die "bluetoothctl is not installed; install BlueZ (for example: sudo apt install bluez)"
}

require_pipewire_tools() {
  if ! command -v pw-dump >/dev/null 2>&1 && ! command -v wpctl >/dev/null 2>&1; then
    die "PipeWire tools not found; install WirePlumber and PipeWire (for example: sudo apt install wireplumber pipewire-bin)"
  fi
  if ! command -v pw-dump >/dev/null 2>&1; then
    printf 'warning: pw-dump is missing; bluetooth-check.sh and bluetooth-audio-test.sh need it (for example: sudo apt install pipewire-bin)\n' >&2
  fi
}

require_user_systemd() {
  command -v systemctl >/dev/null 2>&1 ||
    die "systemctl is not available; this setup expects a systemd user session"
  systemctl --user show-environment >/dev/null 2>&1 ||
    die "systemctl --user is unavailable; run this from the desktop session that owns PipeWire (a user systemd session is required)"
}

restart_wireplumber() {
  printf 'Restarting WirePlumber for the current user...\n'
  if systemctl --user restart wireplumber; then
    printf 'WirePlumber restarted.\n'
    return 0
  fi

  printf 'warning: systemctl --user restart wireplumber failed\n' >&2
  if command -v killall >/dev/null 2>&1 && command -v pgrep >/dev/null 2>&1 &&
    pgrep -x wireplumber >/dev/null 2>&1; then
    printf 'Trying killall wireplumber as a last resort...\n' >&2
    killall wireplumber 2>/dev/null || true
    sleep 2
    if pgrep -x wireplumber >/dev/null 2>&1; then
      printf 'WirePlumber is running again.\n'
      return 0
    fi
  fi

  printf 'error: could not restart WirePlumber; log out and back in, then run: systemctl --user restart wireplumber\n' >&2
  return 1
}

print_next_steps() {
  local mac_placeholder="AA:BB:CC:DD:EE:FF"

  cat <<EOF

Next steps:

1. Pair Phone A (the Audio Gateway). Use the phone's Bluetooth settings so the
   phone UI can confirm the pairing, then trust this host and enable
   "Calls and audio" / "Use for calls and media" for it. From a terminal, the
   equivalent after pairing is:

     bluetoothctl trust ${mac_placeholder}
     bluetoothctl connect ${mac_placeholder}

   ${mac_placeholder} is a placeholder; use Phone A's real address.

2. Verify the adapter and connection without needing an active call:

     ./scripts/bluetooth-check.sh --idle ${mac_placeholder}

3. Place a call on Phone A, then run the full check and audio tests:

     ./scripts/bluetooth-check.sh ${mac_placeholder}
     ./scripts/bluetooth-audio-test.sh list
     ./scripts/bluetooth-audio-test.sh record 10 /tmp/bluetooth-downlink.wav
     ./scripts/bluetooth-audio-test.sh play /tmp/bluetooth-downlink.wav

SCO nodes exist only while a call is active. HFP does not dial or hang up;
keep using scripts/dial.sh for the existing LAN HTTP helper.

EOF
}

install_config() {
  local backup=""

  [[ -f ${CONFIG_SOURCE} ]] || die "missing configuration: ${CONFIG_SOURCE}"
  require_user
  require_user_systemd
  require_bluetoothctl
  require_pipewire_tools

  install -d -m 0755 -- "${WIREPLUMBER_DIR}"
  if [[ ! -w ${WIREPLUMBER_DIR} ]]; then
    die "cannot write to ${WIREPLUMBER_DIR}; fix its ownership or permissions"
  fi

  if [[ -e ${CONFIG_DEST} ]]; then
    if cmp -s -- "${CONFIG_SOURCE}" "${CONFIG_DEST}"; then
      printf 'Configuration already current: %s\n' "${CONFIG_DEST}"
    else
      backup="${CONFIG_DEST}.bak.$(date +%Y%m%d-%H%M%S)"
      cp -p -- "${CONFIG_DEST}" "${backup}"
      printf 'Backed up existing configuration to %s\n' "${backup}"
    fi
  fi

  install -m 0644 -- "${CONFIG_SOURCE}" "${CONFIG_DEST}"
  printf 'Installed %s\n' "${CONFIG_DEST}"

  if ! restart_wireplumber; then
    print_next_steps
    return 1
  fi
  print_next_steps
}

uninstall_config() {
  require_user
  require_user_systemd

  if [[ ! -e ${CONFIG_DEST} ]]; then
    printf 'Not installed: %s\n' "${CONFIG_DEST}"
    return 0
  fi

  printf 'Removing %s (backups are retained).\n' "${CONFIG_DEST}"
  rm -f -- "${CONFIG_DEST}"
  restart_wireplumber
}

main() {
  if (($# > 1)); then
    usage >&2
    exit 2
  fi

  case "${1:-}" in
    "")
      install_config
      ;;
    --uninstall)
      uninstall_config
      ;;
    -h | --help)
      usage
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac
}

main "$@"
