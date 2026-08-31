#!/usr/bin/env bash
# Copyright (c) PyPTO Contributors.
# Resumable historical backfill for perf-tracker.
#
# The forward daily cron (run.sh --since midnight) only covers PRs that land
# from now on. This driver fills in the BACK catalogue: every benchmarkable PR
# from the harness boundary (the commit that added tools/benchmark_rounds.sh)
# up to HEAD, newest-undone first, in batches, until the archive covers them
# all — then it exits fast (idempotent).
#
# Resumable by construction:
#   * archive.jsonl is the cumulative source of truth of which PRs are done;
#     each run recomputes "undone = benchmarkable PRs - archive" and continues.
#   * feishu_state.json["pushed"] dedups the Feishu side, so re-publishing the
#     whole archive each batch never duplicates a doc entry.
#   * flock -n makes overlapping invocations (e.g. an hourly safety-net cron)
#     no-ops while one instance is already running.
#
# It writes batch artifacts under <workdir>/backfill/ (its own git worktrees,
# separate from the forward run's <workdir>/perf_history.*) but SHARES
# <workdir>/feishu_state.json so it publishes into the same per-month docs.
#
# Pre-#227 PRs have no benchmark_rounds.sh and a different (or absent) summary
# format; they are intentionally EXCLUDED — see START_SHA below.
#
# Usage:
#   ./backfill.sh                       # continuous: churn all undone commits
#   ./backfill.sh --limit 100           # backfill only the next 100 commits
#   ./backfill.sh --limit 150 --rebuild # OVERWRITE: benchmark newest 150 fresh,
#                                       # clear the docs and rewrite in order
#   ./backfill.sh --until-sha <sha>     # only commits <sha> and OLDER (fence off
#                                       # commits already in the doc)
#   ./backfill.sh -m 4 -b 32 -r 100     # 4 NPUs, 32 commits/batch, 100 rounds
#   ./backfill.sh --no-push             # benchmark + archive only, skip Feishu
#   ./backfill.sh --once                # one batch then stop (debug)

