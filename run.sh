#!/usr/bin/env bash
# Copyright (c) PyPTO Contributors.
# Standalone perf-history runner. Pulls hw-native-sys/simpler, benchmarks one
# commit per PR (PRs are squash-merged, so each commit on main is one PR's end
# state) across multiple NPUs newest-PR-first, post-processes (device tag,
# ordering, deltas), and optionally pushes to Feishu.
#
# Usage:
#   ./run.sh                       # daily incremental: last 36h of newly-landed PRs
#   ./run.sh --recent 100          # most-recent 100 PRs (initial backfill)
#   ./run.sh --since '3 days ago'  # custom window
#   ./run.sh --recent 50 --push    # also push processed md to Feishu (needs .env)
#
# Flags:
#   --since DATE   git date window (default: '36 hours ago'). The >24h window
#                  means a run that fails (e.g. flaky GitHub egress) is
#                  auto-recovered by the next day's run; Feishu push dedups by
#                  sha, so the overlap never double-posts.
#   --recent N     benchmark most-recent N PRs instead of --since
#   -m M           parallel shards / NPUs (default 4)
#   -r ROUNDS      benchmark rounds per case (default 100)
#   --workdir DIR  outputs/worktrees/clone dir (default <script>/work)
#   --push         push processed md to Feishu (loads .env)

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# 时间取自网络（本服务器时钟被改过，不可信）；网络不可达时回退本机时间。
net_now() { python3 "$SCRIPT_DIR/nettime.py" '%Y-%m-%d %H:%M:%S' 2>/dev/null || date '+%F %T'; }
# Egress convention (see `pypto-setup`): clone/fetch over SSH first, fall back
# to the box's local http proxy with the https URL. REPO_URL is resolved to one
# of these by pick_git_egress below.
REPO_SSH="git@github.com:hw-native-sys/simpler.git"
REPO_HTTPS="https://github.com/hw-native-sys/simpler"
REPO_URL="$REPO_HTTPS"

# Load app creds + notify target early so a failure at any stage can DM. The
# push step (step 4) relies on the same vars, so this single load covers both.
[[ -f "$SCRIPT_DIR/.env" ]] && { set -a; . "$SCRIPT_DIR/.env"; set +a; }

# On any non-zero exit, send a Feishu failure card naming the stage that broke.
# Idempotent (trap + explicit calls share NOTIFIED); a missing/unconfigured
# recipient is a soft no-op (notify_feishu.py exits 3) and never crashes the run.
STAGE="startup"
NOTIFIED=0
notify_failure() {
  [[ "$NOTIFIED" -eq 1 ]] && return 0
  NOTIFIED=1
  python "$SCRIPT_DIR/notify_feishu.py" --status fail \
    --title "perf-tracker 失败 ($(hostname -s))" \
    --line "**阶段**: $STAGE" \
    --line "**原因**: $1" \
    --line "**时间**: $(net_now)" \
    --line "日志: \`work/cron.log\`" >/dev/null 2>&1 \
    || echo "[run] (feishu notify skipped/failed)"
}
trap 'rc=$?; if [[ $rc -ne 0 ]]; then notify_failure "run.sh exited $rc"; fi' EXIT

# Daily increments are small -> default to a single NPU. Override with -m for
# a big backfill (e.g. --recent 100 -m 4).
SINCE="36 hours ago"
RECENT=""
M=1
ROUNDS=100
WORKDIR="$SCRIPT_DIR/work"
PUSH=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --since) SINCE="$2"; RECENT=""; shift 2 ;;
    --recent) RECENT="$2"; SINCE=""; shift 2 ;;
    -m) M="$2"; shift 2 ;;
    -r) ROUNDS="$2"; shift 2 ;;
    --workdir) WORKDIR="$2"; shift 2 ;;
    --push) PUSH=1; shift ;;
    *) echo "unknown arg: $1"; exit 1 ;;
  esac
done
mkdir -p "$WORKDIR"
REPO="$WORKDIR/simpler"

# This box's GitHub egress is flaky and the working path drifts on the shared
# box, so probe rather than pin. Per the `pypto-setup` convention: try SSH
# first; if SSH is down, fall back to the local http proxy (4780/4781) over the
# https URL; direct https is a last resort. The probe is a real `ls-remote`
# (exercises auth + transport), and it also resolves REPO_URL to the scheme
# that works so clone/fetch and the `upstream` remote all use it.
pick_git_egress() {
  local p
  if timeout 20 git ls-remote "$REPO_SSH" HEAD >/dev/null 2>&1; then
    unset https_proxy http_proxy HTTPS_PROXY HTTP_PROXY
    REPO_URL="$REPO_SSH"; echo "[run] egress: ssh"; return 0
  fi
  for p in 4780 4781; do
    if timeout 20 git -c http.proxy="http://127.0.0.1:$p" \
         ls-remote "$REPO_HTTPS" HEAD >/dev/null 2>&1; then
      export https_proxy="http://127.0.0.1:$p" http_proxy="http://127.0.0.1:$p"
      export HTTPS_PROXY="$https_proxy" HTTP_PROXY="$http_proxy"
      REPO_URL="$REPO_HTTPS"; echo "[run] egress: http proxy 127.0.0.1:$p"; return 0
    fi
  done
  if timeout 20 git -c http.proxy= ls-remote "$REPO_HTTPS" HEAD >/dev/null 2>&1; then
    unset https_proxy http_proxy HTTPS_PROXY HTTP_PROXY
    REPO_URL="$REPO_HTTPS"; echo "[run] egress: direct https"; return 0
  fi
  echo "[run] WARN: no egress (ssh/proxy/direct) reached GitHub; using ssh"
  REPO_URL="$REPO_SSH"
}

