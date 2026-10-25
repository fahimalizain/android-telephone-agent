#!/usr/bin/env bash
#
# Verify the HFP Hands-Free (HF) link from this Linux host to Phone A (the
# Audio Gateway). During a call, also verify that PipeWire created the SCO
# source (downlink) and SCO sink (uplink) nodes.
#
# Run as the desktop user. No root and no sudo are needed.
#
# SCO nodes are created only while a call is active. Use --idle for a static
# check of the adapter roles and the phone connection.
#
set -euo pipefail

readonly HEADSET_UUID="00001108-0000-1000-8000-00805f9b34fb"
readonly HANDSFREE_UUID="0000111e-0000-1000-8000-00805f9b34fb"
readonly MAC_REGEX='^([0-9A-F]{2}:){5}[0-9A-F]{2}$'

IDLE=0
MAC=""
DUMP_FILE=""
SCO_ROWS=""
SOURCE_LINES=()
SINK_LINES=()
LINK_LINES=()

usage() {
  cat <<'EOF'
Usage: bluetooth-check.sh [--idle] [MAC]

Check the Bluetooth HFP Hands-Free link from this host to Phone A.

  MAC        Phone A Bluetooth address, for example AA:BB:CC:DD:EE:FF.
             If omitted, BT_ADDRESS is used. If both are empty, connected
             devices are listed and the script exits 2.
  --idle     Static check only: adapter UUIDs and the phone connection. Skip
             the SCO node requirement. SCO nodes exist only during a call.
  -h, --help Show this help.

Exit status:
  0  Adapter advertises Headset (0x1108) and Handsfree (0x111e), Phone A is
     connected, and (unless --idle) both SCO nodes are present and are not
     linked to a local microphone or speaker.
  1  A check failed or PipeWire could not be queried.
  2  Usage error, or no phone address was supplied.

Environment:
  BT_ADDRESS   Phone A address when no positional MAC is given.

Examples:
  ./bluetooth-check.sh AA:BB:CC:DD:EE:FF
  ./bluetooth-check.sh --idle AA:BB:CC:DD:EE:FF
  BT_ADDRESS=AA:BB:CC:DD:EE:FF ./bluetooth-check.sh

Place a call before the full check: HFP audio nodes are created only for an
active call. The .0/.1 suffix on node names is not stable; this script finds
nodes by Bluetooth address and factory name.
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

require_user() {
  if ((EUID == 0)); then
    die "run as the desktop user, not root; PipeWire and WirePlumber live in the user session"
  fi
}

require_bluetoothctl() {
  command -v bluetoothctl >/dev/null 2>&1 ||
    die "bluetoothctl is not installed; install BlueZ (for example: sudo apt install bluez)"
}

require_pw_dump() {
  command -v pw-dump >/dev/null 2>&1 ||
    die "pw-dump is not installed; install PipeWire tools (for example: sudo apt install pipewire-bin)"
  command -v python3 >/dev/null 2>&1 ||
    die "python3 is not installed; install Python 3 to query pw-dump"
}

normalize_mac() {
  local value="${1//-/:}"
  value=$(printf '%s' "${value}" | tr '[:lower:]' '[:upper:]')
  if [[ ! ${value} =~ ${MAC_REGEX} ]]; then
    usage_error "invalid Bluetooth address: ${1}; expected AA:BB:CC:DD:EE:FF"
  fi
  printf '%s\n' "${value}"
}

# Read one field from bluetoothctl output. Leading indentation is stripped and
# the full value is kept, so keys containing spaces also work.
field_value() {
  local key="$1"
  local text="$2"
  printf '%s\n' "${text}" | awk -v key="${key}" '
    {
      wanted = key ":"
      line = $0
      sub(/^[[:space:]]+/, "", line)
      if (index(line, wanted) == 1) {
        value = substr(line, length(wanted) + 1)
        sub(/^[[:space:]]+/, "", value)
        sub(/[[:space:]]+$/, "", value)
        print value
        exit
      }
    }'
}

list_connected() {
  local devices=""

  devices=$(bluetoothctl devices Connected 2>/dev/null || true)
  if [[ -n ${devices} ]]; then
    printf 'Connected Bluetooth devices:\n%s\n\n' "${devices}"
  else
    printf 'No connected Bluetooth devices were reported.\n\n'
  fi
  usage >&2
  exit 2
}

adapter_has_uuid() {
  local text="$1"
  local full_uuid="$2"
  local short_uuid="$3"

  if printf '%s\n' "${text}" | grep -Fqi -- "${full_uuid}"; then
    return 0
  fi
  printf '%s\n' "${text}" | grep -Fqi -- "${short_uuid}"
}

load_sco_rows() {
  require_pw_dump
  DUMP_FILE=$(mktemp "${TMPDIR:-/tmp}/bluetooth-check.XXXXXX") ||
    die "cannot create a temporary file in ${TMPDIR:-/tmp}"

  if ! pw-dump >"${DUMP_FILE}" 2>/dev/null; then
    die "pw-dump failed; is PipeWire running for this user? Try: wpctl status"
  fi

  # Find SCO source/sink nodes by address and factory.name. Do not rely on the
  # .0/.1 suffix: it is a SPA object id and changes between calls.
  if ! SCO_ROWS=$(python3 - "${MAC}" "${DUMP_FILE}" <<'PY'
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


def props_of(obj):
    info = obj.get("info")
    props = info.get("props") if isinstance(info, dict) else None
    if not isinstance(props, dict):
        props = obj.get("props")
    return props if isinstance(props, dict) else None


# Map node id -> (node.name, media.class) so links can be named.
nodes = {}
for obj in objects:
    if not isinstance(obj, dict):
        continue
    props = props_of(obj)
    if props is None:
        continue
    node_id = obj.get("id")
    if node_id is None:
        continue
    nodes[int(node_id)] = (text(props, "node.name"), text(props, "media.class"))

roles = {
    "api.bluez5.sco.source": "source",
    "api.bluez5.sco.sink": "sink",
}
rows = []
seen = set()
sco_ids = {}

for obj in objects:
    if not isinstance(obj, dict):
        continue
    props = props_of(obj)
    if props is None:
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
    if obj.get("id") is not None:
        sco_ids[int(obj["id"])] = (kind, name)
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

# Report every PipeWire link that touches an SCO node. WirePlumber must not
# link these to the host's local devices; only the agent should.
for obj in objects:
    if not isinstance(obj, dict):
        continue
    props = props_of(obj)
    if props is None:
        continue
    try:
        out_id = int(props.get("link.output.node"))
        in_id = int(props.get("link.input.node"))
    except (TypeError, ValueError):
        continue
    for sco_id, peer_id in ((out_id, in_id), (in_id, out_id)):
        entry = sco_ids.get(sco_id)
        if entry is None:
            continue
        kind, sco_name = entry
        peer_name, peer_class = nodes.get(peer_id, ("unknown", "unknown"))
        rows.append((2, "\t".join(("link", kind, sco_name, peer_name, peer_class))))

for _, row in sorted(rows, key=lambda item: item[0]):
    print(row)
PY
  ); then
    die "failed to parse pw-dump output"
  fi

  rm -f -- "${DUMP_FILE}"
  DUMP_FILE=""
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
  LINK_LINES=()
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
      link)
        LINK_LINES+=("${row}")
        ;;
      *)
        die "unexpected SCO row kind: ${kind}"
        ;;
    esac
  done <<<"${SCO_ROWS}"
}

