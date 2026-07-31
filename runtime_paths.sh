#!/usr/bin/env bash
# Resolve perf-tracker's mutable directories without depending on the caller's
# working directory or home directory.

if [[ -n "${PTO_TOOL_ROOT:-}" ]]; then
  TOOL_ROOT="$PTO_TOOL_ROOT"
elif [[ "$(basename "$SCRIPT_DIR")" == "app" ]]; then
  TOOL_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
else
  TOOL_ROOT="$SCRIPT_DIR/runtime"
fi

CONFIG_DIR="$TOOL_ROOT/config"
STATE_DIR="$TOOL_ROOT/state"
LOG_DIR="$TOOL_ROOT/logs"
TMP_DIR="$TOOL_ROOT/tmp"
CONFIG_FILE="${PTO_CONFIG_FILE:-$CONFIG_DIR/perf-tracker.env}"

mkdir -p "$CONFIG_DIR" "$STATE_DIR" "$LOG_DIR" "$TMP_DIR"
export TOOL_ROOT CONFIG_DIR STATE_DIR LOG_DIR TMP_DIR CONFIG_FILE
export TMPDIR="$TMP_DIR"

if [[ -f "$CONFIG_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$CONFIG_FILE"
  set +a
fi
