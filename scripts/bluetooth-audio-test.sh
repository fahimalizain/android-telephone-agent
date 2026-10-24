#!/usr/bin/env bash
#
# List, record, play, or inject a tone through the HFP SCO nodes that exist
# while a call is active. This host is the Hands-Free (HF) side; Phone A is
# the Audio Gateway (AG).
#
# Direction reminder:
#   bluez_input.*  (SCO source) carries Phone A's downlink: the remote party's
#                  voice. Recording it does not capture this host's microphone.
#   bluez_output.* (SCO sink) carries Phone A's uplink: the remote party hears
#                  what is played to it. This host does not hear that audio.
#
# Run as the desktop user. No root and no sudo are needed.
#
set -euo pipefail

readonly RATE=16000
readonly CHANNELS=1
readonly FORMAT=s16
readonly DEFAULT_RECORD_SECONDS=10
readonly DEFAULT_TONE_SECONDS=2
readonly MAX_RECORD_SECONDS=3600
readonly MAX_TONE_SECONDS=30
readonly MAC_REGEX='^([0-9A-F]{2}:){5}[0-9A-F]{2}$'
# Do not let pw-cat fall back to the default source/sink when the call-scoped
# SCO node disappears or the target is missing.
readonly STREAM_PROPERTIES='{"node.dont-fallback": true, "node.dont-reconnect": true}'

BT_ADDRESS="${BT_ADDRESS:-}"
SOURCE_NODE=""
SINK_NODE=""
DUMP_FILE=""
SCO_ROWS=""
SOURCE_LINES=()
SINK_LINES=()

usage() {
  cat <<'EOF'
Usage:
  bluetooth-audio-test.sh list
  bluetooth-audio-test.sh record [seconds] [outfile]
  bluetooth-audio-test.sh play <file>
  bluetooth-audio-test.sh tone [seconds]

Commands:
  list     Show the SCO source and sink nodes, codec, profile, and media.role.
  record   Capture the SCO downlink (Phone A's earpiece / the remote party)
           to a WAV file. Defaults: 10 seconds,
           bluetooth-downlink-YYYYmmdd-HHMMSS.wav in the current directory.
  play     Send a WAV file to the SCO uplink (Phone A's microphone path).
           The remote party hears it; this host does not.
  tone     Generate a quiet 440 Hz sine tone and send it to the SCO uplink.
           Defaults: 2 seconds. Useful as a safe injection test.

SCO nodes exist only while a call is active. This script finds them by
Bluetooth address and factory name; node suffixes such as .0 and .1 are not
stable.

Environment:
  BT_ADDRESS   Optional Phone A Bluetooth address, for example
               AA:BB:CC:DD:EE:FF. Use it to select one phone when more than
               one SCO endpoint is present.

Exit status:
  0  Success.
  1  No active call, missing SCO node, playback/record failure, or no
     PipeWire access.
  2  Usage error.

Examples:
  ./bluetooth-audio-test.sh list
  ./bluetooth-audio-test.sh record 15 /tmp/downlink.wav
  ./bluetooth-audio-test.sh play /tmp/downlink.wav
  ./bluetooth-audio-test.sh tone 2
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

cleanup() {
  if [[ -n ${DUMP_FILE} ]]; then
    rm -f -- "${DUMP_FILE}"
  fi
}

trap cleanup EXIT

require_user() {
  if ((EUID == 0)); then
    die "run as the desktop user, not root; PipeWire and WirePlumber live in the user session"
  fi
}

require_pw_dump() {
  command -v pw-dump >/dev/null 2>&1 ||
    die "pw-dump is not installed; install PipeWire tools (for example: sudo apt install pipewire-bin)"
  command -v python3 >/dev/null 2>&1 ||
    die "python3 is not installed; install Python 3 to query pw-dump"
}

require_pw_record() {
  command -v pw-record >/dev/null 2>&1 ||
    die "pw-record is not installed; install PipeWire tools (for example: sudo apt install pipewire-bin)"
}

require_pw_play() {
  command -v pw-play >/dev/null 2>&1 ||
    die "pw-play is not installed; install PipeWire tools (for example: sudo apt install pipewire-bin)"
}

normalize_mac() {
  local value="${1//-/:}"
  value=$(printf '%s' "${value}" | tr '[:lower:]' '[:upper:]')
  if [[ ! ${value} =~ ${MAC_REGEX} ]]; then
    usage_error "invalid Bluetooth address: ${1}; expected AA:BB:CC:DD:EE:FF"
  fi
  printf '%s\n' "${value}"
}

# Return one tab-separated field from a parsed SCO row. Field order:
# kind, address, node name, codec, profile, media.role, media.class.
row_field() {
  local row="$1"
  local index="$2"
  local -a fields=()
  IFS=$'\t' read -r -a fields <<<"${row}"
  printf '%s\n' "${fields[${index}]:-}"
}

split_sco_rows() {
  local row=""
  local kind=""

  SOURCE_LINES=()
  SINK_LINES=()
  while IFS= read -r row; do
    [[ -n ${row} ]] || continue
    kind=$(row_field "${row}" 0)
    case "${kind}" in
      source)
        SOURCE_LINES+=("${row}")
        ;;
      sink)
        SINK_LINES+=("${row}")
        ;;
      *)
        die "unexpected SCO row kind: ${kind}"
        ;;
    esac
  done <<<"${SCO_ROWS}"
}

