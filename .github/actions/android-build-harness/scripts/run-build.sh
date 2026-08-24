#!/usr/bin/env bash

# Execute the Gradle version pinned by a target Android project's Wrapper
# properties without changing its source or build configuration. In GitHub
# Actions, ANDROID_HARNESS_GRADLE_COMMAND points to the matching official
# setup-gradle distribution, so target-owned Wrapper JAR code is not executed.
# Every task is selected from `tasks --all`; names are never guessed.

set +x
set -uo pipefail

usage() {
  cat <<'EOF'
Usage: run-build.sh --project PATH [options]

Options:
  --variant debug|release|both   Build type(s), default: debug
  --output-dir PATH             Harness output root
  --run-tests | --skip-tests    Run discovered JVM unit-test tasks (default: run)
  --run-lint | --skip-lint      Run discovered lint tasks (default: run)
  --bundle | --skip-bundle      Also run discovered bundle tasks (default: skip)
  --rerun-tasks | --no-rerun-tasks
                                 Regenerate packaging outputs (default: rerun)
  --gradle-arg VALUE            Additional non-secret Gradle argument; repeatable
  -h, --help                    Show this help

Release signing environment:
  ANDROID_RELEASE_KEYSTORE_BASE64, ANDROID_RELEASE_STORE_PASSWORD,
  ANDROID_RELEASE_KEY_PASSWORD, ANDROID_RELEASE_KEY_ALIAS

ANDROID_RELEASE_KEYSTORE may be used instead of the base64 variable when it
names an existing keystore file.  Optional custom debug signing accepts the
corresponding ANDROID_DEBUG_* variables.  Secrets are never placed in command
arguments or retained in reports.

ANDROID_HARNESS_GRADLE_COMMAND may point to a trusted executable installed at
the exact version from gradle-wrapper.properties. If unset, local use falls
back to the target Wrapper.
EOF
}

bool_value() {
  case "${1,,}" in
    1|true|yes|on) printf 'true\n' ;;
    0|false|no|off) printf 'false\n' ;;
    *) return 1 ;;
  esac
}

PROJECT="${PROJECT_ROOT:-$PWD}"
VARIANT="${BUILD_VARIANT:-debug}"
OUTPUT_DIR="${OUTPUT_ROOT:-}"
RUN_TESTS="${RUN_TESTS:-true}"
RUN_LINT="${RUN_LINT:-true}"
BUILD_BUNDLE="${BUILD_BUNDLE:-false}"
RERUN_TASKS="${ANDROID_HARNESS_RERUN_TASKS:-true}"
GRADLE_EXTRA_ARGS=()