set -euo pipefail
SCRIPT_PATH="${BASH_SOURCE[0]}"
while [[ -L "$SCRIPT_PATH" ]]; do
  LINK_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
  SCRIPT_PATH="$(readlink "$SCRIPT_PATH")"
  [[ "$SCRIPT_PATH" = /* ]] || SCRIPT_PATH="$LINK_DIR/$SCRIPT_PATH"
done
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
# shellcheck source=runtime_paths.sh
. "$SCRIPT_DIR/runtime_paths.sh"
REPO_URL="https://github.com/hw-native-sys/simpler"

# Harness boundary: 47fb6b68 = "Add benchmark script ... (#227)" first added
# tools/benchmark_rounds.sh. Everything older lacks the harness and is skipped.
START_SHA="47fb6b68"
REF="upstream/main"
UNTIL_SHA=""     # newest commit to include (upper bound); skip anything newer.
                 # Use it to fence off commits already in the doc. Empty = REF.
M=4
ROUNDS=100
BATCH=32
LIMIT=0          # 0 = no cap (churn every undone commit); else stop after N new
WORKDIR="$STATE_DIR/work"
PUSH=1
ONCE=0
REBUILD=0        # overwrite the doc from scratch (clear + rewrite, ordered)
while [[ $# -gt 0 ]]; do
  case "$1" in
    -m) M="$2"; shift 2 ;;
    --devices) echo "WARN: --devices is ignored; use -m with task-submit auto" >&2; shift 2 ;;
    -r) ROUNDS="$2"; shift 2 ;;
    -b|--batch) BATCH="$2"; shift 2 ;;
    --limit) LIMIT="$2"; shift 2 ;;
    --until-sha) UNTIL_SHA="$2"; shift 2 ;;
    --workdir) WORKDIR="$2"; shift 2 ;;
    --ref) REF="$2"; shift 2 ;;
    --start) START_SHA="$2"; shift 2 ;;
    --no-push) PUSH=0; shift ;;
    --once) ONCE=1; shift ;;
    --rebuild) REBUILD=1; shift ;;
    *) echo "unknown arg: $1"; exit 1 ;;
  esac
done

REPO="$WORKDIR/simpler"
BF="$WORKDIR/backfill"          # batch artifacts + worktrees (separate)
ARCHIVE="$BF/archive.jsonl"     # cumulative raw record (source of truth)
PROC_JSONL="$BF/archive_processed.jsonl"
PROC_MD="$BF/archive_processed.md"
STATE="$WORKDIR/feishu_state.json"   # SHARED with the forward run
# Commits ALREADY present in the Feishu doc (full shas, one per line), audited
# from the live doc. Treated as done: never re-benchmarked, never re-pushed.
# This is what makes the backfill correctly fill the INTERIOR gaps (commits the
# earlier push skipped) instead of only appending older commits.
EXCLUDE="$WORKDIR/doc_shas.txt"
LOCK="$WORKDIR/.backfill.lock"
mkdir -p "$BF"

# Single instance: a second (e.g. hourly safety-net) invocation just exits.
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "[backfill] another instance holds $LOCK; exiting."
  exit 0
fi

# 1. Clone/update target repo (same convention as run.sh).
if [[ -d "$REPO/.git" ]]; then
  git -C "$REPO" remote set-url upstream "$REPO_URL" 2>/dev/null \
    || git -C "$REPO" remote add upstream "$REPO_URL"
else
  git clone "$REPO_URL" "$REPO"
  git -C "$REPO" remote add upstream "$REPO_URL" 2>/dev/null || true
fi
git -C "$REPO" fetch upstream main --quiet

# Reuse an existing pto-isa clone (same fallback as run.sh).
if [[ ! -d "$REPO/build/pto-isa" && -z "${PTO_ISA_ROOT:-}" ]]; then
  for cand in "${PERF_PTO_ISA_ROOT:-}"; do
    if [[ -n "$cand" && -d "$cand" ]]; then export PTO_ISA_ROOT="$cand"; break; fi
  done
fi
[[ -n "${PTO_ISA_ROOT:-}" ]] && echo "[backfill] PTO_ISA_ROOT=$PTO_ISA_ROOT"

# Rebuild (overwrite) mode: start from a clean archive and ignore the doc-audit
# exclude, so the run benchmarks the full --limit set fresh; the final push then
# clears the existing docs and rewrites them in order.
if [[ "$REBUILD" -eq 1 ]]; then
  : > "$ARCHIVE"
  EXCLUDE=""
  echo "[backfill] REBUILD: fresh archive, doc-exclude ignored, will overwrite docs"
fi

touch "$ARCHIVE"

# Candidate commit list (newest first), the whole harness era [START_SHA..REF].
# What's already covered is excluded per-iteration via the doc audit (EXCLUDE)
# and the archive, so undone naturally includes the interior gaps. UNTIL_SHA is
# an optional extra upper bound (normally unset).
UPPER="${UNTIL_SHA:-$REF}"
mapfile -t ALL < <(git -C "$REPO" log --format=%H "${START_SHA}^..${UPPER}")
echo "[backfill] candidate commits [${START_SHA}..${UPPER}]: ${#ALL[@]}  -m=$M  batch=$BATCH"

iter=0
new_done=0          # commits actually ADDED to the archive this run (success-
                    # based, not attempt-based: a dead shard drops ~1/M of a
                    # batch, and those commits stay undone and get retried next
                    # iteration until --limit really land). This is exactly the
                    # failure that scattered the earlier run's interior gaps.
stall=0             # consecutive batches that added nothing -> give up
while :; do
  # Stop once this run has actually landed --limit new commits.
  if [[ "$LIMIT" -gt 0 && "$new_done" -ge "$LIMIT" ]]; then
    echo "[backfill] landed --limit $LIMIT new commits this run; stopping."
    break
  fi
  if [[ "$stall" -ge 3 ]]; then
    echo "[backfill] WARN: 3 batches added nothing (shards failing?); giving up."\
         "See $BF/perf_shard_*.log"
    break
  fi

  # done = shas already benchmarked (archive) OR already in the doc (EXCLUDE).
  declare -A DONE=()
  while read -r sha _; do [[ -n "$sha" ]] && DONE["$sha"]=1; done \
    < <(python3 -c "import json,sys;[print(json.loads(l)['sha']) for l in open('$ARCHIVE') if l.strip()]")
  if [[ -f "$EXCLUDE" ]]; then
    while read -r sha _; do [[ -n "$sha" ]] && DONE["$sha"]=1; done < "$EXCLUDE"
  fi

  # undone = ALL - DONE, newest first; take the next chunk (bounded by BATCH
  # and, if set, the remaining --limit budget for this run).
  cap=$BATCH
  [[ "$LIMIT" -gt 0 && $((LIMIT - new_done)) -lt $cap ]] && cap=$((LIMIT - new_done))
  CHUNK=()
  for sha in "${ALL[@]}"; do
    [[ -n "${DONE[$sha]:-}" ]] && continue
    CHUNK+=("$sha")
    [[ ${#CHUNK[@]} -ge $cap ]] && break
  done

  remaining=$(( ${#ALL[@]} - ${#DONE[@]} ))
  if [[ ${#CHUNK[@]} -eq 0 ]]; then
    echo "[backfill] all candidate commits done. nothing left."
    break
  fi
  iter=$((iter + 1))
  echo "[backfill] iter $iter: benchmarking ${#CHUNK[@]} commits"\
       "(landed $new_done/${LIMIT:-∞} this run, $remaining still missing)"

  before=$(wc -l < "$ARCHIVE")

  # 2. Benchmark this batch (writes <BF>/perf_history.{md,jsonl} fresh).
  bash "$SCRIPT_DIR/perf_history_parallel.sh" \
    --repo "$REPO" --workdir "$BF" -m "$M" -r "$ROUNDS" --ref "$REF" \
    --commit-list "${CHUNK[*]}"

  # 3. Append the batch to the cumulative archive (durable record).
  if [[ -s "$BF/perf_history.jsonl" ]]; then
    cat "$BF/perf_history.jsonl" >> "$ARCHIVE"
  else
    echo "[backfill] WARN: batch produced no records; see $BF/perf_shard_*.log"
  fi

  # Count only what actually landed; a dead shard's commits stay undone and are
  # retried next iteration. Stall guard breaks the loop if nothing lands.
  added=$(( $(wc -l < "$ARCHIVE") - before ))
  new_done=$(( new_done + added ))
  if [[ "$added" -eq 0 ]]; then stall=$((stall + 1)); else stall=0; fi
  echo "[backfill] iter $iter added $added commits (run total $new_done)"

  [[ "$ONCE" -eq 1 ]] && { echo "[backfill] --once: stopping after one batch."; break; }
done

# Finalize + publish ONCE, after all benchmarking is done. Doing it here (not
# per-batch) is what preserves commit order in the doc: shard-failure retries
# pull some newer commits into later batches, so a per-batch push would append
# them out of order. A single push over the fully-sorted archive appends this
# run's new commits at the month-doc bottoms strictly newest->oldest.
if [[ -s "$ARCHIVE" ]]; then
  # Finalize the WHOLE archive (global newest->oldest order + vs-previous deltas).
  python "$SCRIPT_DIR/perf_finalize.py" --repo "$REPO" \
    --jsonl "$ARCHIVE" --out-jsonl "$PROC_JSONL" --out-md "$PROC_MD" \
    --shard-glob "$BF/perf_shard_*.log"
  # Publish once. Normally --append (older commits to the bottom). In rebuild
  # mode --rebuild clears the docs and rewrites the whole sorted set in order.
  if [[ "$PUSH" -eq 1 ]]; then
    PUBLISH_MODE="--append"
    [[ "$REBUILD" -eq 1 ]] && PUBLISH_MODE="--rebuild"
    python "$SCRIPT_DIR/feishu_perf_report.py" \
      --from-processed "$PROC_JSONL" --publish $PUBLISH_MODE --state "$STATE"
  fi
fi

echo "[backfill] done. archive: $ARCHIVE  processed md: $PROC_MD"