load_sco_rows() {
  require_pw_dump
  DUMP_FILE=$(mktemp "${TMPDIR:-/tmp}/bluetooth-audio-test.XXXXXX") ||
    die "cannot create a temporary file in ${TMPDIR:-/tmp}"

  if ! pw-dump >"${DUMP_FILE}" 2>/dev/null; then
    die "pw-dump failed; is PipeWire running for this user? Try: wpctl status"
  fi

  # Discover by api.bluez5.address and factory.name. The .0/.1 suffix is a SPA
  # object id and can change, so it is deliberately not used for matching.
  if ! SCO_ROWS=$(python3 - "${BT_ADDRESS}" "${DUMP_FILE}" <<'PY'
import json
import sys

mac = sys.argv[1].strip().upper()
path = sys.argv[2]

try:
    with open(path, "r", encoding="utf-8") as handle:
        objects = json.load(handle)
except (OSError, json.JSONDecodeError) as exc:
    print(f"error: cannot read pw-dump JSON: {exc}", file=sys.stderr)
    raise SystemExit(1)

if not isinstance(objects, list):
    print("error: pw-dump JSON is not an array", file=sys.stderr)
    raise SystemExit(1)


def text(props, key):
    value = props.get(key)
    return str(value) if value not in (None, "") else "unknown"


roles = {
    "api.bluez5.sco.source": "source",
    "api.bluez5.sco.sink": "sink",
}
rows = []
seen = set()

for obj in objects:
    if not isinstance(obj, dict):
        continue
    info = obj.get("info")
    props = info.get("props") if isinstance(info, dict) else None
    if not isinstance(props, dict):
        props = obj.get("props")
    if not isinstance(props, dict):
        continue

    kind = roles.get(props.get("factory.name"))
    if kind is None:
        continue

    address = str(props.get("api.bluez5.address") or "")
    if mac and address.upper() != mac:
        continue

    name = text(props, "node.name")
    key = (kind, obj.get("id"), name)
    if key in seen:
        continue
    seen.add(key)
    rows.append((
        0 if kind == "source" else 1,
        "\t".join((
            kind,
            address,
            name,
            text(props, "api.bluez5.codec"),
            text(props, "api.bluez5.profile"),
            text(props, "media.role"),
            text(props, "media.class"),
        )),
    ))

for _, row in sorted(rows, key=lambda item: item[0]):
    print(row)
PY
  ); then
    die "failed to parse pw-dump output"
  fi

  rm -f -- "${DUMP_FILE}"
  DUMP_FILE=""
  split_sco_rows
}

print_node() {
  local row="$1"

  printf '  node:        %s\n' "$(row_field "${row}" 2)"
  printf '  address:     %s\n' "$(row_field "${row}" 1)"
  printf '  codec:       %s\n' "$(row_field "${row}" 3)"
  printf '  profile:     %s\n' "$(row_field "${row}" 4)"
  printf '  media.role:  %s\n' "$(row_field "${row}" 5)"
  printf '  media.class: %s\n' "$(row_field "${row}" 6)"
}

print_active_call_hint() {
  printf 'SCO nodes exist only while a call is active. Place a call on Phone A,\n'
  printf 'keep it connected through HFP, and rerun this command.\n'
}

