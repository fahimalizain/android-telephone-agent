#!/usr/bin/env bash
#
# Loopback helper for the UAC1 gadget. Run this on the Raspberry Pi while
# Phone A has an active call.
#
# Direction reminder: on this gadget, aplay sends audio to Phone A's
# microphone input (the remote party hears it), and arecord captures Phone A's
# earpiece (the remote party's voice). See docs/raspberry-pi-setup.md.
#
set -euo pipefail

readonly CARD_NAME="UAC1Gadget"
readonly FORMAT="S16_LE"
readonly SAMPLE_RATE="48000"
readonly CHANNELS="2"
readonly DEVICE_NUMBER="0"

usage() {
  cat <<'EOF'
Usage:
  audio-loopback-test.sh record [seconds] [outfile]
  audio-loopback-test.sh play <file>
  audio-loopback-test.sh list

  record  Capture Phone A's earpiece / call downlink. Defaults: 10 seconds,
          loopback-YYYYmmdd-HHMMSS.wav in the current directory.
  play    Send a file to Phone A's microphone input / call uplink.
  list    Show ALSA playback and capture devices and the detected card.

Environment:
  CARD    ALSA card ID or number. Overrides auto-detection of UAC1Gadget.

Examples:
  ./audio-loopback-test.sh list
  ./audio-loopback-test.sh record 15 /tmp/downlink.wav
  ./audio-loopback-test.sh play /tmp/downlink.wav
  CARD=1 ./audio-loopback-test.sh record 5 /tmp/downlink.wav
EOF
}

die() {
  printf 'error: %s\n' "$*" >&2
  exit 1
}

require_tools() {
  local tool
  for tool in "$@"; do
    command -v "${tool}" >/dev/null 2>&1 ||
      die "${tool} is not installed; run: sudo apt install alsa-utils"
  done
}

detect_card() {
  local tool="$1"
  "${tool}" -l 2>/dev/null | awk -v name="${CARD_NAME}" '
    index($0, name) {
      for (i = 1; i <= NF; i++) {
        if ($i == "card") {
          id = $(i + 1)
          sub(/:$/, "", id)
          print id
          exit
        }
      }
    }'
}

resolve_card() {
  local tool="$1"
  local card=""

  if [[ -n ${CARD:-} ]]; then
    printf '%s\n' "${CARD}"
    return 0
  fi

  card=$(detect_card "${tool}")
  if [[ -z ${card} ]]; then
    die "card ${CARD_NAME} not found in ${tool} -l; start the gadget or set CARD=<id>"
  fi
  printf '%s\n' "${card}"
}

record_audio() {
  local seconds="${1:-10}"
  local outfile="${2:-}"
  local card
  local device

  if (($# > 2)); then
    usage >&2
    exit 2
  fi
  if ! [[ ${seconds} =~ ^[0-9]+$ ]] || ((10#${seconds} < 1)); then
    die "seconds must be a positive integer"
  fi

  if [[ -z ${outfile} ]]; then
    outfile="loopback-$(date +%Y%m%d-%H%M%S).wav"
  fi

  require_tools arecord
  card=$(resolve_card arecord)
  device="plughw:CARD=${card},DEV=${DEVICE_NUMBER}"

  printf 'Recording %s seconds from %s\n' "${seconds}" "${device}"
  printf 'Source: Phone A earpiece / call downlink (the remote party), not the Pi microphone.\n'
  arecord --device "${device}" --format "${FORMAT}" --rate "${SAMPLE_RATE}" \
    --channels "${CHANNELS}" --duration "${seconds}" -- "${outfile}"

  printf 'Wrote %s\n' "${outfile}"
  printf 'The Pi has no speaker. Copy the file to a workstation to listen.\n'
}

play_audio() {
  local file="${1:-}"
  local card
  local device

  if [[ -z ${file} ]]; then
    usage >&2
    exit 2
  fi
  [[ -f ${file} ]] || die "file not found: ${file}"
  [[ -r ${file} ]] || die "file is not readable: ${file}"

  require_tools aplay
  card=$(resolve_card aplay)
  device="plughw:CARD=${card},DEV=${DEVICE_NUMBER}"

  printf 'Playing %s to %s\n' "${file}" "${device}"
  printf 'Destination: Phone A microphone input / call uplink. The remote party hears it; the Pi does not.\n'
  aplay --device "${device}" --format "${FORMAT}" --rate "${SAMPLE_RATE}" \
    --channels "${CHANNELS}" -- "${file}"
  printf 'Playback finished.\n'
}

list_devices() {
  local play_card=""
  local record_card=""

  require_tools aplay arecord
  play_card=$(detect_card aplay || true)
  record_card=$(detect_card arecord || true)

  printf 'Expected card: %s\n' "${CARD_NAME}"
  if [[ -n ${CARD:-} ]]; then
    printf 'CARD override: %s\n' "${CARD}"
  fi
  if [[ -n ${play_card} ]]; then
    printf 'Detected playback card: %s (device %s)\n' "${play_card}" "${DEVICE_NUMBER}"
  else
    printf 'Detected playback card: none\n'
  fi
  if [[ -n ${record_card} ]]; then
    printf 'Detected capture card: %s (device %s)\n' "${record_card}" "${DEVICE_NUMBER}"
  else
    printf 'Detected capture card: none\n'
  fi

  printf '\nPlayback devices:\n'
  aplay -l 2>/dev/null || true
  printf '\nCapture devices:\n'
  arecord -l 2>/dev/null || true
  printf '\nDirection: aplay -> Phone A microphone (uplink); arecord <- Phone A earpiece (downlink).\n'
}

main() {
  if (($# == 0)); then
    usage >&2
    exit 2
  fi

  local mode="$1"
  shift

  case "${mode}" in
    record)
      record_audio "$@"
      ;;
    play)
      (($# == 1)) || {
        usage >&2
        exit 2
      }
      play_audio "$1"
      ;;
    list)
      (($# == 0)) || {
        usage >&2
        exit 2
      }
      list_devices
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
