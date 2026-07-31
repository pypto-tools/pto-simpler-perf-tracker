#!/usr/bin/env bash
# Copyright (c) PyPTO Contributors.
# Parallel driver for perf_history.py: benchmark a set of commits across M
# NPUs concurrently, appending each commit's result to a shared md + JSONL as
# soon as it finishes (crash-safe).
#
# Standalone: operates on any target repo via --repo; task-submit lives ONLY
# here (box-specific), so perf_history.py stays portable.
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
REF="upstream/main"
SINCE=""
COMMIT_LIST=""
# Candidate card pool override. When non-empty (space/comma separated logical
# device ids, e.g. "3 4 5 6 8 9 10 11"), the run confines itself to these cards
# and forces M to their count. Empty = use every local NPU. Either way cards are
# picked per-round with explicit `--device N` and a wedged card is dropped from
# the pool for the rest of the run (see the card-switch loop below) — we never
# use `--device auto`, whose health view can be up to 12h stale.
DEVICES="${PERF_DEVICES:-}"
# task-submit time budget per shard (override via env). On a shared box, a
# short queue-wait makes a shard give up (= early termination) and drop its
# whole commit list when it can't grab a device in time — the failure mode that
# scattered earlier gaps. Wait patiently; never cap the actual run time.
TASK_TIMEOUT="${PERF_TASK_TIMEOUT:-21600}"   # queue-wait for a device (6h)
TASK_MAXTIME="${PERF_TASK_MAXTIME:-0}"       # run-time cap (0 = unlimited)
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --workdir) WORKDIR="$2"; shift 2 ;;
    -n) N="$2"; shift 2 ;;
    -m) M="$2"; shift 2 ;;
    -r) ROUNDS="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --since) SINCE="$2"; shift 2 ;;
    --commit-list) COMMIT_LIST="$2"; shift 2 ;;
    --devices) DEVICES="$2"; shift 2 ;;
    *) echo "unknown arg: $1"; exit 1 ;;
  esac
done

