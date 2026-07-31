#!/usr/bin/env python3
# Copyright (c) PyPTO Contributors.
# Benchmark the runtime performance of a range of commits and emit a
# structured report (JSON + markdown table). Optionally feeds tools/
# feishu_perf_report.py which pushes the table to a Feishu document.
#
# For each commit (oldest -> newest) the driver:
#   1. checks the commit out into an isolated git worktree,
#   2. creates a project-local .venv and rebuilds the C++ runtime
#      (pip install --no-build-isolation -e .),
#   3. runs tools/benchmark_rounds.sh -d <device>,
#   4. parses the "Performance Summary" table (Host/Device/Total/Sched/Orch).
#
# --current-only skips steps 1-2 and benchmarks the already-built working
# tree once -- used to validate the pipeline before a full sweep.
#
# This script does NOT know about task-submit, so it stays portable across
# servers. Where NPU access is gated by task-submit (this dev box / CI),
# wrap the WHOLE invocation externally and forward the locked device:
#
#   task-submit --device auto --device-num 1 --timeout 7200 --max-time 7200 \
#       --run "python tools/perf_history.py -n 3 --device \$TASK_DEVICE"
#
# On a server with direct NPU access, just pass --device <id> (no wrapper).

import argparse
import fcntl
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

# Standalone: set in main() from --repo / --workdir.
#   REPO    = target simpler clone to benchmark (has tools/benchmark_rounds.sh,
#             .github/workflows/ci.yml, build/pto-isa)
#   WORKDIR = where worktrees / logs / outputs live, kept OUT of the target
#             repo so this tool runs independently from any location.
REPO = Path(".").resolve()
WORKDIR = Path(".").resolve()


def run(cmd, **kw):
    """Run a command, stream nothing, return CompletedProcess (text)."""
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


def pto_isa_pin():
    """Extract the pinned PTO-ISA commit from the repo's ci.yml."""
    ci = REPO / ".github" / "workflows" / "ci.yml"
    if not ci.exists():
        return None
    for line in ci.read_text().splitlines():
        m = re.search(r"PTO_ISA_COMMIT:\s*([0-9a-f]{7,40})", line)
        if m:
            return m.group(1)
        m = re.search(r"--pto-isa-commit\s+([0-9a-f]{7,40})", line)
        if m:
            return m.group(1)
    return None


def resolve_commits(n, rev_range, ref, since=None):
    """Return list of (sha, subject) oldest-first, one commit per PR.

    PRs are squash-merged, so each commit on `ref` is exactly one PR's end
    state; `n` therefore counts PRs.

    `since` (git date, e.g. '2026-06-17' or '1 day ago') selects only PRs
    landed newer than that — used by the daily incremental mode.
    """
    spec = [ref]
    if since:
        spec = [f"--since={since}", ref]
    elif rev_range:
        spec = [rev_range]
    else:
        spec = [f"-n{n}", ref]
    out = run(["git", "-C", str(REPO), "log", "--format=%H\t%s", *spec])
    if out.returncode != 0:
        sys.exit(f"git log failed: {out.stderr.strip()}")
    rows = [l.split("\t", 1) for l in out.stdout.splitlines() if l.strip()]
    rows.reverse()  # oldest -> newest
    return [(r[0], r[1] if len(r) > 1 else "") for r in rows]


def extract_summary_block(text):
    """Grab the benchmark's 'Performance Summary' section verbatim.

    Format-agnostic: whatever columns the (possibly old) benchmark prints are
    captured as-is. Returns the text from the summary banner through the last
    table row (excludes the trailing 'Benchmark complete' line). Empty string
    if no summary was printed (e.g. all cases failed).
    """
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines)
                  if "Performance Summary" in l), None)
    if start is None:
        return ""
    # Include the '====' banner line just above the title, if present.
    s = start - 1 if start > 0 and set(lines[start - 1].strip()) == {"="} else start
    end = len(lines)
    for i in range(start, len(lines)):
        if "Benchmark complete" in lines[i]:
            end = i
            break
    return "\n".join(lines[s:end]).rstrip()