list_nodes() {
  local row=""

  load_sco_rows

  if ((${#SOURCE_LINES[@]} == 0 && ${#SINK_LINES[@]} == 0)); then
    printf 'No HFP SCO nodes found'
    if [[ -n ${BT_ADDRESS} ]]; then
      printf ' for %s' "${BT_ADDRESS}"
    fi
    printf '.\n'
    print_active_call_hint
    printf 'If a call is already active, run: ./scripts/bluetooth-check.sh <PHONE_MAC>\n'
    exit 1
  fi

  printf 'HFP SCO nodes'
  if [[ -n ${BT_ADDRESS} ]]; then
    printf ' for %s' "${BT_ADDRESS}"
  fi
  printf ':\n'

  if ((${#SOURCE_LINES[@]} > 0)); then
    printf '\nDownlink/capture (Phone A -> host; the remote party):\n'
    for row in "${SOURCE_LINES[@]}"; do
      print_node "${row}"
    done
  else
    printf '\nDownlink/capture: missing\n'
  fi

  if ((${#SINK_LINES[@]} > 0)); then
    printf '\nUplink/playback (host -> Phone A; the remote party hears it):\n'
    for row in "${SINK_LINES[@]}"; do
      print_node "${row}"
    done
  else
    printf '\nUplink/playback: missing\n'
  fi

  if ((${#SOURCE_LINES[@]} == 0 || ${#SINK_LINES[@]} == 0)); then
    printf '\nIncomplete SCO set. '
    print_active_call_hint
    exit 1
  fi
}

resolve_source() {
  local row=""

  if ((${#SOURCE_LINES[@]} == 0)); then
    die "no SCO source node; place a call and keep Phone A connected through HFP"
  fi
  if ((${#SOURCE_LINES[@]} > 1)); then
    printf 'error: multiple SCO source nodes found:\n' >&2
    for row in "${SOURCE_LINES[@]}"; do
      printf '  %s (%s)\n' "$(row_field "${row}" 2)" "$(row_field "${row}" 1)" >&2
    done
    die "set BT_ADDRESS=AA:BB:CC:DD:EE:FF to select Phone A, or disconnect the other phone"
  fi
  SOURCE_NODE=$(row_field "${SOURCE_LINES[0]}" 2)
}

resolve_sink() {
  local row=""

  if ((${#SINK_LINES[@]} == 0)); then
    die "no SCO sink node; place a call and keep Phone A connected through HFP"
  fi
  if ((${#SINK_LINES[@]} > 1)); then
    printf 'error: multiple SCO sink nodes found:\n' >&2
    for row in "${SINK_LINES[@]}"; do
      printf '  %s (%s)\n' "$(row_field "${row}" 2)" "$(row_field "${row}" 1)" >&2
    done
    die "set BT_ADDRESS=AA:BB:CC:DD:EE:FF to select Phone A, or disconnect the other phone"
  fi
  SINK_NODE=$(row_field "${SINK_LINES[0]}" 2)
}

validate_seconds() {
  local raw="$1"
  local max="$2"
  local label="$3"

  if [[ ! ${raw} =~ ^[0-9]+$ ]] || ((10#${raw} < 1)) || ((10#${raw} > max)); then
    usage_error "${label} must be an integer from 1 to ${max}"
  fi
}

record_audio() {
  local seconds="${1:-${DEFAULT_RECORD_SECONDS}}"
  local outfile="${2:-}"
  local sample_count=0

  if (($# > 2)); then
    usage_error "record takes at most two arguments"
  fi
  validate_seconds "${seconds}" "${MAX_RECORD_SECONDS}" "seconds"
  seconds=$((10#${seconds}))
  sample_count=$((seconds * RATE))

  if [[ -z ${outfile} ]]; then
    outfile="bluetooth-downlink-$(date +%Y%m%d-%H%M%S).wav"
  fi
  if [[ -e ${outfile} || -L ${outfile} ]]; then
    die "refusing to overwrite existing path: ${outfile}"
  fi

  require_user
  require_pw_record
  load_sco_rows
  resolve_source

  printf 'Recording %s seconds from %s\n' "${seconds}" "${SOURCE_NODE}"
  printf 'Direction: SCO downlink - Phone A earpiece / the remote party. This is not the host microphone.\n'
  local record_rc=0
  # PipeWire 1.6.2 returns status 1 after --sample-count even when the WAV is
  # complete. Validate the WAV instead of treating that known status as a
  # failure; a genuinely missing target leaves an invalid or empty file.
  pw-record --target "${SOURCE_NODE}" --properties "${STREAM_PROPERTIES}" \
    --rate "${RATE}" --channels "${CHANNELS}" \
    --format "${FORMAT}" --sample-count "${sample_count}" -- "${outfile}" || record_rc=$?

  if ! python3 - "${outfile}" <<'PY'
import sys
import wave

try:
    with wave.open(sys.argv[1], "rb") as handle:
        if handle.getnframes() < 1:
            raise ValueError("empty WAV")
except (OSError, EOFError, ValueError, wave.Error):
    raise SystemExit(1)
PY
  then
    die "pw-record did not produce a valid WAV (status ${record_rc}); check the call and the SCO source"
  fi
  if ((record_rc != 0)); then
    printf 'note: pw-record returned %d after --sample-count; the WAV was validated and kept.\n' \
      "${record_rc}" >&2
  fi

  printf 'Wrote %s (WAV, %s seconds).\n' "${outfile}" "${seconds}"
  printf 'Copy the file to a workstation to listen; this host has no call speaker.\n'
}

play_audio() {
  local file="${1:-}"

  (($# == 1)) || usage_error "play takes exactly one WAV file"
  [[ -n ${file} ]] || usage_error "play requires a WAV file"
  [[ -f ${file} ]] || die "file not found: ${file}"
  [[ -r ${file} ]] || die "file is not readable: ${file}"
  [[ -s ${file} ]] || die "file is empty: ${file}"

  require_user
  require_pw_play
  load_sco_rows
  resolve_sink

  printf 'Playing %s to %s\n' "${file}" "${SINK_NODE}"
  printf 'Direction: SCO uplink - Phone A microphone path. The remote party hears it; this host does not.\n'
  pw-play --target "${SINK_NODE}" --properties "${STREAM_PROPERTIES}" -- "${file}"
  printf 'Playback finished.\n'
}

tone_audio() {
  local seconds="${1:-${DEFAULT_TONE_SECONDS}}"

  if (($# > 1)); then
    usage_error "tone takes at most one argument"
  fi
  validate_seconds "${seconds}" "${MAX_TONE_SECONDS}" "seconds"
  seconds=$((10#${seconds}))

  require_user
  require_pw_play
  load_sco_rows
  resolve_sink

  printf 'Injecting a quiet 440 Hz tone for %s seconds into %s\n' "${seconds}" "${SINK_NODE}"
  printf 'Direction: SCO uplink - the remote party hears the tone; this host does not.\n'
  python3 - "${seconds}" <<'PY' | pw-play --target "${SINK_NODE}" \
    --properties "${STREAM_PROPERTIES}" --raw --format "${FORMAT}" \
    --rate "${RATE}" --channels "${CHANNELS}" -
import math
import struct
import sys

seconds = int(sys.argv[1])
rate = 16000
frequency = 440.0
amplitude = 0.05
frames = seconds * rate
fade = min(int(rate * 0.01), frames // 2)

out = bytearray()
for index in range(frames):
    envelope = 1.0
    if index < fade:
        envelope = index / fade
    elif index >= frames - fade:
        envelope = (frames - 1 - index) / fade
    sample = int(32767 * amplitude * envelope * math.sin(2 * math.pi * frequency * index / rate))
    out.extend(struct.pack("<h", sample))
sys.stdout.buffer.write(out)
PY
  printf 'Tone finished.\n'
}

main() {
  if (($# == 0)); then
    usage >&2
    exit 2
  fi

  local mode="$1"
  shift

  case "${mode}" in
    -h | --help)
      usage
      exit 0
      ;;
    list | record | play | tone)
      ;;
    *)
      usage >&2
      exit 2
      ;;
  esac

  if [[ -n ${BT_ADDRESS} ]]; then
    BT_ADDRESS=$(normalize_mac "${BT_ADDRESS}") || exit $?
  fi

  case "${mode}" in
    list)
      (($# == 0)) || usage_error "list takes no arguments"
      require_user
      list_nodes
      ;;
    record)
      record_audio "$@"
      ;;
    play)
      play_audio "$@"
      ;;
    tone)
      tone_audio "$@"
      ;;
  esac
}

main "$@"