# A local device node is what WirePlumber picks as a default target: an ALSA
# microphone or speaker. Agent streams use Stream/* media classes instead.
is_local_device() {
  local name="$1"
  local media_class="$2"

  case "${media_class}" in
    Audio/Source | Audio/Sink) return 0 ;;
  esac
  [[ ${name} == alsa_* ]]
}

# Print the links that touch the SCO nodes and fail when a local device is
# attached. WirePlumber links the SCO nodes to the default source and sink
# unless node.autoconnect is disabled, which mixes the host microphone into
# the uplink and plays the downlink on the host speakers.
print_links() {
  local row=""
  local kind=""
  local sco=""
  local peer=""
  local peer_class=""
  local leaks=0

  printf '\nSCO links:\n'
  if ((${#LINK_LINES[@]} == 0)); then
    printf '  none (the agent links both directions explicitly when a call starts)\n'
    return 0
  fi

  for row in "${LINK_LINES[@]}"; do
    kind=$(row_field "${row}" 1)
    sco=$(row_field "${row}" 2)
    peer=$(row_field "${row}" 3)
    peer_class=$(row_field "${row}" 4)
    if is_local_device "${peer}" "${peer_class}"; then
      printf '  LEAK:  %s (%s) is linked to local device %s (%s)\n' \
        "${sco}" "${kind}" "${peer}" "${peer_class}"
      leaks=$((leaks + 1))
    else
      printf '  ok:    %s (%s) <-> %s (%s)\n' "${sco}" "${kind}" "${peer}" "${peer_class}"
    fi
  done

  if ((leaks > 0)); then
    cat <<'EOF'

WirePlumber is routing the call audio through this host's own devices.
The remote party hears this host's microphone, and the downlink plays on the
host speakers. Install the repository fragment and restart WirePlumber:

  ./scripts/bluetooth-setup.sh
EOF
    return 1
  fi
  return 0
}

print_nodes() {
  local label="$1"
  shift
  local row=""
  local node=""
  local address=""
  local codec=""
  local profile=""
  local role=""
  local media_class=""

  printf '\n%s:\n' "${label}"
  for row in "$@"; do
    node=$(row_field "${row}" 2)
    address=$(row_field "${row}" 1)
    codec=$(row_field "${row}" 3)
    profile=$(row_field "${row}" 4)
    role=$(row_field "${row}" 5)
    media_class=$(row_field "${row}" 6)
    printf '  node:        %s\n' "${node}"
    printf '  address:     %s\n' "${address}"
    printf '  codec:       %s\n' "${codec}"
    printf '  profile:     %s\n' "${profile}"
    printf '  media.role:  %s\n' "${role}"
    printf '  media.class: %s\n' "${media_class}"
  done
}

main() {
  local adapter_info=""
  local adapter_address=""
  local powered=""
  local phone_info=""
  local name=""
  local paired=""
  local bonded=""
  local trusted=""
  local connected=""
  local battery=""
  local rssi=""
  local headset_ok=0
  local handsfree_ok=0
  local static_ok=0
  local sco_ok=0
  local link_ok=1
  local positionals=0

  while (($# > 0)); do
    case "$1" in
      --idle)
        IDLE=1
        shift
        ;;
      -h | --help)
        usage
        exit 0
        ;;
      --)
        shift
        while (($# > 0)); do
          if ((positionals > 0)); then
            usage_error "expected at most one Bluetooth address"
          fi
          MAC="$1"
          positionals=$((positionals + 1))
          shift
        done
        ;;
      -*)
        usage_error "unknown option: $1"
        ;;
      *)
        if ((positionals > 0)); then
          usage_error "expected at most one Bluetooth address"
        fi
        MAC="$1"
        positionals=$((positionals + 1))
        shift
        ;;
    esac
  done

  MAC="${MAC:-${BT_ADDRESS:-}}"

  require_user
  require_bluetoothctl

  if [[ -z ${MAC} ]]; then
    list_connected
  fi
  MAC=$(normalize_mac "${MAC}") || exit $?

  trap cleanup EXIT

  if ! adapter_info=$(bluetoothctl show 2>/dev/null); then
    die "bluetoothctl show failed; is BlueZ running? Try: systemctl status bluetooth"
  fi
  adapter_address=$(printf '%s\n' "${adapter_info}" | awk '/^Controller /{print $2; exit}')
  if [[ -z ${adapter_address} ]]; then
    die "no Bluetooth controller found; check: bluetoothctl list"
  fi
  powered=$(field_value "Powered" "${adapter_info}")
  if adapter_has_uuid "${adapter_info}" "${HEADSET_UUID}" "0x1108"; then
    headset_ok=1
  fi
  if adapter_has_uuid "${adapter_info}" "${HANDSFREE_UUID}" "0x111e"; then
    handsfree_ok=1
  fi

  if ! phone_info=$(bluetoothctl info "${MAC}" 2>/dev/null); then
    die "bluetoothctl info failed for ${MAC}; is Phone A paired and is BlueZ running?"
  fi
  if ! printf '%s\n' "${phone_info}" |
    awk -v mac="${MAC}" '$1 == "Device" && toupper($2) == mac { found = 1 } END { exit !found }'; then
    printf 'error: Phone A (%s) is not known to BlueZ.\n' "${MAC}" >&2
    printf 'Pair it from the phone UI or with bluetoothctl, then rerun this check.\n' >&2
    exit 1
  fi

  name=$(field_value "Name" "${phone_info}")
  paired=$(field_value "Paired" "${phone_info}")
  bonded=$(field_value "Bonded" "${phone_info}")
  trusted=$(field_value "Trusted" "${phone_info}")
  connected=$(field_value "Connected" "${phone_info}")
  battery=$(field_value "Battery Percentage" "${phone_info}")
  rssi=$(field_value "RSSI" "${phone_info}")

  printf 'Bluetooth HFP check\n'
  printf '  Phone:   %s\n' "${MAC}"
  printf '  Adapter: %s\n' "${adapter_address}"

  printf '\nAdapter HFP roles:\n'
  printf '  Powered:              %s\n' "${powered:-unknown}"
  if ((headset_ok)); then
    printf '  Headset (0x1108):     yes\n'
  else
    printf '  Headset (0x1108):     no\n'
  fi
  if ((handsfree_ok)); then
    printf '  Handsfree (0x111e):   yes\n'
  else
    printf '  Handsfree (0x111e):   no\n'
  fi
  if ((!headset_ok || !handsfree_ok)); then
    printf '\nThe adapter does not advertise both HFP roles.\n'
    printf 'Run scripts/bluetooth-setup.sh as the desktop user, then restart WirePlumber.\n'
  elif [[ ${powered} != yes ]]; then
    printf '\nThe adapter is powered off. Turn it on with: bluetoothctl power on\n'
  fi

  printf '\nPhone state:\n'
  printf '  Name:      %s\n' "${name:-unknown}"
  printf '  Paired:    %s\n' "${paired:-unknown}"
  printf '  Bonded:    %s\n' "${bonded:-unknown}"
  printf '  Trusted:   %s\n' "${trusted:-unknown}"
  printf '  Connected: %s\n' "${connected:-unknown}"
  if [[ -n ${battery} ]]; then
    printf '  Battery:   %s\n' "${battery}"
  fi
  if [[ -n ${rssi} ]]; then
    printf '  RSSI:      %s\n' "${rssi}"
  fi
  if [[ -z ${battery} && -z ${rssi} ]]; then
    printf '  Battery/RSSI: not reported\n'
  fi

  if ((headset_ok && handsfree_ok)) && [[ ${powered} == yes && ${connected} == yes ]]; then
    static_ok=1
  fi
  if [[ ${connected} == yes && ${trusted} != yes ]]; then
    printf '\nwarning: Phone A is connected but not trusted; run: bluetoothctl trust %s\n' "${MAC}"
  fi

  if ((IDLE)); then
    printf '\n--idle: skipping the active-call SCO node check.\n'
    if ((static_ok)); then
      printf '\nResult: OK (static checks only).\n'
      exit 0
    fi
    printf '\nResult: FAIL - see the messages above.\n' >&2
    exit 1
  fi

  load_sco_rows
  split_sco_rows

  if ((${#SOURCE_LINES[@]} > 0)); then
    print_nodes "SCO downlink/capture (source; Phone A -> host / remote party)" "${SOURCE_LINES[@]}"
  else
    printf '\nSCO downlink/capture (source): missing\n'
  fi
  if ((${#SINK_LINES[@]} > 0)); then
    print_nodes "SCO uplink/playback (sink; host -> Phone A / remote party hears it)" "${SINK_LINES[@]}"
  else
    printf '\nSCO uplink/playback (sink): missing\n'
  fi

  if ((${#SOURCE_LINES[@]} > 0 && ${#SINK_LINES[@]} > 0)); then
    sco_ok=1
  else
    printf '\nNo complete set of HFP SCO nodes was found for %s.\n' "${MAC}"
    printf 'SCO nodes exist only while a call is active. Place a call and keep\n'
    printf 'Phone A connected through HFP, then rerun this check.\n'
    printf 'If a call is active, verify: systemctl --user status pipewire wireplumber\n'
  fi

  if ((sco_ok)); then
    if ! print_links; then
      link_ok=0
    fi
  fi

  if ((static_ok && sco_ok && link_ok)); then
    printf '\nResult: OK - HFP roles, phone connection, both SCO nodes, and no local link.\n'
    exit 0
  fi

  printf '\nResult: FAIL - see the messages above.\n' >&2
  exit 1
}

main "$@"
