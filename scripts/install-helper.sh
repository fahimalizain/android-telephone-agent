#!/usr/bin/env bash
#
# Install the android-helper APK on the phone over adb, grant the runtime
# permissions it needs, whitelist it from device idle, and start DialService.
# All operations are host-side; nothing is built here.
#
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly SCRIPT_DIR
REPO_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd -P)
readonly REPO_ROOT
readonly DEBUG_APK="${REPO_ROOT}/android-helper/app/build/outputs/apk/debug/app-debug.apk"
readonly RELEASE_APK="${REPO_ROOT}/android-helper/app/build/outputs/apk/release/app-release.apk"
readonly PACKAGE="dev.example.dialhelper"
readonly SERVICE="${PACKAGE}/.DialService"
readonly DEFAULT_ANDROID_HOME="/data/Android"
readonly HEALTH_TIMEOUT=10

APK=""
SERIAL=""
START=1
HEALTH_URL=""
FAILED=0
ADB=()
ADB_DEVICE=()

usage() {
  cat <<'EOF'
Usage: install-helper.sh [--apk PATH] [--serial SERIAL] [--no-start] [--health URL]

  --apk PATH       Install PATH instead of the newest built APK.
  --serial SERIAL  Target this adb serial. Without it, exactly one usable
                   device must be connected.
  --no-start       Install, grant permissions, and whitelist only. Do not
                   start DialService.
  --health URL     After installing, GET URL/health and print the response.
                   URL is the helper base URL, for example
                   http://192.168.1.50:8765.
  -h, --help       Show this help.

Without --apk, the newest of the release and debug APKs under
android-helper/app/build/outputs/apk/ is used. Build one with
./scripts/build-helper.sh.

The permission grants and device-idle whitelist are best effort. The script
never reads or prints the helper token. It also prints the phone's wlan0 IPv4
address when available.
EOF
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

usage_error() {
  printf 'error: %s\n' "$*" >&2
  usage >&2
  exit 2
}

validate_health_option() {
  [[ -n ${HEALTH_URL} ]] || return 0

  command -v curl >/dev/null 2>&1 ||
    die "curl is required for --health but was not found"
  if [[ ! ${HEALTH_URL} =~ ^https?://[^[:space:]]+$ ]]; then
    die "--health URL must start with http:// or https:// and contain no whitespace"
  fi
}

resolve_adb() {
  if command -v adb >/dev/null 2>&1; then
    ADB=(adb)
    return 0
  fi

  local sdk=""
  sdk="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-${DEFAULT_ANDROID_HOME}}}"
  if [[ -x ${sdk}/platform-tools/adb ]]; then
    ADB=("${sdk}/platform-tools/adb")
    return 0
  fi

  die "adb not found; add platform-tools to PATH or set ANDROID_HOME"
}

newest_apk() {
  local -a candidates=()
  local apk=""
  local newest=""
  local newest_mtime=0
  local mtime=0

  [[ -f ${RELEASE_APK} ]] && candidates+=("${RELEASE_APK}")
  [[ -f ${DEBUG_APK} ]] && candidates+=("${DEBUG_APK}")

  ((${#candidates[@]} > 0)) || return 1

  for apk in "${candidates[@]}"; do
    mtime=$(stat -c '%Y' -- "${apk}")
    if ((mtime > newest_mtime)); then
      newest="${apk}"
      newest_mtime="${mtime}"
    fi
  done

  [[ -n ${newest} ]] || return 1
  printf '%s\n' "${newest}"
}

resolve_apk() {
  if [[ -n ${APK} ]]; then
    [[ -f ${APK} ]] || die "APK not found: ${APK}"
  else
    APK=$(newest_apk) ||
      die "no built APK found; run ./scripts/build-helper.sh first"
  fi

  APK="$(cd -- "$(dirname -- "${APK}")" && pwd -P)/$(basename -- "${APK}")"
  [[ -r ${APK} ]] || die "APK is not readable: ${APK}"
  printf 'APK: %s\n' "${APK}"
}

resolve_device() {
  local devices_output=""
  local devices_clean=""
  local -a devices=()
  local device=""
  local found=0

  if ! devices_output=$("${ADB[@]}" devices); then
    die "failed to run adb devices"
  fi
  devices_clean="${devices_output//$'\r'/}"
  mapfile -t devices < <(
    printf '%s\n' "${devices_clean}" |
      awk 'NR > 1 && NF >= 2 && $2 == "device" { print $1 }'
  )

  if [[ -n ${SERIAL} ]]; then
    for device in "${devices[@]}"; do
      if [[ ${device} == "${SERIAL}" ]]; then
        found=1
        break
      fi
    done
    if ((!found)); then
      printf 'error: device %s is not connected and authorized\n' "${SERIAL}" >&2
      printf 'adb devices:\n%s\n' "${devices_clean}" >&2
      exit 1
    fi
  else
    if ((${#devices[@]} != 1)); then
      printf 'error: expected exactly one usable device, found %d\n' \
        "${#devices[@]}" >&2
      printf 'adb devices:\n%s\n' "${devices_clean}" >&2
      printf 'Connect one phone or pass --serial SERIAL.\n' >&2
      exit 1
    fi
    SERIAL="${devices[0]}"
  fi

  ADB_DEVICE=("${ADB[@]}" -s "${SERIAL}")
  printf 'Device: %s\n' "${SERIAL}"
}

install_apk() {
  local install_output=""

  printf '\nInstalling...\n'
  if ! install_output=$("${ADB_DEVICE[@]}" install -r "${APK}" 2>&1); then
    printf '%s\n' "${install_output}" >&2
    die "adb install failed"
  fi
  install_output="${install_output//$'\r'/}"
  case "${install_output}" in
    *Failure* | *failed*)
      printf '%s\n' "${install_output}" >&2
      die "adb install failed"
      ;;
  esac
  printf '%s\n' "${install_output}"
}

grant_permissions() {
  local -a permissions=(
    android.permission.CALL_PHONE
    android.permission.READ_PHONE_STATE
    android.permission.READ_PHONE_NUMBERS
    android.permission.ANSWER_PHONE_CALLS
    android.permission.POST_NOTIFICATIONS
  )
  local permission=""

  printf '\nGranting runtime permissions (best effort)...\n'
  for permission in "${permissions[@]}"; do
    if "${ADB_DEVICE[@]}" shell pm grant "${PACKAGE}" "${permission}" \
      >/dev/null 2>&1; then
      printf '  granted %s\n' "${permission##*.}"
    else
      printf '  warning: could not grant %s\n' "${permission}" >&2
    fi
  done
}

whitelist_package() {
  local whitelist_output=""

  printf '\nWhitelisting from device idle...\n'
  if whitelist_output=$(
    "${ADB_DEVICE[@]}" shell cmd deviceidle whitelist "+${PACKAGE}" 2>&1
  ); then
    printf '  %s\n' "${whitelist_output//$'\r'/}"
  else
    printf '  warning: deviceidle whitelist failed: %s\n' \
      "${whitelist_output//$'\r'/}" >&2
  fi
}

start_service() {
  local start_output=""

  if ((!START)); then
    printf '\nSkipping service start (--no-start). Start it later with:\n'
    printf '  adb -s %s shell am start-foreground-service -n %s\n' \
      "${SERIAL}" "${SERVICE}"
    return 0
  fi

  printf '\nStarting %s...\n' "${SERVICE}"
  if ! start_output=$(
    "${ADB_DEVICE[@]}" shell am start-foreground-service -n "${SERVICE}" 2>&1
  ); then
    printf '%s\n' "${start_output//$'\r'/}" >&2
    printf '  warning: service start command failed\n' >&2
    FAILED=1
    return 0
  fi

  start_output="${start_output//$'\r'/}"
  printf '  %s\n' "${start_output}"
  case "${start_output}" in
    *Error* | *Exception*)
      printf '  warning: service may not have started\n' >&2
      FAILED=1
      ;;
  esac
}

print_launcher_hint() {
  printf '\nOpen the launcher activity (if the app has one):\n'
  printf '  adb -s %s shell monkey -p %s -c android.intent.category.LAUNCHER 1\n' \
    "${SERIAL}" "${PACKAGE}"
}

print_wifi_hint() {
  local wlan_output=""
  local ip=""

  printf '\nPhone Wi-Fi (wlan0):\n'
  if ! wlan_output=$(
    "${ADB_DEVICE[@]}" shell ip -f inet addr show wlan0 2>/dev/null
  ); then
    printf '  unavailable; check the phone Wi-Fi settings\n'
    return 0
  fi

  wlan_output="${wlan_output//$'\r'/}"
  if [[ -z ${wlan_output} ]]; then
    printf '  no output; wlan0 may be down or the phone may be on mobile data\n'
    return 0
  fi

  printf '%s\n' "${wlan_output}" | sed 's/^/  /'
  ip=$(printf '%s\n' "${wlan_output}" |
    awk '/inet / { sub(/\/.*/, "", $2); print $2; exit }')
  if [[ -n ${ip} ]]; then
    printf 'Helper URL hint: http://%s:8765\n' "${ip}"
  else
    printf 'Could not parse an IPv4 address from wlan0.\n'
  fi
}

check_health() {
  local endpoint=""
  local response=""
  local status=""
  local body=""
  local attempt=1
  local max_attempts=10

  endpoint="${HEALTH_URL%/}/health"

  printf '\nHealth: GET %s\n' "${endpoint}"
  # The service starts asynchronously after am start-foreground-service.
  # Retry briefly instead of reporting a race as a provisioning failure.
  while ((attempt <= max_attempts)); do
    if response=$(curl --silent --show-error --max-time "${HEALTH_TIMEOUT}" \
      --write-out $'\n%{http_code}' "${endpoint}" 2>/dev/null); then
      status="${response##*$'\n'}"
      body="${response%$'\n'*}"
      if [[ ${status} =~ ^2[0-9][0-9]$ ]]; then
        printf '  HTTP %s\n' "${status}"
        if [[ -n ${body} ]]; then
          printf '%s\n' "${body}" | sed 's/^/  /'
        fi
        return 0
      fi
    fi
    if ((attempt < max_attempts)); then
      printf '  waiting for the service (%d/%d)...\n' "${attempt}" "${max_attempts}"
      sleep 2
    fi
    attempt=$((attempt + 1))
  done

  if [[ -n ${status} ]]; then
    printf 'error: health check returned HTTP %s\n' "${status}" >&2
  else
    printf 'error: health request failed; service is not listening\n' >&2
  fi
  FAILED=1
}

main() {
  while (($# > 0)); do
    case "$1" in
      --apk)
        (($# >= 2)) || usage_error "--apk requires a path"
        [[ -n $2 ]] || usage_error "--apk requires a non-empty path"
        APK="$2"
        shift 2
        ;;
      --serial)
        (($# >= 2)) || usage_error "--serial requires a value"
        [[ -n $2 ]] || usage_error "--serial requires a non-empty value"
        SERIAL="$2"
        shift 2
        ;;
      --no-start)
        START=0
        shift
        ;;
      --health)
        (($# >= 2)) || usage_error "--health requires a URL"
        [[ -n $2 ]] || usage_error "--health requires a non-empty URL"
        HEALTH_URL="$2"
        shift 2
        ;;
      -h | --help)
        usage
        exit 0
        ;;
      *)
        usage_error "unknown option: $1"
        ;;
    esac
  done

  validate_health_option
  resolve_adb
  resolve_apk
  resolve_device
  install_apk
  grant_permissions
  whitelist_package
  start_service
  print_launcher_hint
  print_wifi_hint
  if [[ -n ${HEALTH_URL} ]]; then
    check_health
  else
    printf '\nNext: ./scripts/helper-smoke-test.sh --url http://PHONE_IP:8765\n'
  fi

  if ((FAILED)); then
    printf '\nInstall finished with warnings or errors.\n' >&2
    exit 1
  fi

  printf '\nInstall complete.\n'
}

main "$@"