# Explicit device pinning: parse the id list and force M to its size so the
# round-robin sharding maps shard s -> DEV[s] one-to-one.
declare -a DEV=()
if [[ -n "${DEVICES// }" ]]; then
  read -r -a DEV <<< "${DEVICES//,/ }"
  M=${#DEV[@]}
fi

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
echo "PRs: ${#COMMITS[@]}  shards: $M  rounds: $ROUNDS  pin: ${PIN:0:10}${SINCE:+  since: $SINCE}"
if [[ ${#COMMITS[@]} -eq 0 ]]; then
  echo "no PRs selected; nothing to do."
  exit 0
fi

# Header + truncate JSONL once (shards only append sections).
{
  echo "# Perf history"
  echo
  echo "**ref** \`$REF\`${SINCE:+ · since \`$SINCE\`} · **rounds** $ROUNDS · **pto-isa** \`${PIN:0:10}\` · started $(date '+%Y-%m-%d %H:%M:%S')"
  echo
} > "$MD"
: > "$JSONL"

# ---------------------------------------------------------------------------
# Card selection + failure-driven card switching (no root, no --block).
#
# task-submit's `--device auto` can't be trusted to dodge a freshly-wedged
# card: its health probe runs at most every 12h and hard-exclusion (--block)
# needs sudo. So we drive card choice ourselves with plain `--device N` (a
# normal user CAN pin a card; it bypasses the stale health whitelist) plus our
# OWN in-memory bad-card set, populated empirically — a card that fails a
# benchmark this run is dropped from the remaining rounds and the unfinished
# commits are re-submitted on a fresh card.
#
# The bad set is per-run only, never persisted: every daily run starts clean,
# so a card that recovers (e.g. after a box reboot) is retried next run with no
# stale blacklist to clear.
# ---------------------------------------------------------------------------
MAXROUND="${PERF_MAX_ROUNDS:-4}"     # give up after this many card-switch rounds
MAXATTEMPT="${PERF_MAX_ATTEMPT:-3}"  # per-commit: fail on N cards => PR-broken, stop

# Candidate card pool: the explicit --devices/PERF_DEVICES list if given, else
# every local NPU. A wedged card is excluded via BAD (below), not from here.
if [[ ${#DEV[@]} -gt 0 ]]; then
  ALL_CARDS=("${DEV[@]}")
else
  ALL_CARDS=($(ls -1 /dev/davinci[0-9]* 2>/dev/null | sed 's#.*/davinci##' | sort -n))
fi
declare -A BAD=()   # card id -> 1 once a benchmark wedged on it this run

# Print up to $1 usable card ids: free ones (per task-submit --list) first so
# we rarely queue-wait, busy ones as fallback (--device N waits for the lock).
# Cards in BAD are hard-excluded.
pick_devices() {
  local need="$1" busy id free=() busyc=()
  busy=" $(task-submit --list 2>/dev/null \
            | grep -oE 'NPU:[0-9,]+' | sed 's/NPU://' | tr ',\n' '  ' || true) "
  for id in "${ALL_CARDS[@]}"; do
    [[ -n "${BAD[$id]:-}" ]] && continue
    if [[ "$busy" == *" $id "* ]]; then busyc+=("$id"); else free+=("$id"); fi
  done
  local ordered=("${free[@]}" "${busyc[@]}")
  # Emit nothing (not a blank line) when the pool is exhausted, so the caller's
  # `mapfile` yields an empty array rather than a one-element [""].
  (( ${#ordered[@]} )) && printf '%s\n' "${ordered[@]:0:$need}"
}

# shas that reached a good result (rc==0 with a captured summary) in the jsonl.
done_shas() {
  python3 - "$JSONL" <<'PY'
import json, sys
done = set()
for line in open(sys.argv[1]):
    if not line.strip():
        continue
    e = json.loads(line)
    if e.get("rc") == 0 and (e.get("summary") or "").strip():
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

  mapfile -t CAND < <(pick_devices "$M")
  if [[ ${#CAND[@]} -eq 0 ]]; then
    badlist="${!BAD[*]}"
    echo "round $round: no usable cards left (bad: ${badlist:-none});" \
         "leaving ${#remaining[@]} commit(s) unfinished."
    break
  fi
  K=${#CAND[@]}; (( K > ${#remaining[@]} )) && K=${#remaining[@]}
  echo "round $round/$MAXROUND: ${#remaining[@]} commit(s) over $K card(s)" \
       "[${CAND[*]:0:$K}]${BAD[*]:+  bad: ${!BAD[*]}}"

  # Round-robin remaining commits into K shards, one pinned card each.
  declare -a SHARD=() SHARD_DEV=()
  for i in "${!remaining[@]}"; do
    SHARD[$(( i % K ))]="${SHARD[$(( i % K ))]:-} ${remaining[$i]}"
  done

  pids=()
  for (( s=0; s<K; s++ )); do
    list="${SHARD[$s]:-}"; [[ -z "${list// }" ]] && continue
    dev="${CAND[$s]}"; SHARD_DEV[$s]="$dev"
    for sha in $list; do ATTEMPT[$sha]=$(( ${ATTEMPT[$sha]:-0} + 1 )); done
    echo "  shard $s -> card $dev: $(wc -w <<< "$list") commit(s)"
    # --timeout = queue-wait (TASK_TIMEOUT); --max-time = run cap (0 = none).
    task-submit --device "$dev" --timeout "$TASK_TIMEOUT" --max-time "$TASK_MAXTIME" \
      --run "python '$SCRIPT_DIR/perf_history.py' --repo '$REPO' --workdir '$WORKDIR' \
             --commit-list '$list' --rounds $ROUNDS --ref '$REF' \
             --append-md '$MD' --append-jsonl '$JSONL' \
             -o 'perf_shard_r${round}_${s}.json' --device \$TASK_DEVICE" \
      > "$WORKDIR/perf_shard_r${round}_${s}.log" 2>&1 &
    pids+=($!)
  done
  for p in "${pids[@]}"; do wait "$p" || true; done

  # Flag a card bad when its shard did not finish its tail: a mid-shard wedge
  # cascades to the end, so the last commit never completes. A card that ran its
  # whole list (even if an earlier commit failed for a PR-specific reason) is
  # left healthy — that commit just retries and hits the per-commit attempt cap.
  DONE=(); for s in $(done_shas); do DONE[$s]=1; done
  for (( s=0; s<K; s++ )); do
    list="${SHARD[$s]:-}"; [[ -z "${list// }" ]] && continue
    last=""; for sha in $list; do last="$sha"; done
    if [[ -z "${DONE[$last]:-}" ]]; then
      BAD["${SHARD_DEV[$s]}"]=1
      echo "  card ${SHARD_DEV[$s]} flagged bad (shard tail ${last:0:10} did not complete)"
    fi
  done
  unset SHARD SHARD_DEV
done

DONE=(); for s in $(done_shas); do DONE[$s]=1; done
ok=0; for c in "${COMMITS[@]}"; do [[ -n "${DONE[$c]:-}" ]] && ok=$(( ok + 1 )); done
echo "finished: $ok / ${#COMMITS[@]} commit(s) succeeded${BAD[*]:+  (bad cards this run: ${!BAD[*]})}"
echo "md    -> $MD"
echo "jsonl -> $JSONL"
# Always exit 0 so run.sh still finalizes + publishes the commits that did
# succeed; gaps are visible in the jsonl and Feishu dedups by sha.
exit 0
