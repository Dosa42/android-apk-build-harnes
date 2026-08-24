#!/usr/bin/env bash
set -Eeuo pipefail

usage() {
  cat <<'EOF'
Usage:
  prepare-sdk.sh --sdk-root PATH --doctor-report FILE --report FILE --accept-licenses

Installs only the exact Android SDK packages listed by project-doctor.py.
License acceptance requires both --accept-licenses and CI=true.
EOF
}

# Install only the exact Android SDK packages listed by project-doctor.py.
#
# Usage:
#   prepare-sdk.sh --sdk-root PATH --doctor-report FILE --report FILE --accept-licenses
#
# License acceptance is permitted only when both --accept-licenses is supplied
# and CI=true. The script never updates all SDK packages and never writes a
# target project's local.properties.

sdk_root="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}"
doctor_report=""
report=""
github_output="${GITHUB_OUTPUT:-}"
accept_licenses=false

while (($#)); do
  case "$1" in
    --sdk-root)
      sdk_root="${2:?missing value for --sdk-root}"
      shift 2
      ;;
    --doctor-report)
      doctor_report="${2:?missing value for --doctor-report}"
      shift 2
      ;;
    --report)
      report="${2:?missing value for --report}"
      shift 2
      ;;
    --github-output)
      github_output="${2:?missing value for --github-output}"
      shift 2
      ;;
    --accept-licenses)
      accept_licenses=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'ERROR: unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ -z "$sdk_root" ]]; then
  printf 'ERROR: Android SDK root is empty; pass --sdk-root or set ANDROID_HOME.\n' >&2
  exit 2
fi
if [[ -z "$doctor_report" || ! -f "$doctor_report" ]]; then
  printf 'ERROR: project-doctor report does not exist: %s\n' "$doctor_report" >&2
  exit 2
fi
if [[ -z "$report" ]]; then
  printf 'ERROR: --report is required.\n' >&2
  exit 2
fi

sdk_root="$(realpath -m "$sdk_root")"
doctor_report="$(realpath "$doctor_report")"
report="$(realpath -m "$report")"
mkdir -p "$sdk_root" "$(dirname "$report")"

sdkmanager_bin="$(command -v sdkmanager || true)"
if [[ -z "$sdkmanager_bin" ]]; then
  sdkmanager_bin="$(find "$sdk_root/cmdline-tools" -mindepth 3 -maxdepth 3 -type f -path '*/bin/sdkmanager' -print 2>/dev/null | sort -V | tail -n 1)"
fi
if [[ -z "$sdkmanager_bin" || ! -f "$sdkmanager_bin" ]]; then
  printf 'ERROR: sdkmanager was not found in PATH or %s/cmdline-tools.\n' "$sdk_root" >&2
  exit 2
fi

mapfile -t requested_packages < <(
  python3 - "$doctor_report" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    report = json.load(handle)
if report.get("status") != "ok":
    raise SystemExit("project-doctor report status is not ok")
packages = report.get("android_sdk", {}).get("packages", [])
if not isinstance(packages, list):
    raise SystemExit("android_sdk.packages must be a JSON list")
allowed = re.compile(
    r"^(?:platform-tools|platforms;android-[A-Za-z0-9_.-]+|build-tools;[0-9A-Za-z_.-]+|ndk;[0-9A-Za-z_.-]+|cmake;[0-9A-Za-z_.-]+)$"
)
for package in packages:
    if not isinstance(package, str) or not allowed.fullmatch(package):
        raise SystemExit(f"unsafe or unsupported SDK package in doctor report: {package!r}")
    print(package)
PY
)

