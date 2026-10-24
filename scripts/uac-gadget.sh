#!/usr/bin/env bash
#
# Create or remove the UAC1 USB gadget used by android-telephone-agent.
# Run as root. The script is idempotent: starting an active gadget is a no-op,
# and stopping an absent gadget succeeds.
#
set -euo pipefail

readonly CONFIGFS_ROOT="/sys/kernel/config"
readonly GADGET_NAME="agent"
readonly GADGET_DIR="${CONFIGFS_ROOT}/usb_gadget/${GADGET_NAME}"
readonly UDC_CLASS_DIR="/sys/class/udc"

SELECTED_UDC=""

usage() {
  cat <<'EOF'
Usage: uac-gadget.sh [start|stop]

  start   Create and bind the UAC1 gadget (default).
  stop    Unbind the gadget and remove its configfs objects.

Environment:
  UDC_NAME  Select a controller when /sys/class/udc lists more than one.
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

assert_gadget_path() {
  # Teardown removes directories below this path. Refuse to continue if the
  # path constants ever change in a way that could escape the gadget subtree.
  if [[ ${CONFIGFS_ROOT} != "/sys/kernel/config" ||
        ${GADGET_DIR} != "/sys/kernel/config/usb_gadget/agent" ]]; then
    die "refusing unexpected gadget path: ${GADGET_DIR}"
  fi
}

configfs_mounted() {
  awk '$2 == "/sys/kernel/config" && $3 == "configfs" { found = 1 }
       END { exit(found ? 0 : 1) }' /proc/mounts
}

require_configfs() {
  [[ -d ${CONFIGFS_ROOT} ]] || die "${CONFIGFS_ROOT} does not exist"
  configfs_mounted || die "configfs is not mounted at ${CONFIGFS_ROOT}; check the boot configuration"
  [[ -d ${CONFIGFS_ROOT}/usb_gadget ]] || die "usb_gadget is unavailable; load libcomposite (modules-load=dwc2,libcomposite)"
}

require_udc() {
  local udc
  local -a udcs=()

  while IFS= read -r udc; do
    if [[ -n ${udc} ]]; then
      udcs+=("${udc##*/}")
    fi
  done < <(find "${UDC_CLASS_DIR}" -mindepth 1 -maxdepth 1 -print 2>/dev/null || true)

  if ((${#udcs[@]} == 0)); then
    die "no USB device controller in ${UDC_CLASS_DIR}; check dtoverlay=dwc2,dr_mode=peripheral and use the USB data port"
  fi

  SELECTED_UDC="${UDC_NAME:-${udcs[0]}}"
  if [[ ! -e "${UDC_CLASS_DIR}/${SELECTED_UDC}" ]]; then
    die "UDC '${SELECTED_UDC}' not found; available: ${udcs[*]}"
  fi
}

gadget_active() {
  [[ -s "${GADGET_DIR}/UDC" ]]
}

remove_empty_dir() {
  local path="$1"
  if [[ -d ${path} ]]; then
    rmdir -- "${path}" 2>/dev/null || true
  fi
}

teardown_gadget() {
  assert_gadget_path
  if [[ ! -d ${GADGET_DIR} ]]; then
    return 0
  fi

  # Release the controller before removing any descriptor objects. A newline
  # is an empty UDC name; a zero-byte write would not reach the store method.
  if [[ -e "${GADGET_DIR}/UDC" ]]; then
    printf '\n' > "${GADGET_DIR}/UDC" 2>/dev/null || true
  fi

  # The function link must go before the function directory.
  if [[ -L "${GADGET_DIR}/configs/c.1/uac1.0" ]]; then
    rm -f -- "${GADGET_DIR}/configs/c.1/uac1.0"
  fi

  remove_empty_dir "${GADGET_DIR}/configs/c.1/strings/0x409"
  remove_empty_dir "${GADGET_DIR}/configs/c.1"
  remove_empty_dir "${GADGET_DIR}/functions/uac1.0"
  remove_empty_dir "${GADGET_DIR}/strings/0x409"
  remove_empty_dir "${GADGET_DIR}"
}

cleanup_on_error() {
  local status=$?
  trap - ERR
  printf 'error: gadget setup failed; removing the partial configuration\n' >&2
  teardown_gadget || true
  exit "${status}"
}

create_gadget() {
  mkdir "${GADGET_DIR}"

  printf '0x1d6b\n' > "${GADGET_DIR}/idVendor"
  printf '0x0104\n' > "${GADGET_DIR}/idProduct"
  printf '0x0200\n' > "${GADGET_DIR}/bcdUSB"
  printf '0x0100\n' > "${GADGET_DIR}/bcdDevice"

  mkdir -p "${GADGET_DIR}/strings/0x409"
  printf 'agent-01\n' > "${GADGET_DIR}/strings/0x409/serialnumber"
  printf 'android-telephone-agent\n' > "${GADGET_DIR}/strings/0x409/manufacturer"
  printf 'Telephone Agent UAC1\n' > "${GADGET_DIR}/strings/0x409/product"

  mkdir -p "${GADGET_DIR}/configs/c.1/strings/0x409"
  printf 'UAC1 audio\n' > "${GADGET_DIR}/configs/c.1/strings/0x409/configuration"
  printf '250\n' > "${GADGET_DIR}/configs/c.1/MaxPower"

  mkdir -p "${GADGET_DIR}/functions/uac1.0"
  printf '48000\n' > "${GADGET_DIR}/functions/uac1.0/p_srate"
  printf '48000\n' > "${GADGET_DIR}/functions/uac1.0/c_srate"
  printf '0x3\n' > "${GADGET_DIR}/functions/uac1.0/p_chmask"
  printf '0x3\n' > "${GADGET_DIR}/functions/uac1.0/c_chmask"
  printf '2\n' > "${GADGET_DIR}/functions/uac1.0/p_ssize"
  printf '2\n' > "${GADGET_DIR}/functions/uac1.0/c_ssize"

  # Configfs resolves the relative target from the gadget directory.
  ( cd "${GADGET_DIR}" && ln -s functions/uac1.0 configs/c.1/uac1.0 )

  printf '%s\n' "${SELECTED_UDC}" > "${GADGET_DIR}/UDC"
  [[ -s "${GADGET_DIR}/UDC" ]] || die "failed to bind the gadget to ${SELECTED_UDC}"
}

print_status() {
  local card=""

  if command -v aplay >/dev/null 2>&1; then
    card=$(aplay -l 2>/dev/null |
      sed -nE '/UAC1Gadget/ s/^card [0-9]+: ([^ ]+) .*/\1/p' |
      head -n1 || true)
  fi

  printf 'USB gadget %s is bound to %s.\n' "${GADGET_NAME}" "${SELECTED_UDC}"
  if [[ -n ${card} ]]; then
    printf 'ALSA card: %s (device 0)\n' "${card}"
  else
    printf 'ALSA card: UAC1Gadget (not listed yet; run: aplay -l)\n'
  fi
  printf 'Next: place a call from Phone A, then run scripts/audio-loopback-test.sh record 10.\n'
}

start_gadget() {
  require_root
  assert_gadget_path
  require_configfs
  require_udc

  if gadget_active; then
    printf 'USB gadget %s is already bound to %s; nothing to do.\n' \
      "${GADGET_NAME}" "$(cat "${GADGET_DIR}/UDC")"
    print_status
    return 0
  fi

  if [[ -d ${GADGET_DIR} ]]; then
    printf 'Removing an incomplete gadget configuration first.\n'
    teardown_gadget
  fi

  trap cleanup_on_error ERR
  create_gadget
  trap - ERR
  print_status
}

stop_gadget() {
  require_root
  assert_gadget_path

  if [[ ! -d ${GADGET_DIR} ]]; then
    printf 'USB gadget %s is not configured; nothing to do.\n' "${GADGET_NAME}"
    return 0
  fi

  teardown_gadget
  printf 'USB gadget %s removed.\n' "${GADGET_NAME}"
}

main() {
  if (($# > 1)); then
    usage >&2
    exit 2
  fi

  case "${1:-start}" in
    start)
      start_gadget
      ;;
    stop)
      stop_gadget
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
