#!/usr/bin/env bash
# Load the tracker's private config and run the independent GitHub CI reporter.
set -euo pipefail

SCRIPT_PATH="${BASH_SOURCE[0]}"
while [[ -L "$SCRIPT_PATH" ]]; do
  LINK_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
  SCRIPT_PATH="$(readlink "$SCRIPT_PATH")"
  [[ "$SCRIPT_PATH" = /* ]] || SCRIPT_PATH="$LINK_DIR/$SCRIPT_PATH"
done
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
# Source checkouts historically keep the existing Feishu channel in .env.
# Installed deployments continue to use config/perf-tracker.env.
if [[ "$(basename "$SCRIPT_DIR")" != "app" && -z "${PTO_CONFIG_FILE:-}" \
      && -f "$SCRIPT_DIR/.env" ]]; then
  export PTO_CONFIG_FILE="$SCRIPT_DIR/.env"
fi
# shellcheck source=runtime_paths.sh
. "$SCRIPT_DIR/runtime_paths.sh"

exec python3 "$SCRIPT_DIR/ci_weekly_report.py" "$@"
