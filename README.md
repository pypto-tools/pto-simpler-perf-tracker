# perf-tracker

Standalone tool that benchmarks the per-PR runtime performance of
[hw-native-sys/simpler](https://github.com/hw-native-sys/simpler) across
multiple NPUs and produces a Feishu-ready report.

It runs from anywhere — it pulls the target repo itself.

## Per-PR selection

PRs are **squash-merged**, so each commit on main is exactly one PR's end
state — selection is just `git log` over the ref, **newest PR first**.
`--recent N` therefore counts PRs.

## Files

| File | Role |
| ---- | ---- |
| `run.sh` | entry point: clone/update simpler → benchmark → process → (push) |
| `perf_history.py` | core: per-PR worktree → rebuild → `benchmark_rounds.sh` → capture summary verbatim. Portable (no task-submit). |
| `perf_history_parallel.sh` | shards PRs (one squash commit each) across M NPUs via `task-submit` (box-specific). Picks cards with explicit `--device N` and, on a benchmark failure, drops the wedged card and re-runs the unfinished commits on a fresh one — see [Card-switch on failure](#card-switch-on-failure). |
| `perf_finalize.py` | post-process: tag NPU per commit, parse metrics, order newest→oldest, add Δ-vs-previous-commit. Writes `*_processed.*`, never touches raw. |
| `feishu_perf_report.py` | push report to a Feishu doc (docx/wiki) or sheet |
| `.env.example` | Feishu credentials template → copy to `.env` |

## Usage

```bash
./run.sh                       # daily incremental: last 36h of newly-landed PRs
./run.sh --recent 100          # initial backfill of the most-recent 100 PRs
./run.sh --since '3 days ago'  # custom window
./run.sh --recent 50 --push    # also push to Feishu (needs .env)
```

Flags: `--since DATE` (default `36 hours ago`), `--recent N`, `-m` shards/NPUs
(default 4), `-r` rounds (default 100), `--workdir DIR` (default `./work`),
`--push`.

The default window is intentionally >24h: a daily run that fails (this box's
GitHub egress is flaky) is auto-recovered by the next day's overlapping run,
and the Feishu push dedups by sha so the overlap never double-posts.

### GitHub egress

`run.sh` probes egress before clone/fetch and adopts the first that reaches
GitHub — **SSH first, then the local http proxy (4780/4781), then direct
https** (the `pypto-setup` convention) — then retries git ops over transient
SSL timeouts. No port is pinned; the working path drifts on this shared box.

## Outputs (under `--workdir`, default `./work`)

| File | What |
| ---- | ---- |
| `perf_history.md` / `.jsonl` | **raw**, append-only, crash-safe (one commit at a time, flock) |
| `perf_history_processed.md` / `.jsonl` | NPU-tagged, ordered, with ΔDevice/ΔOrch vs previous commit. Finalize re-reads the prior `.jsonl` and carries its newest commit in as a baseline, so the first commit of each daily window still gets a Δ across the run boundary. **Already-published commits are frozen**: if the 36h overlap re-benchmarks a commit that already has metrics in the prior `.jsonl`, finalize reuses the published metrics instead of the fresh re-measurement — so the value shown in the Feishu doc (push dedups by sha, never updating a published row) stays identical to the baseline used for newer commits' Δ. Without this, a noisy re-measurement would make a newer commit's Δ disagree with what the doc displays. |
| `simpler/` | the managed clone (worktrees built under `perf_worktrees/`) |

## Requirements

- **`task-submit`** on PATH (NPU device locking on this box / CI runners).
- A Python env with the simpler build deps (`scikit-build-core`, `nanobind`,
  `cmake`/`ninja`) on the system site so each worktree's
  `--system-site-packages` venv can rebuild. Activate it before `./run.sh`.
- **PTO-ISA**: the per-commit build reuses `<clone>/build/pto-isa` if present
  (set as `PTO_ISA_ROOT` automatically). On a fresh clone the first build
  populates it; if your network can't clone pto-isa, point `PTO_ISA_ROOT` at an
  existing checkout before running.

## Card-switch on failure

A shared NPU can wedge mid-sweep (an op-timeout / stalled kernel leaves the
card unusable until reset). When one card was pinned for a whole shard, every
commit after the wedge cascade-failed — the tail of the sweep silently lost its
data. The launcher now recovers by **switching cards**, using only
unprivileged `task-submit` primitives:

- Cards are chosen with explicit `--device N` (a normal user may pin a card;
  it bypasses `task-submit`'s auto-allocation health view, which the daemon
  refreshes at most every 12h and so can hand back a just-wedged card).
  `--device auto` is **not** used, and `--block` is avoided (it needs `sudo`
  and marks a card bad box-wide).
- Free cards (per `task-submit --list`) are preferred so we rarely queue-wait;
  busy ones are a fallback (`--device N` waits for the lock).
- After each round, a card whose shard did **not** finish its tail (the
  cascade signature of a wedge) is dropped into an in-memory bad set, and the
  unfinished commits are re-submitted on a fresh card. Up to `PERF_MAX_ROUNDS`
  rounds.
- A commit that fails on `PERF_MAX_ATTEMPT` distinct cards is treated as
  genuinely broken (not a card fault) and left with its `rc=1` record.
- The bad set is **per-run, never persisted**: every daily run starts clean, so
  a card that recovers (e.g. after a box reboot) is retried next run with no
  stale blacklist to clear.

Retries append a fresh line for the re-run commit; `perf_finalize.py` collapses
multiple lines per sha to one, preferring the successful measurement.

Tunables (env): `PERF_MAX_ROUNDS` (default 4), `PERF_MAX_ATTEMPT` (default 3),
`PERF_DEVICES` (confine to a card pool, e.g. `"3,4,5,6"`),
`PERF_TASK_TIMEOUT` (per-shard queue-wait, default 6h).

## Daily incremental (cron)

调度**按北京时间钉住**（`CRON_TZ`），不随服务器时区/时钟漂移——本机被人改过时间，
故不依赖服务器本地时间。cronie 支持 `CRON_TZ`；每天**北京 22:00**（= 服务器 07:00 PDT）运行。

```cron
CRON_TZ=Asia/Shanghai
# 北京 22:00 daily: benchmark commits landed in the last 36h (run.sh default), push to Feishu
0 22 * * *  cd /data/.../mytools/perf-tracker && source /data/miniconda3/etc/profile.d/conda.sh && conda activate mjzkd && PATH=/usr/local/bin:$PATH ./run.sh --push >> work/cron.log 2>&1
```

- `CRON_TZ` 只钉**时区**；「在正确的真实时刻触发」仍依赖系统**时钟**本身准确（靠 NTP，管理员维护）。
  若系统时钟被 `date -s` 改错，cron 会按错误时钟触发——这一点 cron 层面无解。
- 报告/通知里显示的**时间戳**已改用网络时间（见 Notes 的 `nettime.py`），与系统时钟无关。

## Notes

- Parallel appends are serialized with `flock` — verified corruption-free.
- The raw md/jsonl are never rewritten; all processing goes to `*_processed.*`.
- `perf_history.py` has no knowledge of `task-submit`, so it also runs directly
  on a box with unlocked NPU access: `python perf_history.py --repo PATH -d 0 ...`.
- **报告/通知时间戳取自网络时间**（`nettime.py`，读 HTTP `Date:` 头，走同样的
  代理/直连），因为本服务器系统时钟被改过、不可信。网络不可达时回退本机时间并在
  stderr 标注 `LOCAL-FALLBACK`。时区仍按本机 `/etc/localtime`（与系统时钟无关）。
