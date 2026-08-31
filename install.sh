#!/usr/bin/env bash
# Install/update Simpler Perf Tracker in the pypto-tools filesystem layout.
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS_ROOT="/home/pypto-tools"
BIN_DIR="/usr/local/bin"
TOOL_NAME="pto-simpler-perf-tracker"
LEGACY_TOOL_NAME="simpler-perf-tracker"
COMMAND_NAME="pto-simpler-perf-tracker"
INIT_CONFIG=0
RUN_USER="${SUDO_USER:-$(id -un)}"
RUN_GROUP="$(id -gn "$RUN_USER")"

usage() {
  cat <<'EOF'
Usage: ./install.sh [--tools-root DIR] [--init-config] [--bin-dir DIR]

Install the application as <tools-root>/pto-simpler-perf-tracker/app and expose
pto-simpler-perf-tracker in /usr/local/bin. Existing config and state are kept.

Options:
  --tools-root DIR  Installation root (default: /home/pypto-tools)
  --init-config    Create config/perf-tracker.env from the safe template if absent
  --bin-dir DIR     Command directory (default: /usr/local/bin; useful for packaging/tests)
  -h, --help       Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tools-root) TOOLS_ROOT="$2"; shift 2 ;;
    --init-config) INIT_CONFIG=1; shift ;;
    --bin-dir) BIN_DIR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

TOOL_ROOT="$TOOLS_ROOT/$TOOL_NAME"
LEGACY_TOOL_ROOT="$TOOLS_ROOT/$LEGACY_TOOL_NAME"
if [[ -d "$LEGACY_TOOL_ROOT" && ! -e "$TOOL_ROOT" ]]; then
  mv "$LEGACY_TOOL_ROOT" "$TOOL_ROOT"
  echo "migrated $LEGACY_TOOL_ROOT -> $TOOL_ROOT"
elif [[ -e "$LEGACY_TOOL_ROOT" && -e "$TOOL_ROOT" ]]; then
  echo "error: both legacy and canonical tool directories exist; merge them first" >&2
  exit 1
fi
APP_DIR="$TOOL_ROOT/app"
mkdir -p "$TOOL_ROOT" "$TOOL_ROOT/config" "$TOOL_ROOT/state" \
  "$TOOL_ROOT/logs" "$TOOL_ROOT/tmp" "$BIN_DIR"
TOOL_ROOT="$(cd "$TOOL_ROOT" && pwd)"
BIN_DIR="$(cd "$BIN_DIR" && pwd)"
APP_DIR="$TOOL_ROOT/app"
# Programs remain administrator-managed. Mutable directories belong to the
# user who invoked sudo so the public command never needs root at runtime.
chown "$RUN_USER:$RUN_GROUP" "$TOOL_ROOT/config" "$TOOL_ROOT/state" \
  "$TOOL_ROOT/logs" "$TOOL_ROOT/tmp"
chmod 0755 "$TOOLS_ROOT" "$TOOL_ROOT" "$TOOL_ROOT/config" \
  "$TOOL_ROOT/state" "$TOOL_ROOT/logs" "$TOOL_ROOT/tmp"

STAGE_DIR="$(mktemp -d "$TOOL_ROOT/.app.install.XXXXXX")"
cleanup() { rm -rf -- "$STAGE_DIR"; }
trap cleanup EXIT

for file in run.sh ci_weekly.sh backfill.sh catchup_once.sh perf_history_parallel.sh perf_history.py \
  perf_finalize.py feishu_perf_report.py ci_weekly_report.py notify_feishu.py nettime.py \
  scheduled_run.py runtime_paths.sh .env.example README.md; do
  install -m 0644 "$SOURCE_DIR/$file" "$STAGE_DIR/$file"
done
chmod 0755 "$STAGE_DIR/run.sh" "$STAGE_DIR/ci_weekly.sh" "$STAGE_DIR/backfill.sh" \
  "$STAGE_DIR/catchup_once.sh" \
  "$STAGE_DIR/perf_history_parallel.sh" "$STAGE_DIR/perf_history.py" \
  "$STAGE_DIR/perf_finalize.py" "$STAGE_DIR/feishu_perf_report.py" \
  "$STAGE_DIR/ci_weekly_report.py" \
  "$STAGE_DIR/notify_feishu.py" "$STAGE_DIR/nettime.py" \
  "$STAGE_DIR/scheduled_run.py"
chmod 0755 "$STAGE_DIR"

OLD_APP=""
if [[ -e "$APP_DIR" ]]; then
  OLD_APP="$TOOL_ROOT/.app.previous.$$"
  mv "$APP_DIR" "$OLD_APP"
fi
mv "$STAGE_DIR" "$APP_DIR"
trap - EXIT
if [[ -n "$OLD_APP" ]]; then rm -rf -- "$OLD_APP"; fi

if [[ "$INIT_CONFIG" -eq 1 && ! -e "$TOOL_ROOT/config/perf-tracker.env" ]]; then
  install -m 0600 "$APP_DIR/.env.example" "$TOOL_ROOT/config/perf-tracker.env"
  chown "$RUN_USER:$RUN_GROUP" "$TOOL_ROOT/config/perf-tracker.env"
fi

ln -sfn "$APP_DIR/run.sh" "$BIN_DIR/$COMMAND_NAME"
echo "installed $COMMAND_NAME -> $APP_DIR/run.sh"
