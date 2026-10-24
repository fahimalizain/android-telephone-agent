#!/usr/bin/env bash
#
# Install or remove the android-telephone-agent systemd units and the
# uac-gadget helper. Run as root. The installer enables uac-gadget at boot
# but does not start it; verification is deliberately manual.
#
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly SCRIPT_DIR
readonly GADGET_SOURCE="${SCRIPT_DIR}/uac-gadget.sh"
readonly UNIT_SOURCE_DIR="${SCRIPT_DIR}/systemd"
readonly GADGET_DEST="/usr/local/sbin/uac-gadget.sh"
readonly SYSTEMD_DIR="/etc/systemd/system"
readonly UAC_UNIT="${SYSTEMD_DIR}/uac-gadget.service"
readonly AGENT_UNIT="${SYSTEMD_DIR}/telephone-agent.service"
readonly TIMER_UNIT="${SYSTEMD_DIR}/telephone-agent.timer"

usage() {
  cat <<'EOF'
Usage: install.sh [--uninstall]

  (no option)   Install the gadget helper and systemd units; enable uac-gadget.
  --uninstall   Disable and remove the units and gadget helper. Configuration
                under /etc and /opt is retained.
EOF
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

require_root() {
  if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    die "must run as root (use sudo)"
  fi
}

require_sources() {
  [[ -f ${GADGET_SOURCE} ]] || die "missing ${GADGET_SOURCE}"
  [[ -f ${UNIT_SOURCE_DIR}/uac-gadget.service ]] ||
    die "missing ${UNIT_SOURCE_DIR}/uac-gadget.service"
  [[ -f ${UNIT_SOURCE_DIR}/telephone-agent.service ]] ||
    die "missing ${UNIT_SOURCE_DIR}/telephone-agent.service"
  [[ -f ${UNIT_SOURCE_DIR}/telephone-agent.timer ]] ||
    die "missing ${UNIT_SOURCE_DIR}/telephone-agent.timer"
}

install_files() {
  require_sources
  command -v systemctl >/dev/null 2>&1 || die "systemctl is not available"

  install -d /usr/local/sbin "${SYSTEMD_DIR}"
  install -m 0755 "${GADGET_SOURCE}" "${GADGET_DEST}"
  install -m 0644 "${UNIT_SOURCE_DIR}/uac-gadget.service" "${UAC_UNIT}"
  install -m 0644 "${UNIT_SOURCE_DIR}/telephone-agent.service" "${AGENT_UNIT}"
  install -m 0644 "${UNIT_SOURCE_DIR}/telephone-agent.timer" "${TIMER_UNIT}"

  systemctl daemon-reload
  systemctl enable uac-gadget.service

  cat <<'EOF'

Installed:
  /usr/local/sbin/uac-gadget.sh
  /etc/systemd/system/uac-gadget.service
  /etc/systemd/system/telephone-agent.service
  /etc/systemd/system/telephone-agent.timer

uac-gadget.service is enabled but not started. Verify manually:

  sudo systemctl start uac-gadget.service
  systemctl status uac-gadget.service --no-pager
  ls /sys/class/udc
  aplay -l | grep UAC1Gadget
  arecord -l | grep UAC1Gadget
  cat /sys/kernel/config/usb_gadget/agent/UDC

Then place a call from Phone A and run:

  ./scripts/audio-loopback-test.sh list
  ./scripts/audio-loopback-test.sh record 15 /tmp/downlink.wav
  ./scripts/audio-loopback-test.sh play /tmp/downlink.wav

telephone-agent.service is installed but not enabled. It is a one-shot unit: one
timer activation places one call and exits. Do not add Restart=always, or it will
redial after every exit.

Install the runtime under /opt/telephone-agent, create /etc/telephone-agent.toml and
/etc/telephone-agent.env, then edit the OnCalendar line in
/etc/systemd/system/telephone-agent.timer and enable it:

  sudo systemctl enable --now telephone-agent.timer
  systemctl list-timers telephone-agent.timer

EOF
}

uninstall_files() {
  command -v systemctl >/dev/null 2>&1 || die "systemctl is not available"

  # --now runs ExecStop, which unbinds the UDC before files are removed.
  systemctl disable --now uac-gadget.service 2>/dev/null || true
  systemctl disable --now telephone-agent.timer 2>/dev/null || true
  systemctl disable --now telephone-agent.service 2>/dev/null || true

  rm -f -- "${UAC_UNIT}"
  rm -f -- "${AGENT_UNIT}"
  rm -f -- "${TIMER_UNIT}"
  rm -f -- "${GADGET_DEST}"

  systemctl daemon-reload

  cat <<'EOF'

Removed the systemd units and /usr/local/sbin/uac-gadget.sh.

Retained:
  /etc/telephone-agent.env
  /etc/telephone-agent.toml
  /opt/telephone-agent

Remove those manually if you also want to delete configuration and runtime data.

EOF
}

main() {
  if (($# > 1)); then
    usage >&2
    exit 2
  fi

  case "${1:-}" in
    "")
      require_root
      install_files
      ;;
    --uninstall)
      require_root
      uninstall_files
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
