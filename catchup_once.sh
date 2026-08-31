#!/usr/bin/env bash
# Benchmark and publish one fixed, closed commit range.  Publication happens
# only when every target commit has parseable metrics, so the network-window
# scheduler may safely retry this same one-shot after transient card failures.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FROM=""
THROUGH=""
WORKDIR=""
M=4
ROUNDS=100
while [[ $# -gt 0 ]]; do
  case "$1" in
    --from) FROM="$2"; shift 2 ;;
    --through) THROUGH="$2"; shift 2 ;;
    --workdir) WORKDIR="$2"; shift 2 ;;
    -m) M="$2"; shift 2 ;;
    -r) ROUNDS="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$FROM" && -n "$THROUGH" && -n "$WORKDIR" ]] || {
  echo "usage: catchup_once.sh --from SHA --through SHA --workdir DIR [-m N] [-r N]" >&2
  exit 2
}

REPO="$WORKDIR/simpler"
RAW="$WORKDIR/perf_history.jsonl"
PROCESSED="$WORKDIR/perf_history_processed.jsonl"
PROCESSED_MD="$WORKDIR/perf_history_processed.md"
CONFIG_FILE="${PTO_CONFIG_FILE:-/home/pypto-tools/pto-simpler-perf-tracker/config/perf-tracker.env}"
[[ -d "$REPO/.git" ]] || { echo "missing simpler clone: $REPO" >&2; exit 1; }
[[ -f "$CONFIG_FILE" ]] || { echo "missing config: $CONFIG_FILE" >&2; exit 1; }
if [[ -z "${PTO_ISA_ROOT:-}" && -n "${PERF_PTO_ISA_ROOT:-}" \
      && -d "$PERF_PTO_ISA_ROOT" ]]; then
  export PTO_ISA_ROOT="$PERF_PTO_ISA_ROOT"
fi
[[ -n "${PTO_ISA_ROOT:-}" ]] && echo "[catchup] PTO_ISA_ROOT=$PTO_ISA_ROOT"

git -C "$REPO" fetch upstream main --quiet
FROM="$(git -C "$REPO" rev-parse "$FROM^{commit}")"
THROUGH="$(git -C "$REPO" rev-parse "$THROUGH^{commit}")"
git -C "$REPO" merge-base --is-ancestor "$FROM" "$THROUGH" || {
  echo "$FROM is not an ancestor of $THROUGH" >&2
  exit 1
}

# Newest first, matching the daily driver's ordering.  Include the immediate
# parent only as the delta baseline; verification and the one-shot target do
# not include it.
mapfile -t TARGETS < <(git -C "$REPO" log --format=%H "$FROM^..$THROUGH")
BASE="$(git -C "$REPO" rev-parse "$FROM^")"
COMMITS=("${TARGETS[@]}" "$BASE")
echo "[catchup] fixed range: ${FROM:0:10}..${THROUGH:0:10}; ${#TARGETS[@]} targets + baseline"

bash "$SCRIPT_DIR/perf_history_parallel.sh" \
  --repo "$REPO" --workdir "$WORKDIR" -m "$M" -r "$ROUNDS" \
  --ref upstream/main --commit-list "${COMMITS[*]}" --resume

python "$SCRIPT_DIR/perf_finalize.py" --repo "$REPO" \
  --jsonl "$RAW" --out-jsonl "$PROCESSED" --out-md "$PROCESSED_MD" \
  --shard-glob "$WORKDIR/perf_shard_*.log" \
  --no-freeze-existing --no-carry-baseline

python3 - "$PROCESSED" "${TARGETS[@]}" <<'PY'
import json
import os
import re
import sys

processed, *targets = sys.argv[1:]
rows = {e["sha"]: e for e in (json.loads(line) for line in open(processed) if line.strip())}

def complete(entry):
    if not entry or entry.get("rc") != 0 or not entry.get("metrics"):
        return False
    summary = entry.get("summary") or ""
    match = re.search(
        r"Benchmark complete .*?:\s*(\d+) passed,\s*0 failed\s*\((\d+) total\)",
        summary,
    )
    if not match:
        # Older raw rows were written before the completion line was retained
        # in `summary`.  The active fixed range has eight cases; keep those
        # already-successful rows resumable while applying the stronger check
        # to every newly written row.
        return len(entry["metrics"]) == int(os.environ.get("PERF_EXPECTED_CASES", "8"))
    passed, total = map(int, match.groups())
    return passed == total == len(entry["metrics"])

missing = [sha for sha in targets if not complete(rows.get(sha))]
if missing:
    print(f"[catchup] incomplete: {len(missing)}/{len(targets)} target(s) lack metrics", file=sys.stderr)
    for sha in missing:
        print(f"  {sha}", file=sys.stderr)
    raise SystemExit(1)
print(f"[catchup] verified metrics for all {len(targets)} target commits")
PY

set -a
# shellcheck disable=SC1090
. "$CONFIG_FILE"
set +a
python "$SCRIPT_DIR/feishu_perf_report.py" \
  --from-processed "$PROCESSED" --publish \
  --state "$WORKDIR/feishu_state.json"
python "$SCRIPT_DIR/feishu_perf_report.py" \
  --from-processed "$PROCESSED" --publish --no-delta \
  --title-prefix "Simpler 性能实测值" \
  --state "$WORKDIR/feishu_state_raw.json"
echo "[catchup] one-shot range fully published"
