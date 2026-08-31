#!/usr/bin/env bash
# Copyright (c) PyPTO Contributors.
# Parallel driver for perf_history.py: benchmark a set of commits across M
# NPUs concurrently, appending each commit's result to a shared md + JSONL as
# soon as it finishes (crash-safe).
#
# Standalone: operates on any target repo via --repo.  Local worker processes
# build commits in parallel; perf_history.py calls task-submit only when a
# built benchmark is ready to use its assigned card.
#
# Usage:
#   perf_history_parallel.sh --repo PATH [--workdir DIR] [-n N] [-m M]
#                            [-r ROUNDS] [--ref REF] [--since DATE]
#     --repo    target simpler clone (required)
#     --workdir outputs/worktrees dir (default <repo-parent>/perf_work)
#     -n        most-recent PRs to benchmark (default 100; ignored if --since)
#     --since   only PRs landed newer than this git date (daily-incremental mode)
#
# PRs are squash-merged, so each commit on main is exactly one PR's end state —
# selection is just plain `git log` over REF, newest PR first.
#     -m        parallel shards / devices (default 4)
#     -r        benchmark rounds per case (default 100)
#     --host-rounds N  HBG host rounds per case (default 6)
#     --host-case C    only measure this host case; repeat to select both
#     --host-only      skip the Device benchmark (quick validation)
#     --no-host        disable HBG host measurements
#     --ref     git ref to read commits from (default upstream/main)
#     --commit-list "<sha sha ...>"  explicit commits in order (overrides -n /
#               --since; used by backfill.sh, which selects PRs itself)
#
# Shared outputs (append-only): <workdir>/perf_history.md + .jsonl

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

REPO=""
WORKDIR=""
N=100
M=4
ROUNDS=100
HOST_ROUNDS=6
HOST_CASE_ARGS=()
NO_HOST=0
HOST_ONLY=0
REF="upstream/main"
SINCE=""
COMMIT_LIST=""
RESUME=0
# task-submit now wraps one benchmark only; worktree creation and compilation
# happen outside the NPU allocation.  PERF_TASK_TIMEOUT is retained as a
# backwards-compatible name for the per-benchmark hard limit used by the
# existing cron entry.  The client wait must be comfortably longer so it never
# abandons a live task and starts a colliding retry.
TASK_MAXTIME="${PERF_TASK_MAXTIME:-${PERF_TASK_TIMEOUT:-3600}}"
TASK_WAIT_TIMEOUT="${PERF_TASK_WAIT_TIMEOUT:-86400}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --workdir) WORKDIR="$2"; shift 2 ;;
    -n) N="$2"; shift 2 ;;
    -m) M="$2"; shift 2 ;;
    -r) ROUNDS="$2"; shift 2 ;;
    --host-rounds) HOST_ROUNDS="$2"; shift 2 ;;
    --host-case) HOST_CASE_ARGS+=(--host-case "$2"); shift 2 ;;
    --host-only) HOST_ONLY=1; shift ;;
    --no-host) NO_HOST=1; shift ;;
    --ref) REF="$2"; shift 2 ;;
    --since) SINCE="$2"; shift 2 ;;
    --commit-list) COMMIT_LIST="$2"; shift 2 ;;
    # Compatibility only. Device selection is always delegated to auto; use
    # -m to control concurrency.
    --devices) echo "WARN: --devices is ignored; using task-submit auto" >&2; shift 2 ;;
    --resume) RESUME=1; shift ;;
    *) echo "unknown arg: $1"; exit 1 ;;
  esac
done

[[ -n "$REPO" ]] || { echo "--repo is required"; exit 1; }
REPO="$(cd "$REPO" && pwd)"
[[ -d "$REPO/.git" ]] || { echo "--repo $REPO is not a git repo"; exit 1; }
[[ -n "$WORKDIR" ]] || WORKDIR="$(dirname "$REPO")/perf_work"
mkdir -p "$WORKDIR"
command -v task-submit >/dev/null || { echo "task-submit not found"; exit 1; }

MD="$WORKDIR/perf_history.md"
JSONL="$WORKDIR/perf_history.jsonl"

git -C "$REPO" fetch upstream main --quiet || true
PIN=$(grep -oP 'PTO_ISA_COMMIT:\s*\K[0-9a-f]+' "$REPO/.github/workflows/ci.yml" | head -1)

# Commit selection. Each commit on main is one squash-merged PR, so one commit
# == one PR's end state. --commit-list (explicit, verbatim order — used by the
# backfill driver) takes precedence; otherwise newest first: --since
# (incremental) else most-recent N PRs.
if [[ -n "$COMMIT_LIST" ]]; then
  read -r -a COMMITS <<< "$COMMIT_LIST"
elif [[ -n "$SINCE" ]]; then
  mapfile -t COMMITS < <(git -C "$REPO" log --since="$SINCE" --format=%H "$REF")
else
  mapfile -t COMMITS < <(git -C "$REPO" log -n "$N" --format=%H "$REF")