def build_bench_cmd(root, rounds, runtime, platform):
    """Benchmark command WITHOUT the -d device flag (caller appends it).

    The device flag is added separately so the task-submit path can leave
    $TASK_DEVICE unquoted for shell expansion.

    `pin` is intentionally NOT forwarded: current simpler pins pto-isa per
    commit via its own committed pto_isa.pin, and benchmark_rounds.sh no longer
    accepts a `-c <commit>` flag. Passing it made benchmark_rounds forward the
    unknown `-c` straight into `test_*.py`, which rejected it ("unrecognized
    arguments") and failed every case -> empty summaries -> Feishu "(no data)".
    """
    return [
        str(Path(root) / "tools" / "benchmark_rounds.sh"),
        "-n", str(rounds),
        "-r", runtime,
        "-p", platform,
    ]


def run_benchmark(root, device, rounds, runtime, platform, logfile, venv=None):
    """Run benchmark_rounds.sh -d <device> in `root`.

    If `venv` is given, activate it first (so the benchmark imports the
    freshly-built simpler from that worktree). Streams combined output to
    `logfile` (tailable during long runs), then reads it back for parsing.
    """
    cmd = build_bench_cmd(root, rounds, runtime, platform) + ["-d", device]
    bench = " ".join(shlex.quote(c) for c in cmd)
    if venv:
        activate = shlex.quote(str(venv) + "/bin/activate")
        run_str = f"source {activate} && {bench}"
    else:
        run_str = bench
    # Point the benchmark's -c pto-isa checkout at the shared clone so it does
    # not re-clone over ssh/proxy.
    env = dict(os.environ)
    shared_pto_isa = REPO / "build" / "pto-isa"
    if shared_pto_isa.exists():
        env["PTO_ISA_ROOT"] = str(shared_pto_isa)
    print(f"  [run] {bench}  (log: {logfile})", flush=True)
    with open(logfile, "w") as f:
        proc = subprocess.run(["bash", "-lc", run_str], cwd=root, env=env,
                              stdout=f, stderr=subprocess.STDOUT, text=True)
    out = Path(logfile).read_text(errors="replace")
    return out, proc.returncode


def _git_worktree(args):
    """Run a `git worktree ...` command serialized across processes.

    Concurrent `git worktree add/remove` on the same repo race on git's
    internal lock; an flock makes the parallel launcher safe.
    """
    lock = WORKDIR / ".worktree.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            return run(["git", "-C", str(REPO), "worktree", *args])
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def patch_example_map(wt_dir):
    """Transiently override the worktree's benchmark example set.

    When PERF_BENCH_MAP_FILE points at a file holding a replacement
    `declare -A TMR_EXAMPLE_CASES=(...)` + `TMR_EXAMPLE_ORDER=(...)` block, swap
    it into this (disposable) worktree's tools/benchmark_rounds.sh before the
    benchmark runs. Used to restore a fuller example set that a commit's own
    script trimmed, WITHOUT committing anything to the target repo. No-op when
    the env var is unset (the daily cron path is untouched).
    """
    mapf = os.environ.get("PERF_BENCH_MAP_FILE")
    if not mapf or not Path(mapf).exists():
        return
    script = Path(wt_dir) / "tools" / "benchmark_rounds.sh"
    if not script.exists():
        return
    block = Path(mapf).read_text().rstrip("\n")
    text = script.read_text()
    new, n = re.subn(
        r"declare -A TMR_EXAMPLE_CASES=\(.*?\)\nTMR_EXAMPLE_ORDER=\(.*?\)",
        lambda _m: block, text, count=1, flags=re.S)
    if n:
        script.write_text(new)
        print(f"  patched example map from {mapf}", flush=True)
    else:
        print(f"  WARN: example-map block not found in {script}", flush=True)


