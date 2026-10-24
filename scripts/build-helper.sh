#!/usr/bin/env bash
#
# Build the android-helper APK with the project's Gradle wrapper and verify the
# result with the Android SDK build tools. Host-side script; it does not touch
# the phone.
#
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly SCRIPT_DIR
REPO_ROOT=$(cd -- "${SCRIPT_DIR}/.." && pwd -P)
readonly REPO_ROOT
readonly HELPER_DIR="${REPO_ROOT}/android-helper"
readonly GRADLEW="${HELPER_DIR}/gradlew"
readonly KEYSTORE_PROPERTIES="${HELPER_DIR}/keystore.properties"
readonly DEBUG_APK="${HELPER_DIR}/app/build/outputs/apk/debug/app-debug.apk"
readonly RELEASE_APK="${HELPER_DIR}/app/build/outputs/apk/release/app-release.apk"
readonly DEFAULT_JAVA_HOME="/usr/lib/jvm/java-21-openjdk-amd64"
readonly DEFAULT_ANDROID_HOME="/data/Android"
readonly DEFAULT_GRADLE_USER_HOME="/scratch/gradle"
readonly PREFERRED_BUILD_TOOLS="36.1.0"

MODE="auto"
CLEAN=0
FAILED=0
BUILD_TOOLS_DIR=""