while (($#)); do
  case "$1" in
    --project|--project-dir)
      (($# >= 2)) || { echo "Missing value for $1" >&2; exit 64; }
      PROJECT=$2; shift 2 ;;
    --variant)
      (($# >= 2)) || { echo "Missing value for --variant" >&2; exit 64; }
      VARIANT=${2,,}; shift 2 ;;
    --output-dir)
      (($# >= 2)) || { echo "Missing value for --output-dir" >&2; exit 64; }
      OUTPUT_DIR=$2; shift 2 ;;
    --run-tests)
      if (($# >= 2)) && [[ $2 != --* ]]; then RUN_TESTS=$(bool_value "$2") || { echo "Invalid boolean: $2" >&2; exit 64; }; shift 2; else RUN_TESTS=true; shift; fi ;;
    --skip-tests) RUN_TESTS=false; shift ;;
    --run-lint)
      if (($# >= 2)) && [[ $2 != --* ]]; then RUN_LINT=$(bool_value "$2") || { echo "Invalid boolean: $2" >&2; exit 64; }; shift 2; else RUN_LINT=true; shift; fi ;;
    --skip-lint) RUN_LINT=false; shift ;;
    --bundle)
      if (($# >= 2)) && [[ $2 != --* ]]; then BUILD_BUNDLE=$(bool_value "$2") || { echo "Invalid boolean: $2" >&2; exit 64; }; shift 2; else BUILD_BUNDLE=true; shift; fi ;;
    --skip-bundle) BUILD_BUNDLE=false; shift ;;
    --rerun-tasks) RERUN_TASKS=true; shift ;;
    --no-rerun-tasks) RERUN_TASKS=false; shift ;;
    --gradle-arg)
      (($# >= 2)) || { echo "Missing value for --gradle-arg" >&2; exit 64; }
      GRADLE_EXTRA_ARGS+=("$2"); shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 64 ;;
  esac
done

case "$VARIANT" in debug|release|both) ;; *) echo "Invalid variant: $VARIANT" >&2; exit 64 ;; esac
RUN_TESTS=$(bool_value "$RUN_TESTS") || { echo "Invalid RUN_TESTS value" >&2; exit 64; }
RUN_LINT=$(bool_value "$RUN_LINT") || { echo "Invalid RUN_LINT value" >&2; exit 64; }
BUILD_BUNDLE=$(bool_value "$BUILD_BUNDLE") || { echo "Invalid BUILD_BUNDLE value" >&2; exit 64; }
RERUN_TASKS=$(bool_value "$RERUN_TASKS") || { echo "Invalid ANDROID_HARNESS_RERUN_TASKS value" >&2; exit 64; }

PROJECT=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$PROJECT")
[[ -d "$PROJECT" ]] || { echo "Project directory does not exist: $PROJECT" >&2; exit 66; }
if [[ -z "$OUTPUT_DIR" ]]; then OUTPUT_DIR="$PROJECT/android-harness-output"; fi
OUTPUT_DIR=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$OUTPUT_DIR")

ARTIFACT_DIR="$OUTPUT_DIR/artifacts"
REPORT_DIR="$OUTPUT_DIR/reports"
LOG_DIR="$OUTPUT_DIR/logs"
mkdir -p "$ARTIFACT_DIR" "$REPORT_DIR" "$LOG_DIR"

RUN_STARTED_NS=$(python3 -c 'import time; print(time.time_ns())')
RUN_STARTED_UTC=$(python3 -c 'from datetime import datetime,timezone; print(datetime.now(timezone.utc).isoformat())')
printf '%s\n' "$RUN_STARTED_NS" > "$REPORT_DIR/run-start.epoch-ns"

RUNTIME_BASE=${RUNNER_TEMP:-${TMPDIR:-/tmp}}
RUNTIME_DIR=$(mktemp -d "$RUNTIME_BASE/android-build-harness.XXXXXX")
cleanup() {
  local rc=$?
  if [[ -n ${RUNTIME_DIR:-} && -d $RUNTIME_DIR ]]; then
    find "$RUNTIME_DIR" -type f -exec chmod u+w {} + 2>/dev/null || true
    rm -rf -- "$RUNTIME_DIR"
  fi
  return "$rc"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

STATUS_TSV="$RUNTIME_DIR/status.tsv"
SELECTED_TSV="$RUNTIME_DIR/selected.tsv"
: > "$STATUS_TSV"
: > "$SELECTED_TSV"

sanitize_file() {
  local source=$1 destination=$2
  python3 - "$source" "$destination" <<'PY'
import os
import sys

source, destination = sys.argv[1:]
with open(source, "rb") as handle:
    data = handle.read()
secret_names = []
if any(os.environ.get(name) for name in (
    "ANDROID_RELEASE_KEYSTORE_BASE64", "ANDROID_RELEASE_KEYSTORE",
    "ANDROID_RELEASE_STORE_PASSWORD", "ANDROID_RELEASE_KEY_PASSWORD",
)):
    secret_names.extend((
        "ANDROID_RELEASE_KEYSTORE_BASE64", "ANDROID_RELEASE_STORE_PASSWORD",
        "ANDROID_RELEASE_KEY_PASSWORD", "ANDROID_RELEASE_KEY_ALIAS",
    ))
if os.environ.get("ANDROID_DEBUG_KEYSTORE_BASE64") or os.environ.get("ANDROID_DEBUG_KEYSTORE"):
    secret_names.extend((
        "ANDROID_DEBUG_KEYSTORE_BASE64", "ANDROID_DEBUG_STORE_PASSWORD",
        "ANDROID_DEBUG_KEY_PASSWORD", "ANDROID_DEBUG_KEY_ALIAS",
    ))
for name in secret_names:
    value = os.environ.get(name, "")
    if value:
        data = data.replace(value.encode(), b"***")
with open(destination, "wb") as handle:
    handle.write(data)
PY
}

GRADLE_WRAPPER="$PROJECT/gradlew"
GRADLE_COMMAND=${ANDROID_HARNESS_GRADLE_COMMAND:-}
OVERALL=0
DISCOVERY_STATUS=not_run
RELEASE_SIGNING_STATUS=not_requested
DEBUG_SIGNING_STATUS=not_requested
BASE_GRADLE_HOME=${GRADLE_USER_HOME:-${HOME:-$RUNTIME_DIR}/.gradle}
RELEASE_GRADLE_HOME=""
DEBUG_GRADLE_HOME=""
KEYTOOL_BIN=${ANDROID_HARNESS_KEYTOOL:-}
if [[ -z $KEYTOOL_BIN ]]; then KEYTOOL_BIN=$(command -v keytool || true); fi

if [[ -n $GRADLE_COMMAND ]]; then
  GRADLE_COMMAND=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$GRADLE_COMMAND")
  if [[ ! -f $GRADLE_COMMAND || ! -x $GRADLE_COMMAND ]]; then
    echo "Trusted Gradle executable does not exist or is not executable: $GRADLE_COMMAND" >&2
    OVERALL=1
    DISCOVERY_STATUS=failed
  fi
elif [[ ! -f "$GRADLE_WRAPPER" ]]; then
  echo "Target project has no Gradle Wrapper at $GRADLE_WRAPPER" >&2
  OVERALL=1
  DISCOVERY_STATUS=failed
fi

run_wrapper_capture() {
  local label=$1 log_file=$2 gradle_home=$3
  shift 3
  local raw="$RUNTIME_DIR/raw-$(python3 -c 'import secrets; print(secrets.token_hex(8))').log"
  local rc
  echo "Running target-pinned Gradle: $label"
  if [[ -n $GRADLE_COMMAND ]]; then
    if (cd "$PROJECT" && GRADLE_USER_HOME="$gradle_home" "$GRADLE_COMMAND" "$@") >"$raw" 2>&1; then rc=0; else rc=$?; fi
  elif [[ -x "$GRADLE_WRAPPER" ]]; then
    if (cd "$PROJECT" && GRADLE_USER_HOME="$gradle_home" "$GRADLE_WRAPPER" "$@") >"$raw" 2>&1; then rc=0; else rc=$?; fi
  else
    if (cd "$PROJECT" && GRADLE_USER_HOME="$gradle_home" bash "$GRADLE_WRAPPER" "$@") >"$raw" 2>&1; then rc=0; else rc=$?; fi
  fi
  sanitize_file "$raw" "$log_file"
  rm -f -- "$raw"
  cat "$log_file"
  return "$rc"
}

TASKS_LOG="$LOG_DIR/gradle-tasks.log"
TASKS_REPORT="$REPORT_DIR/gradle-tasks.txt"
TASK_NAMES="$REPORT_DIR/gradle-task-names.txt"
if [[ $DISCOVERY_STATUS != failed ]]; then
  if run_wrapper_capture "discover tasks" "$TASKS_LOG" "$BASE_GRADLE_HOME" tasks --all --console=plain --no-daemon "${GRADLE_EXTRA_ARGS[@]}"; then
    cp "$TASKS_LOG" "$TASKS_REPORT"
    python3 - "$TASKS_REPORT" "$TASK_NAMES" <<'PY'
import re
import sys

line_re = re.compile(r"^\s*((?::?[A-Za-z0-9_.-]+:)*[A-Za-z0-9_.-]+)\s+-\s+")
tasks = set()
with open(sys.argv[1], encoding="utf-8", errors="replace") as handle:
    for line in handle:
        match = line_re.match(line)
        if match:
            tasks.add(match.group(1))
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    for task in sorted(tasks):
        handle.write(task + "\n")
PY
    if [[ -s "$TASK_NAMES" ]]; then
      DISCOVERY_STATUS=success
    else
      echo "Gradle task discovery returned no parseable tasks." >&2
      DISCOVERY_STATUS=failed
      OVERALL=1
    fi
  else
    rc=$?
    cp "$TASKS_LOG" "$TASKS_REPORT" 2>/dev/null || true
    echo "Gradle task discovery failed with exit code $rc." >&2
    DISCOVERY_STATUS=failed
    OVERALL=1
  fi
fi

prepare_keystore() {
  local kind=$1
  local upper=${kind^^}
  local base64_name="ANDROID_${upper}_KEYSTORE_BASE64"
  local path_name="ANDROID_${upper}_KEYSTORE"
  local store_name="ANDROID_${upper}_STORE_PASSWORD"
  local key_name="ANDROID_${upper}_KEY_PASSWORD"
  local alias_name="ANDROID_${upper}_KEY_ALIAS"
  local encoded=${!base64_name:-}
  local supplied_path=${!path_name:-}
  local store_password=${!store_name:-}
  local key_password=${!key_name:-}
  local key_alias=${!alias_name:-}
  local keystore="$RUNTIME_DIR/${kind}.keystore"
  local gradle_home="$RUNTIME_DIR/gradle-${kind}"
  local missing=()
  local generate_debug=false

  if [[ $kind == debug && -z $encoded && -z $supplied_path ]]; then
    generate_debug=true
  fi
  if [[ $kind == release && -z $encoded && -z $supplied_path ]]; then missing+=("$base64_name"); fi
  if [[ -z $store_password ]]; then
    if [[ $kind == debug ]]; then store_password=android; export ANDROID_DEBUG_STORE_PASSWORD=$store_password; else missing+=("$store_name"); fi
  fi
  if [[ -z $key_password ]]; then
    if [[ $kind == debug ]]; then key_password=android; export ANDROID_DEBUG_KEY_PASSWORD=$key_password; else missing+=("$key_name"); fi
  fi
  if [[ -z $key_alias ]]; then
    if [[ $kind == debug ]]; then key_alias=androiddebugkey; export ANDROID_DEBUG_KEY_ALIAS=$key_alias; else missing+=("$alias_name"); fi
  fi
  if ((${#missing[@]})); then
    printf '%s signing preflight: missing required environment variable(s): %s\n' "$kind" "${missing[*]}" >&2
    return 1
  fi

  if [[ $generate_debug == true ]]; then
    if [[ -z $KEYTOOL_BIN || ! -x $KEYTOOL_BIN ]]; then
      echo "debug signing preflight: keytool is unavailable in the selected JDK." >&2
      return 1
    fi
    if ! "$KEYTOOL_BIN" -genkeypair -noprompt -keystore "$keystore" -storetype JKS \
      -storepass:env "$store_name" -keypass:env "$key_name" -alias "$key_alias" \
      -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=Android Debug,O=Android,C=US" \
      >/dev/null 2>&1; then
      echo "debug signing preflight: temporary standard debug keystore generation failed." >&2
      return 1
    fi
  elif [[ -n $encoded ]]; then
    if ! python3 - "$keystore" "$base64_name" <<'PY'
import base64
import os
import sys

destination, variable = sys.argv[1:]
try:
    payload = "".join(os.environ[variable].split())
    decoded = base64.b64decode(payload, validate=True)
    if not decoded:
        raise ValueError("empty keystore")
    with open(destination, "wb") as handle:
        handle.write(decoded)
except Exception:
    raise SystemExit(1)
PY
    then
      echo "$kind signing preflight: keystore base64 is invalid or empty." >&2
      return 1
    fi
  else
    if [[ ! -f $supplied_path ]]; then
      echo "$kind signing preflight: configured keystore path is not a file." >&2
      return 1
    fi
    cp -- "$supplied_path" "$keystore"
  fi
  chmod 600 "$keystore"

  if [[ -z $KEYTOOL_BIN || ! -x $KEYTOOL_BIN ]]; then
    echo "$kind signing preflight: keytool is unavailable in the selected JDK." >&2
    return 1
  fi
  if ! "$KEYTOOL_BIN" -list -keystore "$keystore" -storepass:env "$store_name" -alias "$key_alias" >/dev/null 2>&1; then
    echo "$kind signing preflight: keystore, store password, or key alias validation failed." >&2
    return 1
  fi

  mkdir -p "$gradle_home"
  if [[ -d $BASE_GRADLE_HOME ]]; then
    for shared in caches wrapper jdks init.d; do
      if [[ -e $BASE_GRADLE_HOME/$shared ]]; then ln -s "$BASE_GRADLE_HOME/$shared" "$gradle_home/$shared"; fi
    done
    if [[ -f $BASE_GRADLE_HOME/gradle.properties ]]; then cp "$BASE_GRADLE_HOME/gradle.properties" "$gradle_home/gradle.properties"; fi
  fi
  python3 - "$gradle_home/gradle.properties" "$keystore" "$store_name" "$key_name" "$alias_name" <<'PY'
import os
import sys

path, keystore, store_name, key_name, alias_name = sys.argv[1:]
def escaped(value):
    value = value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return "".join(("\\" + char) if char in "=:#!" else char for char in value)

with open(path, "a", encoding="utf-8") as handle:
    handle.write("\n# Temporary Android Build Harness signing injection\n")
    handle.write("android.injected.signing.store.file=" + escaped(keystore) + "\n")
    handle.write("android.injected.signing.store.password=" + escaped(os.environ[store_name]) + "\n")
    handle.write("android.injected.signing.key.password=" + escaped(os.environ[key_name]) + "\n")
    handle.write("android.injected.signing.key.alias=" + escaped(os.environ[alias_name]) + "\n")
os.chmod(path, 0o600)
PY

  if [[ $kind == release ]]; then RELEASE_GRADLE_HOME=$gradle_home; else DEBUG_GRADLE_HOME=$gradle_home; fi
  return 0
}

if [[ $VARIANT == release || $VARIANT == both ]]; then
  if prepare_keystore release; then RELEASE_SIGNING_STATUS=ready; else RELEASE_SIGNING_STATUS=failed; OVERALL=1; fi
fi
if [[ $VARIANT == debug || $VARIANT == both ]]; then
  if prepare_keystore debug; then
    if [[ -n ${ANDROID_DEBUG_KEYSTORE_BASE64:-} || -n ${ANDROID_DEBUG_KEYSTORE:-} ]]; then
      DEBUG_SIGNING_STATUS=custom_ready
    else
      DEBUG_SIGNING_STATUS=generated_ready
    fi
  else
    DEBUG_SIGNING_STATUS=failed
    OVERALL=1
  fi
fi

if [[ $DISCOVERY_STATUS == success ]]; then
  python3 - "$TASK_NAMES" "$SELECTED_TSV" "$VARIANT" "$RUN_TESTS" "$RUN_LINT" "$BUILD_BUNDLE" "$REPORT_DIR/environment.json" <<'PY'
import json
import re
import sys

task_file, output_file, requested, run_tests, run_lint, bundle, environment_report = sys.argv[1:]
with open(task_file, encoding="utf-8") as handle:
    tasks = [line.strip() for line in handle if line.strip()]

# The preceding project doctor identifies actual com.android.application
# modules.  When its report is present, limit execution to the real tasks it
# attributed to those modules; otherwise retain the Wrapper-only fallback.
try:
    with open(environment_report, encoding="utf-8") as handle:
        environment = json.load(handle)
    task_report = environment.get("tasks", {})
    allowed = {
        item.lstrip(":")
        for category in ("assemble", "bundle", "lint", "test")
        for item in task_report.get(category, [])
    }
    if allowed:
        tasks = [task for task in tasks if task.lstrip(":") in allowed]
except (OSError, ValueError, TypeError):
    pass

def leaf(task):
    return task.rsplit(":", 1)[-1]

def choose_assemble(cap):
    def preferred(matches):
        scoped = sorted(task for task in matches if ":" in task)
        return scoped if scoped else sorted(matches)
    exact = [task for task in tasks if leaf(task) == f"assemble{cap}"]
    if exact:
        return preferred(exact)
    pattern = re.compile(rf"assemble[A-Za-z0-9_.-]*{cap}")
    return preferred([task for task in tasks if pattern.fullmatch(leaf(task))])

def counterpart(assemble_task, counterpart_leaf):
    prefix = assemble_task.rsplit(":", 1)[0] + ":" if ":" in assemble_task else ""
    candidate = prefix + counterpart_leaf
    return candidate if candidate in tasks else None

variants = [requested] if requested != "both" else ["debug", "release"]
rows = []
row_index = {}
for variant in variants:
    cap = variant.capitalize()
    assemble_tasks = choose_assemble(cap)
    selections = [("assemble", assemble_tasks)]
    tests = []
    lint = []
    bundles = []
    for assemble_task in assemble_tasks:
        suffix = leaf(assemble_task)[len("assemble"):]
        if run_tests == "true":
            task = counterpart(assemble_task, f"test{suffix}UnitTest") or counterpart(assemble_task, "test")
            if task:
                tests.append(task)
        if run_lint == "true":
            task = counterpart(assemble_task, f"lint{suffix}") or counterpart(assemble_task, "lint")
            if task:
                lint.append(task)
        if bundle == "true":
            task = counterpart(assemble_task, f"bundle{suffix}")
            if task:
                bundles.append(task)
    if run_tests == "true":
        selections.append(("test", tests))
    if run_lint == "true":
        selections.append(("lint", lint))
    if bundle == "true":
        selections.append(("bundle", bundles))
    for phase, selected in selections:
        for task in selected:
            key = (phase, task)
            if key not in row_index:
                row_index[key] = len(rows)
                rows.append((phase, variant, task))
            elif rows[row_index[key]][1] != variant:
                rows[row_index[key]] = (phase, "both", task)

with open(output_file, "w", encoding="utf-8") as handle:
    for row in rows:
        handle.write("\t".join(row) + "\n")
PY
fi

record_status() {
  local phase=$1 variant=$2 task=$3 status=$4 exit_code=$5 duration=$6 log=$7
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$phase" "$variant" "$task" "$status" "$exit_code" "$duration" "$log" >> "$STATUS_TSV"
}

has_selected() {
  local phase=$1 variant=$2
  awk -F '\t' -v p="$phase" -v v="$variant" '$1==p && ($2==v || $2=="both") {found=1} END {exit !found}' "$SELECTED_TSV"
}

if [[ $DISCOVERY_STATUS == success ]]; then
  variants=("$VARIANT")
  if [[ $VARIANT == both ]]; then variants=(debug release); fi
  for requested_variant in "${variants[@]}"; do
    if [[ $RUN_TESTS == true ]] && ! has_selected test "$requested_variant"; then
      record_status test "$requested_variant" "(no existing relevant task)" not_available 0 0 ""
    fi
    if [[ $RUN_LINT == true ]] && ! has_selected lint "$requested_variant"; then
      record_status lint "$requested_variant" "(no existing relevant task)" not_available 0 0 ""
    fi
    if ! has_selected assemble "$requested_variant"; then
      echo "No discovered assemble task exists for $requested_variant." >&2
      record_status assemble "$requested_variant" "(missing)" missing 1 0 ""
      OVERALL=1
    fi
    if [[ $BUILD_BUNDLE == true ]] && ! has_selected bundle "$requested_variant"; then
      echo "Bundle output was requested, but no discovered bundle task exists for $requested_variant." >&2
      record_status bundle "$requested_variant" "(missing)" missing 1 0 ""
      OVERALL=1
    fi
  done
fi

run_selected_phase() {
  local wanted_phase=$1
  while IFS=$'\t' read -r phase task_variant task; do
    [[ $phase == "$wanted_phase" ]] || continue
    if [[ $task_variant == release && ( $phase == assemble || $phase == bundle ) && $RELEASE_SIGNING_STATUS != ready ]]; then
      record_status "$phase" "$task_variant" "$task" blocked 1 0 ""
      continue
    fi
    if [[ $task_variant == debug && ( $phase == assemble || $phase == bundle ) && $DEBUG_SIGNING_STATUS == failed ]]; then
      record_status "$phase" "$task_variant" "$task" blocked 1 0 ""
      continue
    fi

    local safe_task=${task//:/__}
    safe_task=${safe_task//\//_}
    local log_file="$LOG_DIR/${phase}-${task_variant}-${safe_task}.log"
    local log_rel="logs/${phase}-${task_variant}-${safe_task}.log"
    local gradle_home=$BASE_GRADLE_HOME
    if [[ $task_variant == release && ( $phase == assemble || $phase == bundle ) ]]; then gradle_home=$RELEASE_GRADLE_HOME; fi
    if [[ $task_variant == debug && ( $phase == assemble || $phase == bundle ) && -n $DEBUG_GRADLE_HOME ]]; then gradle_home=$DEBUG_GRADLE_HOME; fi
    local extra=()
    if [[ ( $phase == assemble || $phase == bundle ) && $RERUN_TASKS == true ]]; then extra+=(--rerun-tasks); fi
    local started ended duration rc
    started=$(date +%s)
    if run_wrapper_capture "$task" "$log_file" "$gradle_home" "$task" --console=plain --stacktrace --no-daemon "${extra[@]}" "${GRADLE_EXTRA_ARGS[@]}"; then
      rc=0
      ended=$(date +%s); duration=$((ended-started))
      record_status "$phase" "$task_variant" "$task" success 0 "$duration" "$log_rel"
    else
      rc=$?
      ended=$(date +%s); duration=$((ended-started))
      record_status "$phase" "$task_variant" "$task" failed "$rc" "$duration" "$log_rel"
      OVERALL=1
    fi
  done < "$SELECTED_TSV"
}

# Checks are intentionally independent.  A failed test does not hide lint or a
# packaging result, but it remains an overall harness failure.
if [[ $DISCOVERY_STATUS == success ]]; then
  run_selected_phase test
  run_selected_phase lint
fi

BUILD_MARKER="$REPORT_DIR/build-start.marker"
touch "$BUILD_MARKER"
python3 - "$BUILD_MARKER" "$REPORT_DIR/build-start.epoch-ns" "$REPORT_DIR/build-start.epoch" <<'PY'
import os
import sys
value = str(os.stat(sys.argv[1]).st_mtime_ns) + "\n"
for destination in sys.argv[2:]:
    with open(destination, "w", encoding="utf-8") as handle:
        handle.write(value)
PY

if [[ $DISCOVERY_STATUS == success ]]; then
  run_selected_phase assemble
  if [[ $BUILD_BUNDLE == true ]]; then run_selected_phase bundle; fi
fi

CURRENT_ARTIFACTS="$REPORT_DIR/current-artifacts.txt"
python3 - "$PROJECT" "$BUILD_MARKER" "$CURRENT_ARTIFACTS" <<'PY'
import os
import pathlib
import sys

project = pathlib.Path(sys.argv[1]).resolve()
marker_ns = pathlib.Path(sys.argv[2]).stat().st_mtime_ns
found = []
for path in project.rglob("*"):
    try:
        if not path.is_file() or path.suffix.lower() not in {".apk", ".aab"}:
            continue
        relative = path.relative_to(project)
        parts = relative.parts
        if "build" not in parts or "outputs" not in parts:
            continue
        if path.stat().st_mtime_ns > marker_ns:
            found.append(path.resolve())
    except (OSError, ValueError):
        continue
with open(sys.argv[3], "w", encoding="utf-8") as handle:
    for path in sorted(found, key=str):
        handle.write(str(path) + "\n")
PY

RUN_FINISHED_UTC=$(python3 -c 'from datetime import datetime,timezone; print(datetime.now(timezone.utc).isoformat())')
SUMMARY_JSON="$REPORT_DIR/build-summary.json"
SUMMARY_MD="$REPORT_DIR/build-summary.md"
python3 - "$STATUS_TSV" "$SELECTED_TSV" "$CURRENT_ARTIFACTS" "$SUMMARY_JSON" "$SUMMARY_MD" \
  "$PROJECT" "$VARIANT" "$RUN_STARTED_UTC" "$RUN_FINISHED_UTC" "$OVERALL" "$DISCOVERY_STATUS" \
  "$RUN_TESTS" "$RUN_LINT" "$BUILD_BUNDLE" "$RERUN_TASKS" "$RELEASE_SIGNING_STATUS" "$DEBUG_SIGNING_STATUS" <<'PY'
import json
import os
import pathlib
import sys

(
    status_file, selected_file, artifacts_file, json_file, markdown_file,
    project, requested_variant, run_started, run_finished, overall,
    discovery_status, run_tests, run_lint, build_bundle, rerun_tasks,
    release_signing, debug_signing,
) = sys.argv[1:]
records = []
with open(status_file, encoding="utf-8") as handle:
    for line in handle:
        fields = line.rstrip("\n").split("\t")
        if len(fields) == 7:
            phase, variant, task, status, exit_code, duration, log = fields
            records.append({
                "phase": phase, "variant": variant, "task": task, "status": status,
                "exit_code": int(exit_code), "duration_seconds": int(duration),
                "log": log or None,
            })
selected = []
with open(selected_file, encoding="utf-8") as handle:
    for line in handle:
        fields = line.rstrip("\n").split("\t")
        if len(fields) == 3:
            selected.append({"phase": fields[0], "variant": fields[1], "task": fields[2]})
artifacts = []
with open(artifacts_file, encoding="utf-8") as handle:
    artifacts = [line.strip() for line in handle if line.strip()]
summary = {
    "schema_version": 1,
    "project": project,
    "requested_variant": requested_variant,
    "run_started_utc": run_started,
    "run_finished_utc": run_finished,
    "status": "success" if int(overall) == 0 else "failed",
    "discovery_status": discovery_status,
    "run_tests": run_tests == "true",
    "run_lint": run_lint == "true",
    "bundle_requested": build_bundle == "true",
    "rerun_packaging_tasks": rerun_tasks == "true",
    "release_signing_preflight": release_signing,
    "debug_signing_preflight": debug_signing,
    "selected_tasks": selected,
    "task_results": records,
    "current_artifact_sources": artifacts,
}
with open(json_file, "w", encoding="utf-8") as handle:
    json.dump(summary, handle, indent=2, sort_keys=True)
    handle.write("\n")
lines = [
    "# Android build summary", "",
    f"- Status: **{summary['status']}**",
    f"- Requested variant: `{summary['requested_variant']}`",
    f"- Gradle task discovery: `{summary['discovery_status']}`",
    f"- Release signing preflight: `{summary['release_signing_preflight']}`",
    f"- Current APK/AAB sources: {len(artifacts)}", "",
    "| Phase | Variant | Discovered task | Result | Exit | Log |",
    "| --- | --- | --- | --- | ---: | --- |",
]
for item in records:
    lines.append(f"| {item['phase']} | {item['variant']} | `{item['task']}` | {item['status']} | {item['exit_code']} | {item['log'] or ''} |")
if not records:
    lines.append("| — | — | — | no tasks executed | 1 | — |")
lines.extend(["", "Only task names reported by the target Gradle Wrapper were executed.", ""])
with open(markdown_file, "w", encoding="utf-8") as handle:
    handle.write("\n".join(lines))
PY

printf '%s\n' "$OVERALL" > "$REPORT_DIR/build-exit-code.txt"
echo "Build summary: $SUMMARY_JSON"
echo "Current artifact manifest: $CURRENT_ARTIFACTS"
exit "$OVERALL"