def setup_worktree(sha, wt_dir):
    print(f"  worktree: {wt_dir}", flush=True)
    _git_worktree(["add", "--detach", str(wt_dir), sha])
    # Reuse the already-cloned PTO-ISA (each commit self-pins it via its own
    # committed pto_isa.pin) so the per-worktree rebuild does not re-clone over
    # ssh/proxy.
    env = dict(os.environ)
    shared_pto_isa = REPO / "build" / "pto-isa"
    if shared_pto_isa.exists():
        env["PTO_ISA_ROOT"] = str(shared_pto_isa)
    # Per-worktree venv (inherits build deps from the active env's
    # site-packages via --system-site-packages).
    subprocess.run(
        ["python3", "-m", "venv", "--system-site-packages", ".venv"],
        cwd=wt_dir, check=True)
    print(f"  pip install --no-build-isolation -e . (rebuild, "
          f"PTO_ISA_ROOT={env.get('PTO_ISA_ROOT', 'unset')}) ...", flush=True)
    rc = subprocess.run(
        ["bash", "-lc",
         "source .venv/bin/activate && pip install --no-build-isolation -e . "
         "> .venv/build.log 2>&1"],
        cwd=wt_dir, env=env)
    if rc.returncode != 0:
        print(f"  BUILD FAILED (see {wt_dir}/.venv/build.log)", flush=True)
        return False
    return True


def teardown_worktree(wt_dir):
    _git_worktree(["remove", "--force", str(wt_dir)])
    if Path(wt_dir).exists():
        shutil.rmtree(wt_dir, ignore_errors=True)


def render_markdown(report):
    """Per-commit section embedding the benchmark summary block verbatim."""
    meta = (f"**ref** `{report.get('ref')}` · **runtime** `{report.get('runtime')}`"
            f" · **platform** `{report.get('platform')}` · **rounds** "
            f"{report.get('rounds')} · **pto-isa** "
            f"`{(report.get('pto_isa_commit') or '')[:10]}`")
    out = ["# Perf history", "", meta, ""]
    for c in report["commits"]:
        out.append(commit_md_section(c))
    return "\n".join(out)


def commit_md_section(entry):
    """Markdown for a single commit: heading + verbatim summary code block."""
    dev = entry.get("device")
    tag = f"  ·  NPU {dev}" if dev not in (None, "") else ""
    out = [f"## {entry['sha'][:10]} — {entry['subject']}{tag}", ""]
    summary = entry.get("summary") or ""
    if summary.strip():
        out += ["```text", summary, "```", ""]
    else:
        out += [f"_no summary (rc={entry.get('rc')})_", ""]
    return "\n".join(out)


