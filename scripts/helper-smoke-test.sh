#!/usr/bin/env bash
#
# Smoke-test the android-helper endpoint from the host. The default path checks
# health and confirms that an invalid bearer token is rejected; it never places
# a call. A real call requires an explicit --call argument.
#
set -euo pipefail

readonly TIMEOUT=15
readonly E164_REGEX='^\+[1-9][0-9]{6,14}$'
readonly WRONG_TOKEN="smoke-test-invalid-token"

URL_ARG=""
TOKEN_FILE=""
CALL_NUMBER=""
DO_HANGUP=0
URL=""
TOKEN=""
FAILURES=0
HTTP_STATUS=""
HTTP_BODY=""
HEALTH_OK=0

usage() {
  cat <<'EOF'
Usage: helper-smoke-test.sh [--url URL] [--token-file PATH] [--call +E164] [--hangup]

  --url URL          Helper base URL, for example http://192.168.1.50:8765.
                     Falls back to TELEPHONE_AGENT_DIALER_URL.
  --token-file PATH  Read the bearer token from PATH. Falls back to
                     TELEPHONE_AGENT_DIALER_TOKEN, then
                     TELEPHONE_AGENT_DIALER__TOKEN.
  --call +E164       POST a real call after the health and auth checks. Only
                     use this when you intend to place a call.
  --hangup           POST /hangup. Requires a token.
  -h, --help         Show this help.

Without --call or --hangup, the script performs only safe checks:

  1. GET /health and verify HTTP 2xx plus "status":"ok" and a "device" field.
  2. POST /call with a deliberately invalid token and assert HTTP 401.

The token is optional for those safe checks. It is required for --call and
--hangup, and is never printed. Exit status is 1 when a check fails and 2 for
usage errors.
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

pass() {
  printf 'PASS: %s\n' "$*"
}

fail() {
  printf 'FAIL: %s\n' "$*" >&2
  FAILURES=$((FAILURES + 1))
}

read_token_file() {
  local path="$1"
  local raw=""

  [[ -r ${path} ]] || die "cannot read token file: ${path}"
  raw=$(<"${path}")
  TOKEN="${raw%$'\r'}"

  if [[ -z ${TOKEN} ]]; then
    die "token file is empty: ${path}"
  fi
  if [[ ${TOKEN} == *$'\n'* || ${TOKEN} == *$'\r'* ]]; then
    die "token file must contain a single line: ${path}"
  fi
}

load_settings() {
  URL="${URL_ARG:-${TELEPHONE_AGENT_DIALER_URL:-}}"
  [[ -n ${URL} ]] ||
    die "set --url URL or TELEPHONE_AGENT_DIALER_URL"

  if [[ ! ${URL} =~ ^https?://[^[:space:]]+$ ]]; then
    die "helper URL must start with http:// or https:// and contain no whitespace"
  fi
  URL="${URL%/}"
  if [[ ${URL} == */call || ${URL} == */hangup || ${URL} == */health ]]; then
    die "helper URL must be the base URL, without /call, /hangup, or /health"
  fi

  if [[ -n ${TOKEN_FILE} ]]; then
    read_token_file "${TOKEN_FILE}"
  else
    TOKEN="${TELEPHONE_AGENT_DIALER_TOKEN:-${TELEPHONE_AGENT_DIALER__TOKEN:-}}"
    TOKEN="${TOKEN%$'\r'}"
  fi
  if [[ ${TOKEN} == *$'\n'* || ${TOKEN} == *$'\r'* ]]; then
    die "token must not contain a newline"
  fi

  if [[ -n ${CALL_NUMBER} && ! ${CALL_NUMBER} =~ ${E164_REGEX} ]]; then
    die "--call must be E.164, for example +91XXXXXXXXXX"
  fi

  if [[ -n ${CALL_NUMBER} || ${DO_HANGUP} -eq 1 ]]; then
    [[ -n ${TOKEN} ]] ||
      die "a token is required for --call and --hangup; use --token-file or TELEPHONE_AGENT_DIALER_TOKEN"
  fi
}

http_request() {
  local method="$1"
  local url="$2"
  local token="$3"
  local data="${4:-}"
  local response=""
  local -a args=(
    --silent
    --show-error
    --max-time "${TIMEOUT}"
    --request "${method}"
    --write-out $'\n%{http_code}'
  )

  if [[ -n ${token} ]]; then
    args+=(--header "Authorization: Bearer ${token}")
  fi
  if [[ -n ${data} ]]; then
    args+=(--header "Content-Type: application/json" --data "${data}")
  fi

  HTTP_STATUS=""
  HTTP_BODY=""
  if ! response=$(curl "${args[@]}" "${url}"); then
    return 1
  fi

  HTTP_STATUS="${response##*$'\n'}"
  HTTP_BODY="${response%$'\n'*}"
  return 0
}

print_http() {
  printf '  HTTP %s\n' "${HTTP_STATUS}"
  if [[ -n ${HTTP_BODY} ]]; then
    printf '%s\n' "${HTTP_BODY}" | sed 's/^/  /'
  fi
}

is_2xx() {
  [[ ${HTTP_STATUS} =~ ^2[0-9][0-9]$ ]]
}

validate_health_body() {
  local body="$1"

  if command -v python3 >/dev/null 2>&1; then
    python3 -c '
import json
import sys

try:
    data = json.load(sys.stdin)
except json.JSONDecodeError:
    raise SystemExit(1)

if data.get("status") != "ok" or "device" not in data:
    raise SystemExit(1)
' <<<"${body}" 2>/dev/null
  else
    grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"' <<<"${body}" &&
      grep -Eq '"device"[[:space:]]*:' <<<"${body}"
  fi
}

check_health() {
  printf '\nGET %s/health\n' "${URL}"
  if ! http_request GET "${URL}/health" ""; then
    fail "health request failed (connection, DNS, or timeout)"
    return 0
  fi

  print_http
  if ! is_2xx; then
    fail "health check expected HTTP 2xx, got ${HTTP_STATUS}"
    return 0
  fi
  if ! validate_health_body "${HTTP_BODY}"; then
    fail 'health response is not JSON with "status":"ok" and a "device" field'
    return 0
  fi

  HEALTH_OK=1
  pass "health response has status=ok and a device field"
}

check_invalid_token() {
  local wrong_token="${WRONG_TOKEN}"
  local payload='{"number":"not-a-phone-number"}'

  if [[ -n ${TOKEN} && ${TOKEN} == "${WRONG_TOKEN}" ]]; then
    wrong_token="${WRONG_TOKEN}-2"
  fi

  printf '\nPOST %s/call with an invalid token (expect HTTP 401)\n' "${URL}"
  if ! http_request POST "${URL}/call" "${wrong_token}" "${payload}"; then
    fail "invalid-token request failed (connection, DNS, or timeout)"
    return 0
  fi

  print_http
  if [[ ${HTTP_STATUS} == "401" ]]; then
    pass "invalid token rejected with HTTP 401"
  else
    fail "invalid token expected HTTP 401, got ${HTTP_STATUS}"
  fi
}

check_call() {
  local payload=""

  printf '\nPOST %s/call for %s\n' "${URL}" "${CALL_NUMBER}"
  payload=$(printf '{"number":"%s"}' "${CALL_NUMBER}")
  if ! http_request POST "${URL}/call" "${TOKEN}" "${payload}"; then
    fail "call request failed (connection, DNS, or timeout)"
    return 0
  fi

  print_http
  if is_2xx; then
    pass "call request accepted"
  else
    fail "call request expected HTTP 2xx, got ${HTTP_STATUS}"
  fi
}

check_hangup() {
  printf '\nPOST %s/hangup\n' "${URL}"
  if ! http_request POST "${URL}/hangup" "${TOKEN}" '{}'; then
    fail "hangup request failed (connection, DNS, or timeout)"
    return 0
  fi

  print_http
  if is_2xx; then
    pass "hangup request accepted"
  else
    fail "hangup request expected HTTP 2xx, got ${HTTP_STATUS}"
  fi
}

main() {
  while (($# > 0)); do
    case "$1" in
      --url)
        (($# >= 2)) || usage_error "--url requires a URL"
        [[ -n $2 ]] || usage_error "--url requires a non-empty URL"
        URL_ARG="$2"
        shift 2
        ;;
      --token-file)
        (($# >= 2)) || usage_error "--token-file requires a path"
        [[ -n $2 ]] || usage_error "--token-file requires a non-empty path"
        TOKEN_FILE="$2"
        shift 2
        ;;
      --call)
        (($# >= 2)) || usage_error "--call requires an E.164 number"
        [[ -n $2 ]] || usage_error "--call requires a non-empty number"
        CALL_NUMBER="$2"
        shift 2
        ;;
      --hangup)
        DO_HANGUP=1
        shift
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

  command -v curl >/dev/null 2>&1 || die "curl is not installed"
  load_settings

  check_health
  check_invalid_token

  if [[ -n ${CALL_NUMBER} ]]; then
    if ((HEALTH_OK)); then
      check_call
    else
      printf '\nSKIP: call not attempted because /health did not pass\n'
    fi
  fi

  if ((DO_HANGUP)); then
    check_hangup
  fi

  if ((FAILURES > 0)); then
    printf '\n%d check(s) failed.\n' "${FAILURES}" >&2
    exit 1
  fi

  printf '\nAll checks passed.\n'
}

main "$@"
