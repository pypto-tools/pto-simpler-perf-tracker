#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d)"
cleanup() { rm -rf -- "$TEST_ROOT"; }
trap cleanup EXIT

TOOLS_ROOT="$TEST_ROOT/tools"
BIN_DIR="$TEST_ROOT/bin"
"$REPO_DIR/install.sh" --tools-root "$TOOLS_ROOT" --bin-dir "$BIN_DIR" --init-config >/dev/null

TOOL_ROOT="$TOOLS_ROOT/simpler-perf-tracker"
for dir in app config state logs tmp; do
  [[ -d "$TOOL_ROOT/$dir" ]]
done
[[ -L "$BIN_DIR/pto-simpler-perf-tracker" ]]
[[ "$(readlink "$BIN_DIR/pto-simpler-perf-tracker")" == "$TOOL_ROOT/app/run.sh" ]]
[[ -x "$BIN_DIR/pto-simpler-perf-tracker" ]]
bash -n "$BIN_DIR/pto-simpler-perf-tracker"
[[ "$(find "$BIN_DIR" -mindepth 1 -maxdepth 1 | wc -l)" -eq 1 ]]

CONFIG="$TOOL_ROOT/config/perf-tracker.env"
printf 'PERF_TEST_MARKER=preserved\n' > "$CONFIG"
printf 'persistent-state\n' > "$TOOL_ROOT/state/sentinel"
printf 'stale-app-file\n' > "$TOOL_ROOT/app/stale"
"$REPO_DIR/install.sh" --tools-root "$TOOLS_ROOT" --bin-dir "$BIN_DIR" --init-config >/dev/null
grep -qx 'PERF_TEST_MARKER=preserved' "$CONFIG"
grep -qx 'persistent-state' "$TOOL_ROOT/state/sentinel"
[[ ! -e "$TOOL_ROOT/app/stale" ]]

installed_paths="$({ SCRIPT_DIR="$TOOL_ROOT/app"; source "$TOOL_ROOT/app/runtime_paths.sh"; printf '%s\n%s\n%s\n' "$CONFIG_DIR" "$STATE_DIR" "${PERF_TEST_MARKER:-}"; })"
[[ "$installed_paths" == "$TOOL_ROOT/config
$TOOL_ROOT/state
preserved" ]]

source_paths="$({ SCRIPT_DIR="$REPO_DIR"; source "$REPO_DIR/runtime_paths.sh"; printf '%s\n%s\n' "$CONFIG_DIR" "$STATE_DIR"; })"
[[ "$source_paths" == "$REPO_DIR/runtime/config
$REPO_DIR/runtime/state" ]]

echo "install/layout tests passed"