usage() {
  cat <<'EOF'
Usage: build-helper.sh [--debug | --release] [--clean]

  (no option)  Build the debug APK, and the release APK when
               android-helper/keystore.properties exists.
  --debug      Build only the debug APK.
  --release    Build only the release APK. Requires keystore.properties.
  --clean      Run the Gradle clean task first.
  -h, --help   Show this help.

Environment variables are used when set; otherwise these defaults apply:
  JAVA_HOME          /usr/lib/jvm/java-21-openjdk-amd64
  ANDROID_HOME       /data/Android
  GRADLE_USER_HOME   /scratch/gradle

The script runs ./gradlew --no-daemon, then prints each APK's path, size,
SHA-256, apksigner verification, and an aapt2 badging summary. It exits
non-zero if the build fails or an APK cannot be verified.
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

resolve_environment() {
  if [[ -z ${JAVA_HOME:-} ]]; then
    JAVA_HOME="${DEFAULT_JAVA_HOME}"
  fi
  if [[ -z ${ANDROID_HOME:-} ]]; then
    ANDROID_HOME="${DEFAULT_ANDROID_HOME}"
  fi
  if [[ -z ${GRADLE_USER_HOME:-} ]]; then
    GRADLE_USER_HOME="${DEFAULT_GRADLE_USER_HOME}"
  fi

  export JAVA_HOME ANDROID_HOME GRADLE_USER_HOME
}

validate_java() {
  local version_output=""
  local version_line=""
  local major=0

  [[ -d ${JAVA_HOME} ]] || die "JAVA_HOME is not a directory: ${JAVA_HOME}"
  [[ -x ${JAVA_HOME}/bin/java ]] ||
    die "no executable java under JAVA_HOME: ${JAVA_HOME}/bin/java"

  if ! version_output=$("${JAVA_HOME}/bin/java" -version 2>&1); then
    die "failed to run ${JAVA_HOME}/bin/java -version"
  fi
  version_line="${version_output%%$'\n'*}"

  if [[ ${version_line} =~ \"1\.([0-9]+) ]]; then
    major="${BASH_REMATCH[1]}"
  elif [[ ${version_line} =~ \"([0-9]+) ]]; then
    major="${BASH_REMATCH[1]}"
  fi

  if ((major < 17)); then
    die "Java 17 or newer is required; found: ${version_line}"
  fi
  if ((major != 21)); then
    printf 'warning: Java %s detected; the project targets JDK 21\n' "${major}" >&2
  fi

  printf 'Java: %s\n' "${version_line}"
}

find_build_tools() {
  local preferred="${ANDROID_HOME}/build-tools/${PREFERRED_BUILD_TOOLS}"
  local candidate=""
  local version=""
  local -a versions=()

  if [[ -x ${preferred}/apksigner && -x ${preferred}/aapt2 ]]; then
    BUILD_TOOLS_DIR="${preferred}"
    return 0
  fi

  mapfile -t versions < <(
    find "${ANDROID_HOME}/build-tools" -mindepth 1 -maxdepth 1 \
      -type d -printf '%f\n' 2>/dev/null | sort -Vr
  )
  for version in "${versions[@]}"; do
    candidate="${ANDROID_HOME}/build-tools/${version}"
    if [[ -x ${candidate}/apksigner && -x ${candidate}/aapt2 ]]; then
      printf 'warning: build-tools %s not found; using %s\n' \
        "${PREFERRED_BUILD_TOOLS}" "${version}" >&2
      BUILD_TOOLS_DIR="${candidate}"
      return 0
    fi
  done

  die "no usable build-tools under ${ANDROID_HOME}/build-tools (need apksigner and aapt2)"
}

validate_sdk() {
  [[ -d ${ANDROID_HOME} ]] || die "ANDROID_HOME is not a directory: ${ANDROID_HOME}"
  [[ -d ${ANDROID_HOME}/build-tools ]] ||
    die "missing Android build-tools under ${ANDROID_HOME}"
  find_build_tools
  printf 'Android SDK: %s\n' "${ANDROID_HOME}"
  printf 'Build tools: %s\n' "${BUILD_TOOLS_DIR}"
}

validate_wrapper() {
  [[ -d ${HELPER_DIR} ]] || die "missing android-helper directory: ${HELPER_DIR}"
  [[ -f ${GRADLEW} ]] || die "missing Gradle wrapper: ${GRADLEW}"
  [[ -x ${GRADLEW} ]] ||
    die "Gradle wrapper is not executable; run: chmod +x ${GRADLEW}"
}

validate_gradle_home() {
  if [[ ! -d ${GRADLE_USER_HOME} ]]; then
    mkdir -p -- "${GRADLE_USER_HOME}" ||
      die "cannot create GRADLE_USER_HOME: ${GRADLE_USER_HOME}"
  fi
  [[ -w ${GRADLE_USER_HOME} ]] ||
    die "GRADLE_USER_HOME is not writable: ${GRADLE_USER_HOME}"
  printf 'Gradle user home: %s\n' "${GRADLE_USER_HOME}"
}

format_size() {
  local bytes="$1"

  if command -v numfmt >/dev/null 2>&1; then
    numfmt --to=iec-i --suffix=B "${bytes}" 2>/dev/null || printf '%s bytes' "${bytes}"
  else
    printf '%s bytes' "${bytes}"
  fi
}

report_apk() {
  local apk="$1"
  local bytes=""
  local size=""
  local digest=""
  local verify_output=""
  local badging=""

  printf '\nAPK: %s\n' "${apk}"
  bytes=$(stat -c '%s' -- "${apk}")
  size=$(format_size "${bytes}")
  printf '  size: %s (%s bytes)\n' "${size}" "${bytes}"

  digest=$(sha256sum -- "${apk}")
  digest="${digest%% *}"
  printf '  sha256: %s\n' "${digest}"

  if verify_output=$("${BUILD_TOOLS_DIR}/apksigner" verify --verbose "${apk}" 2>&1); then
    printf '  apksigner: verified\n'
    printf '%s\n' "${verify_output}" | sed 's/^/    /'
  else
    printf '  apksigner: FAILED\n' >&2
    printf '%s\n' "${verify_output}" | sed 's/^/    /' >&2
    FAILED=1
  fi

  if badging=$("${BUILD_TOOLS_DIR}/aapt2" dump badging "${apk}" 2>&1); then
    printf '  badging:\n'
    printf '%s\n' "${badging}" |
      grep -E '^(package|sdkVersion|targetSdkVersion|application-label|launchable-activity):' |
      sed 's/^/    /' || true
  else
    printf '  badging: FAILED\n' >&2
    printf '%s\n' "${badging}" | sed 's/^/    /' >&2
    FAILED=1
  fi
}

build() {
  local -a tasks=()
  local -a apks=()
  local build_release=0
  local debug_requested=0
  local apk=""

  if ((CLEAN)); then
    tasks+=(clean)
  fi

  case "${MODE}" in
    debug)
      debug_requested=1
      tasks+=(assembleDebug)
      ;;
    release)
      [[ -f ${KEYSTORE_PROPERTIES} ]] ||
        die "--release requires ${KEYSTORE_PROPERTIES} (not found)"
      build_release=1
      tasks+=(assembleRelease)
      ;;
    auto)
      debug_requested=1
      tasks+=(assembleDebug)
      if [[ -f ${KEYSTORE_PROPERTIES} ]]; then
        build_release=1
        tasks+=(assembleRelease)
        printf 'Release signing: %s found; building release too\n' \
          "${KEYSTORE_PROPERTIES}"
      else
        printf 'Release signing: %s not found; skipping release build\n' \
          "${KEYSTORE_PROPERTIES}"
      fi
      ;;
    *)
      die "internal error: unknown build mode: ${MODE}"
      ;;
  esac

  printf '\nRunning: ./gradlew --no-daemon %s\n\n' "${tasks[*]}"
  if ! (cd -- "${HELPER_DIR}" && ./gradlew --no-daemon "${tasks[@]}"); then
    die "Gradle build failed"
  fi

  if ((debug_requested)); then
    apks+=("${DEBUG_APK}")
  fi
  if ((build_release)); then
    apks+=("${RELEASE_APK}")
  fi

  printf '\nBuilt artifacts:\n'
  for apk in "${apks[@]}"; do
    if [[ ! -f ${apk} ]]; then
      printf 'error: expected APK not found: %s\n' "${apk}" >&2
      FAILED=1
      continue
    fi
    report_apk "${apk}"
  done

  if ((FAILED)); then
    printf '\nerror: build completed with verification failures\n' >&2
    exit 1
  fi

  printf '\nBuild and verification succeeded.\n'
}

main() {
  while (($# > 0)); do
    case "$1" in
      --debug)
        [[ ${MODE} == "auto" ]] ||
          usage_error "choose only one of --debug and --release"
        MODE="debug"
        shift
        ;;
      --release)
        [[ ${MODE} == "auto" ]] ||
          usage_error "choose only one of --debug and --release"
        MODE="release"
        shift
        ;;
      --clean)
        CLEAN=1
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

  resolve_environment
  validate_java
  validate_sdk
  validate_wrapper
  validate_gradle_home
  build
}

main "$@"
