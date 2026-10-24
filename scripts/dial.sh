#!/usr/bin/env bash
#
# Place a call through the Phone A dial helper, or check its health endpoint.
# Configuration comes from TELEPHONE_AGENT_DIALER_URL and
# TELEPHONE_AGENT_DIALER_TOKEN, or from a TOML file named by
# TELEPHONE_AGENT_CONFIG with [dialer] url and [dialer] token. The
# TELEPHONE_AGENT_DIALER__URL and __TOKEN forms are also accepted, so the
# runtime's environment file can be sourced for manual dialing.
#
set -euo pipefail

readonly DEFAULT_TIMEOUT=30
readonly E164_REGEX='^\+[1-9][0-9]{6,14}$'

URL=""
TOKEN=""
NUMBER=""

usage() {
  cat <<'EOF'
Usage:
  dial.sh +91XXXXXXXXXX
  dial.sh --health

Environment:
  TELEPHONE_AGENT_DIALER_URL    Base URL of the helper, for example
                                http://192.168.1.50:8765. The script posts to
                                /call and checks /health.
  TELEPHONE_AGENT_DIALER_TOKEN  Bearer token for the helper.
  TELEPHONE_AGENT_CONFIG        Optional TOML file with [dialer] url and
                                [dialer] token. Environment values win.
                                The __URL and __TOKEN spellings are also read.

The number must be E.164: a plus sign, a non-zero country digit, and 6 to 14
more digits (7 to 15 digits total).

Exit status is non-zero for invalid input, configuration errors, transport
errors, and non-2xx HTTP responses. The response body is printed when present.
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

require_tools() {
  command -v curl >/dev/null 2>&1 || die "curl is not installed"
  command -v python3 >/dev/null 2>&1 || die "python3 is not installed"
}

read_config() {
  local path="$1"
  python3 - "${path}" <<'PY'
import sys

try:
    import tomllib
except ModuleNotFoundError:
    print("dial.sh: Python 3.11 or later is required for TOML parsing", file=sys.stderr)
    raise SystemExit(2)

path = sys.argv[1]
try:
    with open(path, "rb") as handle:
        data = tomllib.load(handle)
except OSError as exc:
    print(f"dial.sh: cannot read {path}: {exc}", file=sys.stderr)
    raise SystemExit(1)
except tomllib.TOMLDecodeError as exc:
    print(f"dial.sh: invalid TOML in {path}: {exc}", file=sys.stderr)
    raise SystemExit(1)

dialer = data.get("dialer")
if not isinstance(dialer, dict):
    print(f"dial.sh: {path} has no [dialer] table", file=sys.stderr)
    raise SystemExit(1)

url = dialer.get("url", "")
token = dialer.get("token", "")
if not isinstance(url, str) or not isinstance(token, str):
    print(f"dial.sh: [dialer] url and token must be strings", file=sys.stderr)
    raise SystemExit(1)

print(url)
print(token)
PY
}

load_settings() {
  local config_output=""
  local -a config_lines=()
  local config_url=""
  local config_token=""

  URL="${TELEPHONE_AGENT_DIALER_URL:-${TELEPHONE_AGENT_DIALER__URL:-}}"
  TOKEN="${TELEPHONE_AGENT_DIALER_TOKEN:-${TELEPHONE_AGENT_DIALER__TOKEN:-}}"

  if [[ -n ${URL} && -n ${TOKEN} ]]; then
    return 0
  fi

  if [[ -z ${TELEPHONE_AGENT_CONFIG:-} ]]; then
    die "set TELEPHONE_AGENT_DIALER_URL and TELEPHONE_AGENT_DIALER_TOKEN, or set TELEPHONE_AGENT_CONFIG"
  fi
  [[ -r ${TELEPHONE_AGENT_CONFIG} ]] || die "cannot read config file: ${TELEPHONE_AGENT_CONFIG}"

  if ! config_output=$(read_config "${TELEPHONE_AGENT_CONFIG}"); then
    die "failed to read dialer settings from ${TELEPHONE_AGENT_CONFIG}"
  fi

  mapfile -t config_lines <<<"${config_output}"
  config_url="${config_lines[0]:-}"
  config_token="${config_lines[1]:-}"

  URL="${URL:-${config_url}}"
  TOKEN="${TOKEN:-${config_token}}"
}

validate_settings() {
  [[ -n ${URL} ]] || die "dialer URL is empty"
  [[ -n ${TOKEN} ]] || die "dialer token is empty"

  if [[ ! ${URL} =~ ^https?://[^[:space:]]+$ ]]; then
    die "dialer URL must start with http:// or https:// and contain no whitespace"
  fi
  if [[ ${TOKEN} == *$'\n'* || ${TOKEN} == *$'\r'* ]]; then
    die "dialer token must not contain a newline"
  fi

  URL="${URL%/}"
  if [[ ${URL} == */call ]]; then
    die "dialer URL must be the helper base URL, without /call"
  fi
}

validate_number() {
  if [[ ! ${NUMBER} =~ ${E164_REGEX} ]]; then
    die "number must be E.164, for example +91XXXXXXXXXX"
  fi
}

http_json() {
  local method="$1"
  local url="$2"
  local data="${3:-}"
  local response=""
  local status=""
  local body=""
  local -a args=(
    --silent
    --show-error
    --max-time "${DEFAULT_TIMEOUT}"
    --request "${method}"
    --header "Authorization: Bearer ${TOKEN}"
    --write-out $'\n%{http_code}'
  )

  if [[ ${method} == "POST" ]]; then
    args+=(--header "Content-Type: application/json" --data "${data}")
  fi

  if ! response=$(curl "${args[@]}" "${url}"); then
    printf 'error: request to %s failed\n' "${url}" >&2
    return 1
  fi

  status="${response##*$'\n'}"
  body="${response%$'\n'*}"

  printf '%s\n' "${body}"
  if [[ ! ${status} =~ ^2[0-9][0-9]$ ]]; then
    printf 'error: HTTP %s from %s\n' "${status}" "${url}" >&2
    return 1
  fi
}

call_dial() {
  local payload=""

  payload=$(printf '{"number":"%s"}' "${NUMBER}")
  http_json POST "${URL}/call" "${payload}"
}

call_health() {
  http_json GET "${URL}/health"
}

main() {
  if (($# == 0)); then
    usage >&2
    exit 2
  fi

  case "$1" in
    -h | --help)
      usage
      ;;
    --health)
      shift
      (($# == 0)) || usage_error "--health takes no arguments"
      require_tools
      load_settings
      validate_settings
      call_health || exit 1
      ;;
    -*)
      usage_error "unknown option: $1"
      ;;
    *)
      (($# == 1)) || usage_error "expected exactly one phone number"
      NUMBER="$1"
      require_tools
      validate_number
      load_settings
      validate_settings
      call_dial || exit 1
      ;;
  esac
}

main "$@"