if ((${#requested_packages[@]} == 0)); then
  printf 'ERROR: project-doctor produced no Android SDK packages.\n' >&2
  exit 2
fi

printf 'Android SDK preparation report\n' >"$report"
printf 'SDK root: %s\n' "$sdk_root" | tee -a "$report"
printf 'sdkmanager: %s\n' "$sdkmanager_bin" | tee -a "$report"
printf 'Doctor report: %s\n' "$doctor_report" | tee -a "$report"
printf 'Target local.properties written: false\n' | tee -a "$report"
printf 'Requested exact packages:\n' | tee -a "$report"
printf '  %s\n' "${requested_packages[@]}" | tee -a "$report"

installed_listing="$(mktemp "${RUNNER_TEMP:-/tmp}/android-sdk-installed-before.XXXXXX")"
after_listing="$(mktemp "${RUNNER_TEMP:-/tmp}/android-sdk-installed-after.XXXXXX")"
cleanup() {
  rm -f "$installed_listing" "$after_listing"
}
trap cleanup EXIT

if ! "$sdkmanager_bin" --sdk_root="$sdk_root" --list_installed >"$installed_listing" 2>&1; then
  printf 'ERROR: sdkmanager --list_installed failed. Output follows.\n' | tee -a "$report" >&2
  tee -a "$report" <"$installed_listing" >&2
  exit 3
fi

declare -A installed=()
while IFS= read -r line; do
  [[ "$line" == *"|"* ]] || continue
  package="${line%%|*}"
  package="${package#"${package%%[![:space:]]*}"}"
  package="${package%"${package##*[![:space:]]}"}"
  [[ -n "$package" && "$package" != "Path" ]] && installed["$package"]=1
done <"$installed_listing"

missing_packages=()
already_installed=()
for package in "${requested_packages[@]}"; do
  if [[ -n "${installed[$package]:-}" ]]; then
    already_installed+=("$package")
  else
    missing_packages+=("$package")
  fi
done

printf 'Already installed:\n' | tee -a "$report"
if ((${#already_installed[@]})); then
  printf '  %s\n' "${already_installed[@]}" | tee -a "$report"
else
  printf '  (none)\n' | tee -a "$report"
fi
printf 'Missing before preparation:\n' | tee -a "$report"
if ((${#missing_packages[@]})); then
  printf '  %s\n' "${missing_packages[@]}" | tee -a "$report"
else
  printf '  (none)\n' | tee -a "$report"
fi

if ((${#missing_packages[@]})); then
  if [[ "$accept_licenses" != true ]]; then
    printf 'ERROR: exact packages are missing and --accept-licenses was not supplied.\n' | tee -a "$report" >&2
    exit 4
  fi
  if [[ "${CI:-}" != "true" ]]; then
    printf 'ERROR: non-interactive Android SDK license acceptance is restricted to CI=true.\n' | tee -a "$report" >&2
    exit 4
  fi

  printf 'Accepting Android SDK licenses non-interactively for this CI setup.\n' | tee -a "$report"
  set +o pipefail
  yes | "$sdkmanager_bin" --sdk_root="$sdk_root" --licenses 2>&1 | tee -a "$report"
  pipeline_status=("${PIPESTATUS[@]}")
  set -o pipefail
  license_status="${pipeline_status[1]}"
  tee_status="${pipeline_status[2]}"
  if [[ "$license_status" -ne 0 || "$tee_status" -ne 0 ]]; then
    printf 'ERROR: Android SDK license acceptance failed (sdkmanager=%s, tee=%s).\n' "$license_status" "$tee_status" | tee -a "$report" >&2
    exit 4
  fi

  printf 'Installing exact missing SDK packages (no global update):\n' | tee -a "$report"
  printf '  %s\n' "${missing_packages[@]}" | tee -a "$report"
  "$sdkmanager_bin" --sdk_root="$sdk_root" "${missing_packages[@]}" 2>&1 | tee -a "$report"
fi

if ! "$sdkmanager_bin" --sdk_root="$sdk_root" --list_installed >"$after_listing" 2>&1; then
  printf 'ERROR: post-install sdkmanager --list_installed failed. Output follows.\n' | tee -a "$report" >&2
  tee -a "$report" <"$after_listing" >&2
  exit 5
fi

verification_failed=false
printf 'Post-install verification:\n' | tee -a "$report"
for package in "${requested_packages[@]}"; do
  if awk -F'|' -v wanted="$package" '
    {
      value=$1
      gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
      if (value == wanted) found=1
    }
    END { exit(found ? 0 : 1) }
  ' "$after_listing"; then
    printf '  PASS %s\n' "$package" | tee -a "$report"
  else
    printf '  FAIL %s\n' "$package" | tee -a "$report" >&2
    verification_failed=true
  fi
done
if [[ "$verification_failed" == true ]]; then
  printf 'ERROR: one or more exact Android SDK packages remain unavailable.\n' | tee -a "$report" >&2
  exit 5
fi

requested_json="$(printf '%s\n' "${requested_packages[@]}" | python3 -c 'import json,sys; print(json.dumps([line.rstrip("\n") for line in sys.stdin if line.rstrip("\n")], separators=(",", ":")))')"
installed_json="$(printf '%s\n' "${missing_packages[@]}" | python3 -c 'import json,sys; print(json.dumps([line.rstrip("\n") for line in sys.stdin if line.rstrip("\n")], separators=(",", ":")))')"
if [[ -n "$github_output" ]]; then
  {
    printf 'sdk-root=%s\n' "$sdk_root"
    printf 'requested-packages=%s\n' "$requested_json"
    printf 'installed-packages=%s\n' "$installed_json"
    printf 'report=%s\n' "$report"
  } >>"$github_output"
fi

printf 'Android SDK preparation complete. Report: %s\n' "$report"