# Retry a (git) command over transient network failures: 5 attempts, linear
# backoff. Returns the command's status on the final attempt.
git_retry() {
  local n=0 max=5
  until "$@"; do
    n=$((n + 1))
    (( n >= max )) && { echo "[run] FAILED after $max attempts: $*" >&2; return 1; }
    echo "[run] attempt $n/$max failed; retry in $((n * 15))s: $*" >&2
    sleep $((n * 15))
  done
}

STAGE="egress/fetch"
pick_git_egress

# 1. Clone or update the target repo from GitHub.
if [[ -d "$REPO/.git" ]]; then
  echo "[run] updating $REPO"
  git -C "$REPO" remote set-url upstream "$REPO_URL" 2>/dev/null \
    || git -C "$REPO" remote add upstream "$REPO_URL"
else
  echo "[run] cloning $REPO_URL -> $REPO"
  git_retry git clone "$REPO_URL" "$REPO"
  git -C "$REPO" remote add upstream "$REPO_URL" 2>/dev/null || true
fi
# Non-fatal: on total fetch failure keep the (stale) clone and let the next
# run's 36h window recover. A stale upstream/main just yields already-pushed
# commits, which the Feishu dedup skips.
git_retry git -C "$REPO" fetch upstream main --quiet || {
  echo "[run] WARN: fetch failed; using stale upstream/main"
  # Soft alert (does not flip NOTIFIED): the run continues on the stale clone,
  # and the 36h window recovers today's commits on the next successful run.
  python "$SCRIPT_DIR/notify_feishu.py" --status fail \
    --title "perf-tracker fetch 失败 ($(hostname -s))" \
    --line "GitHub fetch 重试后仍失败,本次改用旧 clone" \
    --line "可能漏测今天的新 commit;36h 窗口将在下次运行补回" \
    --line "**时间**: $(net_now)" >/dev/null 2>&1 || true
}

# Reuse an existing pto-isa clone so a fresh simpler clone's per-commit build
# does not re-clone pto-isa over ssh (fragile on this box). Override with
# PERF_PTO_ISA_ROOT; falls back to a known local checkout.
if [[ ! -d "$REPO/build/pto-isa" && -z "${PTO_ISA_ROOT:-}" ]]; then
  for cand in "${PERF_PTO_ISA_ROOT:-}" \
              /data/m00956180/runtime/simpler_wc/build/pto-isa; do
    if [[ -n "$cand" && -d "$cand" ]]; then export PTO_ISA_ROOT="$cand"; break; fi
  done
fi
[[ -n "${PTO_ISA_ROOT:-}" ]] && echo "[run] PTO_ISA_ROOT=$PTO_ISA_ROOT"

# 2. Parallel benchmark (writes <workdir>/perf_history.{md,jsonl}).
ARGS=(--repo "$REPO" --workdir "$WORKDIR" -m "$M" -r "$ROUNDS" --ref upstream/main)
if [[ -n "$RECENT" ]]; then ARGS+=(-n "$RECENT"); else ARGS+=(--since "$SINCE"); fi
echo "[run] benchmarking: ${ARGS[*]}"
STAGE="benchmark shards"
bash "$SCRIPT_DIR/perf_history_parallel.sh" "${ARGS[@]}"

# 3. Post-process into separate files (raw kept intact).
STAGE="finalize"
if [[ -s "$WORKDIR/perf_history.jsonl" ]]; then
  python "$SCRIPT_DIR/perf_finalize.py" --repo "$REPO" \
    --jsonl "$WORKDIR/perf_history.jsonl" \
    --out-jsonl "$WORKDIR/perf_history_processed.jsonl" \
    --out-md "$WORKDIR/perf_history_processed.md" \
    --shard-glob "$WORKDIR/perf_shard_*.log"
else
  echo "[run] no results to process."
  exit 0
fi

# 4. Optional: publish to Feishu (per-month docs + index; prepend newest).
# .env was already sourced at the top of this script.
if [[ "$PUSH" -eq 1 ]]; then
  STAGE="feishu publish"
  python "$SCRIPT_DIR/feishu_perf_report.py" \
    --from-processed "$WORKDIR/perf_history_processed.jsonl" \
    --publish --state "$WORKDIR/feishu_state.json"

  # Second, independent doc: raw measured values only (no Δ, no alerts). Its own
  # state file -> its own index/month docs. Dedups by sha, so each commit shows
  # the value from its first measurement; starts empty (begins accumulating from
  # the first run that creates feishu_state_raw.json).
  STAGE="feishu publish (raw values)"
  python "$SCRIPT_DIR/feishu_perf_report.py" \
    --from-processed "$WORKDIR/perf_history_processed.jsonl" \
    --publish --no-delta --title-prefix "Simpler 性能实测值" \
    --state "$WORKDIR/feishu_state_raw.json"
fi

echo "[run] done. processed md: $WORKDIR/perf_history_processed.md"