fi
echo "repo: $REPO"
echo "workdir: $WORKDIR"
echo "PRs: ${#COMMITS[@]}  shards: $M  rounds: $ROUNDS  host-rounds: $HOST_ROUNDS  pin: ${PIN:0:10}${SINCE:+  since: $SINCE}"
if [[ ${#COMMITS[@]} -eq 0 ]]; then
  echo "no PRs selected; nothing to do."
  exit 0
fi

# A fixed-range catch-up resumes strict successes from earlier attempts.  Daily
# overlapping-window runs retain the old truncate-on-start behaviour.
if [[ "$RESUME" -eq 1 ]]; then
  touch "$MD" "$JSONL"
  {
    echo
    echo "<!-- resumed $(date '+%Y-%m-%d %H:%M:%S') -->"
    echo
  } >> "$MD"
else
  {
    echo "# Perf history"
    echo
    echo "**ref** \`$REF\`${SINCE:+ · since \`$SINCE\`} · **rounds** $ROUNDS · **pto-isa** \`${PIN:0:10}\` · started $(date '+%Y-%m-%d %H:%M:%S')"
    echo
  } > "$MD"
  : > "$JSONL"
fi

# task-submit chooses an available card for every benchmark. The local worker
# count controls build/benchmark concurrency without pinning logical NPU ids.
MAXROUND="${PERF_MAX_ROUNDS:-4}"
MAXATTEMPT="${PERF_MAX_ATTEMPT:-3}"
PY_HOST_ARGS=(--host-rounds "$HOST_ROUNDS" "${HOST_CASE_ARGS[@]}")
if [[ "$NO_HOST" -eq 1 ]]; then PY_HOST_ARGS+=(--no-host); fi
if [[ "$HOST_ONLY" -eq 1 ]]; then PY_HOST_ARGS+=(--host-only); fi

# SHAs with a complete device result and, when present, complete host results.
done_shas() {
  python3 - "$JSONL" <<'PY'
import json, sys
done = set()
for line in open(sys.argv[1]):
    if not line.strip():
        continue
    e = json.loads(line)
    host = e.get("host")
    host_ok = host is None or host.get("status") in {
        "ok", "unsupported", "disabled"
    }
    device_measured = bool((e.get("summary") or "").strip())
    device_ok = (e.get("device_status") == "disabled"
                 or (e.get("rc") == 0 and device_measured))
    host_measured = host is not None and host.get("status") == "ok"
    if device_ok and host_ok and (device_measured or host_measured):
        done.add(e["sha"])
print(" ".join(sorted(done)))
PY
}

declare -A DONE=() ATTEMPT=()
for (( round=1; round<=MAXROUND; round++ )); do
  DONE=(); for s in $(done_shas); do DONE[$s]=1; done
  # remaining = not-done and not-exhausted commits, in newest-first order.
  remaining=()
  for c in "${COMMITS[@]}"; do
    [[ -n "${DONE[$c]:-}" ]] && continue
    [[ "${ATTEMPT[$c]:-0}" -ge "$MAXATTEMPT" ]] && continue
    remaining+=("$c")
  done
  [[ ${#remaining[@]} -eq 0 ]] && break

  K=$M; (( K > ${#remaining[@]} )) && K=${#remaining[@]}
  echo "round $round/$MAXROUND: ${#remaining[@]} commit(s) over $K card(s)" \
       "[task-submit auto]"

  # Keep adjacent commits together so most deltas compare measurements from
  # the same card. Cross-card boundaries are suppressed by perf_finalize.py.
  declare -a SHARD=()
  chunk=$(( (${#remaining[@]} + K - 1) / K ))
  for i in "${!remaining[@]}"; do
    s=$(( i / chunk ))
    SHARD[$s]="${SHARD[$s]:-} ${remaining[$i]}"
  done

  pids=()
  for (( s=0; s<K; s++ )); do
    list="${SHARD[$s]:-}"; [[ -z "${list// }" ]] && continue
    for sha in $list; do ATTEMPT[$sha]=$(( ${ATTEMPT[$sha]:-0} + 1 )); done
    echo "  shard $s -> auto: $(wc -w <<< "$list") commit(s)"
    # This worker builds locally; task-submit is invoked inside perf_history.py
    # only around each call to benchmark_rounds.sh.
    run_tag="$(date +%Y%m%d%H%M%S)-$$"
    python "$SCRIPT_DIR/perf_history.py" --repo "$REPO" --workdir "$WORKDIR" \
      --commit-list "$list" --rounds "$ROUNDS" --ref "$REF" \
      --append-md "$MD" --append-jsonl "$JSONL" \
      -o "perf_shard_${run_tag}_r${round}_${s}.json" --device auto \
      --task-submit --task-wait-timeout "$TASK_WAIT_TIMEOUT" \
      --task-max-time "$TASK_MAXTIME" "${PY_HOST_ARGS[@]}" \
      > "$WORKDIR/perf_shard_${run_tag}_r${round}_${s}.log" 2>&1 &
    pids+=($!)
  done
  for p in "${pids[@]}"; do wait "$p" || true; done

  unset SHARD
done

DONE=(); for s in $(done_shas); do DONE[$s]=1; done
ok=0; for c in "${COMMITS[@]}"; do [[ -n "${DONE[$c]:-}" ]] && ok=$(( ok + 1 )); done
echo "finished: $ok / ${#COMMITS[@]} commit(s) strictly succeeded"
echo "md    -> $MD"
echo "jsonl -> $JSONL"
# A selected batch with no strict success is a failed run, even though every
# individual attempt was recorded successfully.  run.sh deliberately captures
# this status so it can still finalize the diagnostics before returning the
# failure to scheduled_run.py.  Partial success remains publishable.
if (( ok == 0 )); then
  echo "ERROR: all ${#COMMITS[@]} selected commit(s) failed strict measurement" >&2
  exit 4
fi
exit 0