def append_locked(path, text):
    """Append text to a file under an exclusive lock (parallel-safe)."""
    with open(path, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True,
                    help="path to the target simpler clone to benchmark")
    ap.add_argument("--workdir", default="perf_work",
                    help="dir for worktrees/logs/outputs (kept out of --repo; "
                         "default ./perf_work)")
    ap.add_argument("-n", "--commits", type=int, default=5,
                    help="benchmark the most recent N commits (default 5)")
    ap.add_argument("--since",
                    help="benchmark only commits newer than this git date "
                         "(e.g. '2026-06-17', 'midnight', '1 day ago'); "
                         "daily-incremental mode")
    ap.add_argument("--rev-range", help="explicit git rev range, e.g. A..B")
    ap.add_argument("--commit-list",
                    help="explicit space/comma-separated SHAs to benchmark, in "
                         "order (used by the parallel launcher to shard work)")
    ap.add_argument("--ref", default="upstream/main",
                    help="ref to take recent commits from (default upstream/main)")
    ap.add_argument("--append-md",
                    help="append each commit's section to this md file as soon "
                         "as it finishes (crash-safe, flock; parallel-safe)")
    ap.add_argument("--append-jsonl",
                    help="append each commit's result as one JSON line here "
                         "(durable record; flock; parallel-safe)")
    ap.add_argument("--rounds", type=int, default=100)
    ap.add_argument("--runtime", default="tensormap_and_ringbuffer")
    ap.add_argument("--platform", default="a2a3")
    ap.add_argument("--current-only", action="store_true",
                    help="benchmark the current (already built) tree once; "
                         "skip checkout/rebuild. For pipeline validation.")
    ap.add_argument("-d", "--device", default="0",
                    help="NPU device id passed to benchmark_rounds.sh -d "
                         "(default 0). On a task-submit box, forward "
                         "$TASK_DEVICE from the wrapping task.")
    ap.add_argument("-o", "--output", default="perf_history.json",
                    help="output json filename, relative to --workdir")
    args = ap.parse_args()

    global REPO, WORKDIR
    REPO = Path(args.repo).resolve()
    WORKDIR = Path(args.workdir).resolve()
    if not (REPO / ".git").exists():
        sys.exit(f"--repo {REPO} is not a git repo")
    WORKDIR.mkdir(parents=True, exist_ok=True)

    pin = pto_isa_pin()
    print(f"repo: {REPO}")
    print(f"workdir: {WORKDIR}")
    print(f"PTO-ISA pin: {pin or '(none found)'}")
    print(f"device: {args.device}")

    report = {
        "ref": args.ref,
        "runtime": args.runtime,
        "platform": args.platform,
        "rounds": args.rounds,
        "pto_isa_commit": pin,
        "commits": [],
    }

    if args.current_only:
        sha = run(["git", "-C", str(REPO), "rev-parse", "HEAD"]).stdout.strip()
        subj = run(["git", "-C", str(REPO), "log", "-1", "--format=%s"]).stdout.strip()
        print(f"\n=== current tree {sha[:10]}  {subj} ===")
        WORKDIR.mkdir(parents=True, exist_ok=True)
        out, rc = run_benchmark(REPO, args.device, args.rounds,
                                args.runtime, args.platform,
                                WORKDIR / "perf_current_raw.txt")
        summary = extract_summary_block(out)
        report["commits"].append(
            {"sha": sha, "subject": subj, "rc": rc, "summary": summary,
             "device": args.device})
        print(f"  captured summary: {'yes' if summary else 'no'} (rc={rc})")
    else:
        if args.commit_list:
            shas = [s for s in re.split(r"[\s,]+", args.commit_list) if s]
            commits = [(s, run(["git", "-C", str(REPO), "show", "-s",
                                "--format=%s", s]).stdout.strip()) for s in shas]
        else:
            # fetch upstream so --ref is current
            run(["git", "-C", str(REPO), "fetch", "upstream", "main",
                 "--quiet"])
            commits = resolve_commits(args.commits, args.rev_range, args.ref,
                                      since=args.since)
        print(f"Benchmarking {len(commits)} commits on {args.ref}"
              + (f" since {args.since}" if args.since else ""))
        if not commits:
            print("no commits to benchmark; nothing to do.")
        wt_root = WORKDIR / "perf_worktrees"
        wt_root.mkdir(parents=True, exist_ok=True)
        log_root = WORKDIR / "perf_logs"
        for sha, subj in commits:
            print(f"\n=== {sha[:10]}  {subj} ===", flush=True)
            wt = wt_root / sha[:12]
            entry = {"sha": sha, "subject": subj, "rc": None, "summary": "",
                     "device": args.device}
            try:
                if not setup_worktree(sha, wt):
                    entry["rc"] = "build_failed"
                    continue
                patch_example_map(wt)
                out, rc = run_benchmark(wt, args.device, args.rounds,
                                        args.runtime, args.platform,
                                        wt / "bench_raw.txt", venv=wt / ".venv")
                entry["summary"] = extract_summary_block(out)
                entry["rc"] = rc
                print(f"  captured summary: "
                      f"{'yes' if entry['summary'] else 'no'} (rc={rc})")
            finally:
                # Preserve logs (build + benchmark) before removing worktree.
                keep = log_root / sha[:12]
                keep.mkdir(parents=True, exist_ok=True)
                for src in [wt / "bench_raw.txt", wt / ".venv" / "build.log"]:
                    if src.exists():
                        shutil.copy(src, keep / src.name)
                report["commits"].append(entry)
                # Crash-safe incremental append (one commit at a time, locked).
                if args.append_jsonl:
                    append_locked(args.append_jsonl, json.dumps(entry) + "\n")
                if args.append_md:
                    append_locked(args.append_md, commit_md_section(entry))
                teardown_worktree(wt)

    out_path = WORKDIR / args.output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2))
    md_path = out_path.with_suffix(".md")
    md_path.write_text(render_markdown(report))
    print(f"\nJSON   -> {out_path}")
    print(f"Markdown -> {md_path}\n")
    print(render_markdown(report))


if __name__ == "__main__":
    main()
