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
# On a server with direct NPU access, pass --device and run locally.  On a box
# where NPU access is gated, --task-submit acquires the device only for step 3;
# worktree creation and compilation stay outside the NPU allocation:
#
#   python perf_history.py --repo ... --task-submit

import argparse
import fcntl
import json
import os
import re
import shlex
import shutil
import statistics
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

HOST_CASES = {
    "qwen3-14b": {
        "name": "qwen3-14b",
        "entry": "examples/a2a3/host_build_graph/qwen3_14b_decode/main.py",
        "device_num": 1,
        "ranks": 1,
        "timeout": 2400,
        "log_level_arg": True,
    },
    "dsv4-flash": {
        "name": "dsv4-flash",
        "entry": "examples/a2a3/host_build_graph/deepseek_v4_flash_decode/main.py",
        "device_num": 2,
        "ranks": 2,
        "timeout": 3600,
        "log_level_arg": False,
    },
}
HOST_BIND_PHASE_RE = re.compile(
    r"bind phase=(\w+) start_ns=(\d+) dur_ns=(\d+)")
HOST_STAMP_RE = re.compile(r"^\[stamp\] (.*)$", re.MULTILINE)
HOST_BIND_CLOSING_PHASE = "arena_h2d"
HOST_CONTROL_PLANE = (
    "host_orch", "graph_upload", "relocate", "sm_h2d", "arena_h2d")


class TaskStillRunning(RuntimeError):
    """task-submit stopped waiting while the submitted task is still alive."""


def resolve_cann_env_script():
    """Return the CANN environment script used for builds and benchmarks.

    Cron starts with a deliberately small environment, and activating the
    project's conda environment does not add CANN's shared libraries to
    LD_LIBRARY_PATH.  Resolve the environment setup here so execution never
    depends on an interactive shell having sourced it first.

    PERF_CANN_ENV_SCRIPT is a strict override: a typo is an error instead of
    silently falling back to another CANN installation.
    """
    override = os.environ.get("PERF_CANN_ENV_SCRIPT")
    if override:
        path = Path(override).expanduser()
        if not path.is_file():
            raise RuntimeError(
                f"PERF_CANN_ENV_SCRIPT does not exist or is not a file: {path}")
        return path.resolve()

    candidates = []
    for name in ("ASCEND_HOME_PATH", "ASCEND_TOOLKIT_HOME", "CANN_HOME"):
        value = os.environ.get(name)
        if value:
            candidates.append(Path(value).expanduser() / "set_env.sh")
    candidates.extend([
        Path("/usr/local/Ascend/cann/set_env.sh"),
        Path("/usr/local/Ascend/ascend-toolkit/set_env.sh"),
        Path("/usr/local/Ascend/ascend-toolkit/latest/set_env.sh"),
    ])

    seen = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            return path.resolve()
    checked = ", ".join(str(p) for p in candidates)
    raise RuntimeError(
        "CANN environment setup was not found; set PERF_CANN_ENV_SCRIPT "
        f"to the installed set_env.sh (checked: {checked})")


def resolve_arch_precheck_script(repo, platform, task_submit):
    """Return simpler's onboard architecture gate for allocated hardware runs."""
    if not task_submit or platform.endswith("sim"):
        return None
    if platform not in ("a2a3", "a5"):
        raise RuntimeError(f"unsupported onboard platform: {platform}")
    script = (Path(repo) / ".claude" / "skills" /
              "onboard-arch-precheck" / "check.sh")
    if not script.is_file():
        raise RuntimeError(
            f"onboard architecture precheck is missing: {script}")
    return script.resolve()


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
    table row and trailing 'Benchmark complete' line. Empty string
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
            end = i + 1
            break
    return "\n".join(lines[s:end]).rstrip()


def build_bench_cmd(root, rounds, runtime, platform, verbose=False):
    """Benchmark command without the -d device flag (caller appends it).

    The device flag is added separately by the caller.

    `pin` is intentionally NOT forwarded: current simpler pins pto-isa per
    commit via its own committed pto_isa.pin, and benchmark_rounds.sh no longer
    accepts a `-c <commit>` flag. Passing it made benchmark_rounds forward the
    unknown `-c` straight into `test_*.py`, which rejected it ("unrecognized
    arguments") and failed every case -> empty summaries -> Feishu "(no data)".
    """
    cmd = [
        str(Path(root) / "tools" / "benchmark_rounds.sh"),
        "-n", str(rounds),
        "-r", runtime,
        "-p", platform,
    ]
    if verbose:
        cmd.append("--verbose")
    return cmd


def parse_host_bind_metrics(text, rounds, ranks):
    """Parse warm HBG bind-phase statistics in microseconds.

    The control-plane total is summed inside each bind before statistics are
    calculated. The first bind of every rank is excluded as cold warm-up.
    """
    binds = []
    current = {}
    for match in HOST_BIND_PHASE_RE.finditer(text or ""):
        phase, dur_ns = match.group(1), int(match.group(3))
        current[phase] = dur_ns / 1000.0
        if phase == HOST_BIND_CLOSING_PHASE:
            if "host_orch" in current:
                binds.append(current)
            current = {}
    if current and "host_orch" in current:
        binds.append(current)
    if not binds:
        raise ValueError("no complete `bind phase=` groups found")
    if rounds and len(binds) != rounds * ranks:
        raise ValueError(
            f"found {len(binds)} binds, expected exactly {rounds * ranks}")
    warm = binds[min(ranks, len(binds)):]
    if not warm:
        raise ValueError("all binds are cold; host benchmark needs at least two rounds")

    def spread(values):
        return {
            "min_us": round(min(values), 3),
            "median_us": round(statistics.median(values), 3),
            "max_us": round(max(values), 3),
            "n": len(values),
        }

    metrics = {}
    phases = sorted({phase for bind in warm for phase in bind})
    for phase in phases:
        values = [bind[phase] for bind in warm if phase in bind]
        metrics[phase] = spread(values)

    present = [phase for phase in HOST_CONTROL_PLANE
               if any(phase in bind for bind in warm)]
    partial = [phase for phase in present
               if not all(phase in bind for bind in warm)]
    if partial:
        raise ValueError(
            f"control-plane phase(s) missing from some warm binds: {', '.join(partial)}")
    if not present:
        raise ValueError("no control-plane phases found")
    totals = [sum(bind[phase] for phase in present) for bind in warm]
    metrics["control_plane"] = spread(totals)
    stamp = HOST_STAMP_RE.search(text or "")
    return {
        "binds": len(binds),
        "warm_binds": len(warm),
        "metrics": metrics,
        "stamp": stamp.group(1) if stamp else "",
        "control_phases": present,
    }


def component_rc_from_output(text, component, fallback):
    """Return a component status marker emitted by the allocated job."""
    match = re.search(
        rf"\[perf-tracker\]\s+{re.escape(component)}_rc=(\d+)", text or "")
    return int(match.group(1)) if match else fallback


def host_result(case, rounds, text, rc):
    """Build the durable per-commit host benchmark record."""
    result = {
        "status": "failed" if rc else "ok",
        "case": case["name"],
        "rounds": rounds,
        "ranks": case["ranks"],
        "rc": rc,
    }
    if rc:
        return result
    try:
        result.update(parse_host_bind_metrics(text, rounds, case["ranks"]))
    except ValueError as exc:
        result["status"] = "invalid"
        result["error"] = str(exc)
    return result


def run_benchmark(root, device, rounds, runtime, platform, logfile, venv=None,
                  task_submit=False, task_wait_timeout=86400,
                  task_max_time=3600, cann_env_script=None,
                  arch_precheck_script=None):
    """Run benchmark_rounds.sh -d <device> in `root`.

    If `venv` is given, activate it first (so the benchmark imports the
    freshly-built simpler from that worktree). Streams combined output to
    `logfile` (tailable during long runs), then reads it back for parsing.
    """
    cann_env_script = cann_env_script or resolve_cann_env_script()
    cmd = build_bench_cmd(
        root, rounds, runtime, platform,
        # The benchmark wrapper otherwise deletes the captured test stderr on
        # return, leaving only "benchmark run returned non-zero".  Preserve
        # detailed per-case output by default; PERF_BENCH_VERBOSE=0 is the
        # explicit opt-out for unusually constrained runs.
        verbose=os.environ.get("PERF_BENCH_VERBOSE", "1") != "0")
    bench_base = " ".join(shlex.quote(c) for c in cmd)
    # task-submit chooses the card at execution time.  Keep TASK_DEVICE for the
    # remote shell to expand; direct mode still accepts an explicit device id.
    bench = (f'{bench_base} -d "$TASK_DEVICE"' if task_submit
             else f"{bench_base} -d {shlex.quote(device)}")
    steps = [
        f"source {shlex.quote(str(cann_env_script))}",
    ]
    if arch_precheck_script:
        # Shared hosts require architecture detection and the hardware test to
        # stay in the same allocated job. The precheck itself may call npu-smi.
        steps.append(
            f"{shlex.quote(str(arch_precheck_script))} "
            f"{shlex.quote(platform)}")
    if venv:
        activate = shlex.quote(str(venv) + "/bin/activate")
        steps.append(f"source {activate}")
    if task_submit:
        # Run after activating the worktree venv and, on task-submit hosts,
        # after acquiring the device. This turns a missing CANN runtime into a
        # single explicit failure instead of eight opaque per-case failures.
        steps.append(
            "python -c 'import torch_npu' || { rc=$?; "
            "echo \"[perf-tracker] CANN preflight failed: "
            "torch_npu import rc=$rc\" >&2; exit \"$rc\"; }")
    steps.append(bench)
    run_str = " && ".join(steps)
    env = dict(os.environ)
    benchmark_tmp_override = os.environ.get("PERF_BENCH_TMPDIR")
    if benchmark_tmp_override:
        # Escape hatch for hosts that already provide a suitable short temp
        # path. The caller owns this directory and its cleanup policy.
        env["TMPDIR"] = benchmark_tmp_override
    else:
        # Keep the actual files under the tool root, but expose them through a
        # short /tmp symlink because some benchmark/runtime paths have tight
        # length limits. Each benchmark gets an isolated home-backed directory
        # and removes both it and the data-free compatibility symlink on exit.
        tmp_base = Path(os.environ.get("TMP_DIR", WORKDIR / "tmp")) / "benchmark"
        tmp_root = shlex.quote(str(tmp_base))
        run_str = (
            f"perf_tmp_root={tmp_root}; "
            'mkdir -p -- "$perf_tmp_root" || exit $?; '
            'perf_tmp_dir=$(mktemp -d "$perf_tmp_root/run.XXXXXX") || exit $?; '
            'perf_tmp_link="/tmp/pto-perf-${UID}-${BASHPID}-${RANDOM}"; '
            'ln -s -- "$perf_tmp_dir" "$perf_tmp_link" || exit $?; '
            'cleanup_perf_tmp() { rm -f -- "$perf_tmp_link"; '
            'rm -rf -- "$perf_tmp_dir"; }; '
            'trap cleanup_perf_tmp EXIT; '
            'export TMPDIR="$perf_tmp_link"; '
            f"{run_str}"
        )
        # task-submit snapshots the whole caller environment. Do not let the
        # outer TOOL_ROOT/tmp overwrite the short alias exported above.
        env.pop("TMPDIR", None)
    # Point the benchmark's pto-isa checkout at the shared clone so it does not
    # re-clone over ssh/proxy.
    shared_pto_isa = REPO / "build" / "pto-isa"
    if shared_pto_isa.exists():
        env["PTO_ISA_ROOT"] = str(shared_pto_isa)
    if task_submit:
        # Building happens in the ordinary user process.  Acquire an NPU only
        # for the benchmark itself, and wait longer than the task's hard run
        # limit so a client-side wait timeout can never leave an orphan task
        # using a worktree that the caller is about to remove.
        run_str = (f"cd {shlex.quote(str(root))} && "
                   f'echo "[perf-tracker] device=$TASK_DEVICE" && {run_str}')
        submit = [
            "task-submit", "--device", "auto",
            "--timeout", str(task_wait_timeout),
            "--max-time", str(task_max_time),
        ]
        for name in ("PTO_ISA_ROOT", "TMPDIR"):
            if env.get(name):
                submit += ["--env", f"{name}={env[name]}"]
        submit += ["--run", run_str]
        exec_cmd = submit
        exec_cwd = None
        mode = "task-submit NPU auto"
    else:
        exec_cmd = ["bash", "-lc", run_str]
        exec_cwd = root
        mode = "local"
    print(f"  [run:{mode}] {bench}  (log: {logfile})", flush=True)
    with open(logfile, "w") as f:
        proc = subprocess.run(exec_cmd, cwd=exec_cwd, env=env,
                              stdout=f, stderr=subprocess.STDOUT, text=True)
    out = Path(logfile).read_text(errors="replace")
    if task_submit and proc.returncode != 0 and (
            "任务仍在运行" in out or "task is still running" in out.lower()):
        raise TaskStillRunning(
            "task-submit wait timed out while the benchmark is still running")
    return out, proc.returncode


def run_host_benchmark(root, case, device, rounds, logfile, venv=None,
                       task_submit=False, task_wait_timeout=86400,
                       task_max_time=3600, cann_env_script=None,
                       arch_precheck_script=None, commit_sha=""):
    """Measure one host_build_graph bind case and retain its stamped raw log."""
    cann_env_script = cann_env_script or resolve_cann_env_script()
    entry = Path(root) / case["entry"]
    host_env = [
        "SIMPLER_HBG_BIND_BREAKDOWN_ENABLE=1",
        "TORCH_DEVICE_BACKEND_AUTOLOAD=0",
        "SIMPLER_SKIP_DEVICE_RUN=1",
    ]
    device_arg = '"$TASK_DEVICE"' if task_submit else shlex.quote(device)
    argv = [
        "python", str(entry), "-p", "a2a3", "--skip-golden",
        "--rounds", str(rounds),
    ]
    if case["log_level_arg"]:
        argv += ["--log-level", "timing"]
    command = ("env " + " ".join(host_env) + " "
               + " ".join(shlex.quote(part) for part in argv)
               + f" -d {device_arg}")
    stamp_argv = (
        f"python {case['entry']} -p a2a3 --skip-golden --rounds {rounds}")
    if case["log_level_arg"]:
        stamp_argv += " --log-level timing"
    stamp_command = (
        "env " + " ".join(host_env) + f" {stamp_argv} -d $TASK_DEVICE")
    Path(logfile).parent.mkdir(parents=True, exist_ok=True)
    Path(logfile).write_text(
        f"[stamp] {commit_sha[:10]} {stamp_command}\n")

    steps = [f"source {shlex.quote(str(cann_env_script))}"]
    if arch_precheck_script:
        steps.append(
            f"{shlex.quote(str(arch_precheck_script))} a2a3")
    if venv:
        steps.append(
            f"source {shlex.quote(str(venv) + '/bin/activate')}")
    if task_submit:
        steps.append(
            "python -c 'import torch_npu' || { rc=$?; "
            "echo \"[perf-tracker] CANN preflight failed: "
            "torch_npu import rc=$rc\" >&2; exit \"$rc\"; }")
        steps.append(
            f"cd {shlex.quote(str(root))} && "
            f'echo "[perf-tracker] device=$TASK_DEVICE" && {command}')
        submit = [
            "task-submit", "--device", "auto", "--device-num",
            str(case["device_num"]), "--timeout", str(task_wait_timeout),
            "--max-time", str(task_max_time), "--run", " && ".join(steps),
        ]
        exec_cmd = submit
        exec_cwd = None
        mode = f"task-submit host {case['name']} x{case['device_num']}"
    else:
        steps.append(command)
        exec_cmd = ["bash", "-lc", " && ".join(steps)]
        exec_cwd = root
        mode = f"local host {case['name']}"
    print(f"  [run:{mode}] {command}  (log: {logfile})", flush=True)
    with open(logfile, "a") as stream:
        proc = subprocess.run(
            exec_cmd, cwd=exec_cwd, env=dict(os.environ), stdout=stream,
            stderr=subprocess.STDOUT, text=True)
    out = Path(logfile).read_text(errors="replace")
    if task_submit and proc.returncode != 0 and (
            "任务仍在运行" in out or "task is still running" in out.lower()):
        raise TaskStillRunning(
            "task-submit wait timed out while the host benchmark is still running")
    return out, proc.returncode


def aggregate_host_results(cases):
    """Return one status plus the independently measured HBG cases."""
    statuses = [case.get("status") for case in cases.values()]
    if any(status in ("failed", "invalid") for status in statuses):
        status = "failed"
    elif any(status == "ok" for status in statuses):
        status = "ok"
    else:
        status = "unsupported"
    return {"status": status, "cases": cases}


def measure_host_cases(root, cases, rounds, log_dir, device, venv=None,
                       task_submit=False, task_wait_timeout=86400,
                       task_max_time=3600, cann_env_script=None,
                       arch_precheck_script=None, commit_sha=""):
    """Measure every selected HBG case from one built commit worktree."""
    results = {}
    for case in cases:
        entry = Path(root) / case["entry"]
        if not entry.is_file():
            results[case["name"]] = {
                "status": "unsupported",
                "case": case["name"],
                "rounds": rounds,
                "ranks": case["ranks"],
                "rc": None,
                "error": f"case entry is absent at this commit: {case['entry']}",
            }
            continue
        logfile = Path(log_dir) / f"host_bind_{case['name']}_raw.txt"
        text, rc = run_host_benchmark(
            root, case, device, rounds, logfile, venv=venv,
            task_submit=task_submit, task_wait_timeout=task_wait_timeout,
            task_max_time=task_max_time, cann_env_script=cann_env_script,
            arch_precheck_script=arch_precheck_script,
            commit_sha=commit_sha)
        result = host_result(case, rounds, text, rc)
        result["device"] = (task_device_from_output(text)
                            if task_submit else device)
        results[case["name"]] = result
        print(
            f"  captured host {case['name']}: {result['status']} "
            f"({result.get('warm_binds', 0)} warm binds, rc={rc})",
            flush=True)
    return aggregate_host_results(results)


def task_device_from_output(text):
    """Return the logical device selected by task-submit, if recorded."""
    match = re.search(r"\[perf-tracker\]\s+device=([0-9,-]+)", text or "")
    return match.group(1) if match else None


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


def setup_worktree(sha, wt_dir, cann_env_script):
    print(f"  worktree: {wt_dir}", flush=True)
    added = _git_worktree(["add", "--detach", str(wt_dir), sha])
    if added.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {added.stderr.strip()}")
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
    # The active conda env may itself contain an editable `simpler` install.
    # Its .pth meta-path hook takes precedence over sys.path and used to make
    # historical worktrees compile runtime sources from that unrelated checkout.
    # Shadow only that bootstrap hook while pip builds; the editable install
    # created in this venv then points at the correct worktree for benchmarks.
    bootstrap = Path(wt_dir) / ".venv" / "perf_bootstrap"
    bootstrap.mkdir(parents=True, exist_ok=True)
    (bootstrap / "_simpler_editable.py").write_text(
        "# Intentionally empty: shadow an outer editable install during build.\n")
    python_path = [str(bootstrap), str(Path(wt_dir) / "python"), str(wt_dir)]
    if env.get("PYTHONPATH"):
        python_path.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(python_path)
    print(f"  pip install --no-build-isolation -e . (rebuild, "
          f"PTO_ISA_ROOT={env.get('PTO_ISA_ROOT', 'unset')}) ...", flush=True)
    rc = subprocess.run(
        ["bash", "-lc",
         f"source {shlex.quote(str(cann_env_script))} && "
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
    elif entry.get("device_status") == "disabled":
        out += ["_Device benchmark disabled; host-only run._", ""]
    else:
        out += [f"_no summary (rc={entry.get('rc')})_", ""]
    for name, case in (entry.get("host") or {}).get("cases", {}).items():
        out += [f"### Host bind · {name}", ""]
        if case.get("status") != "ok":
            reason = case.get("error") or f"rc={case.get('rc')}"
            out += [f"_{case.get('status', 'not_run')}: {reason}_", ""]
            continue
        out += [
            f"{case.get('binds')} binds · {case.get('warm_binds')} warm · "
            f"NPU {case.get('device', '?')} · `{case.get('stamp', '')}`",
            "",
            "| Phase | Min (us) | Median (us) | Max (us) |",
            "|:--|--:|--:|--:|",
        ]
        for phase in ("control_plane", "host_orch", "graph_upload",
                      "arena_h2d"):
            metric = (case.get("metrics") or {}).get(phase)
            if metric:
                out.append(
                    f"| {phase} | {metric['min_us']:.1f} | "
                    f"{metric['median_us']:.1f} | {metric['max_us']:.1f} |")
        out.append("")
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
    ap.add_argument("--host-rounds", type=int, default=6,
                    help="rounds per HBG host case (default 6; first bind per "
                         "rank is dropped as cold)")
    ap.add_argument("--host-case", action="append", choices=HOST_CASES,
                    help="HBG host case to measure; repeat to select cases "
                         "(default: qwen3-14b and dsv4-flash)")
    ap.add_argument("--no-host", action="store_true",
                    help="disable the additional HBG host measurements")
    ap.add_argument("--host-only", action="store_true",
                    help="skip the Device benchmark and run only HBG host cases")
    ap.add_argument("--current-only", action="store_true",
                    help="benchmark the current (already built) tree once; "
                         "skip checkout/rebuild. For pipeline validation.")
    ap.add_argument("-d", "--device", default="0",
                    help="direct-mode NPU id; --task-submit always uses auto")
    ap.add_argument("--task-submit", action="store_true",
                    help="use task-submit only around benchmark execution; "
                         "worktree setup and builds remain outside the NPU lock")
    ap.add_argument("--task-wait-timeout", type=int, default=86400,
                    help="task-submit client wait timeout in seconds")
    ap.add_argument("--task-max-time", type=int, default=3600,
                    help="hard runtime limit for one benchmark task in seconds")
    ap.add_argument("-o", "--output", default="perf_history.json",
                    help="output json filename, relative to --workdir")
    args = ap.parse_args()
    if args.host_rounds < 2:
        ap.error("--host-rounds must be at least 2")
    host_cases = [] if args.no_host else [
        HOST_CASES[name] for name in (args.host_case or HOST_CASES)]
    device_enabled = not args.host_only
    if not host_cases and not device_enabled:
        ap.error("--host-only and --no-host select no benchmarks")

    global REPO, WORKDIR
    REPO = Path(args.repo).resolve()
    WORKDIR = Path(args.workdir).resolve()
    if not (REPO / ".git").exists():
        sys.exit(f"--repo {REPO} is not a git repo")
    WORKDIR.mkdir(parents=True, exist_ok=True)

    try:
        cann_env_script = resolve_cann_env_script()
        arch_precheck_script = resolve_arch_precheck_script(
            REPO, args.platform, args.task_submit)
    except RuntimeError as exc:
        sys.exit(f"benchmark environment error: {exc}")

    pin = pto_isa_pin()
    print(f"repo: {REPO}")
    print(f"workdir: {WORKDIR}")
    print(f"PTO-ISA pin: {pin or '(none found)'}")
    print(f"CANN env: {cann_env_script}")
    if arch_precheck_script:
        print(f"architecture precheck: {arch_precheck_script}")
    print(f"device: {'auto' if args.task_submit else args.device}")

    report = {
        "ref": args.ref,
        "runtime": args.runtime,
        "platform": args.platform,
        "rounds": args.rounds,
        "host_rounds": args.host_rounds,
        "host_cases": [case["name"] for case in host_cases],
        "device_benchmark": device_enabled,
        "pto_isa_commit": pin,
        "commits": [],
    }

    if args.current_only:
        sha = run(["git", "-C", str(REPO), "rev-parse", "HEAD"]).stdout.strip()
        subj = run(["git", "-C", str(REPO), "log", "-1", "--format=%s"]).stdout.strip()
        print(f"\n=== current tree {sha[:10]}  {subj} ===")
        WORKDIR.mkdir(parents=True, exist_ok=True)
        if device_enabled:
            out, rc = run_benchmark(
                REPO, args.device, args.rounds, args.runtime, args.platform,
                WORKDIR / "perf_current_raw.txt",
                task_submit=args.task_submit,
                task_wait_timeout=args.task_wait_timeout,
                task_max_time=args.task_max_time,
                cann_env_script=cann_env_script,
                arch_precheck_script=arch_precheck_script)
            summary = extract_summary_block(out)
            measured_device = (task_device_from_output(out)
                               if args.task_submit else args.device)
        else:
            rc, summary, measured_device = 0, "", None
        report["commits"].append(
            {"sha": sha, "subject": subj, "rc": rc, "summary": summary,
             "device": measured_device,
             "device_status": (("ok" if rc == 0 else "failed")
                               if device_enabled else "disabled"),
             "host": (measure_host_cases(
                 REPO, host_cases, args.host_rounds, WORKDIR, args.device,
                 task_submit=args.task_submit,
                 task_wait_timeout=args.task_wait_timeout,
                 task_max_time=args.task_max_time,
                 cann_env_script=cann_env_script,
                 arch_precheck_script=arch_precheck_script,
                 commit_sha=sha) if host_cases else
                 {"status": "disabled", "cases": {}})})
        if device_enabled:
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
            # A prior interrupted launcher may have left its task alive.  A
            # process-specific path prevents a later retry from deleting files
            # underneath that task.
            wt = wt_root / f"{sha[:12]}-auto-p{os.getpid()}"
            entry = {"sha": sha, "subject": subj, "rc": None, "summary": "",
                     "device": None if args.task_submit else args.device,
                     "device_status": "pending" if device_enabled else "disabled",
                     "host": {"status": "not_run", "cases": {}}}
            preserve_worktree = False
            abort = None
            try:
                if not setup_worktree(sha, wt, cann_env_script):
                    entry["rc"] = "build_failed"
                    continue
                patch_example_map(wt)
                if device_enabled:
                    out, rc = run_benchmark(
                        wt, args.device, args.rounds, args.runtime,
                        args.platform, wt / "bench_raw.txt", venv=wt / ".venv",
                        task_submit=args.task_submit,
                        task_wait_timeout=args.task_wait_timeout,
                        task_max_time=args.task_max_time,
                        cann_env_script=cann_env_script,
                        arch_precheck_script=arch_precheck_script)
                    entry["summary"] = extract_summary_block(out)
                    entry["rc"] = rc
                    entry["device_status"] = "ok" if rc == 0 else "failed"
                    if args.task_submit:
                        entry["device"] = task_device_from_output(out)
                    print(f"  captured summary: "
                          f"{'yes' if entry['summary'] else 'no'} (rc={rc})")
                else:
                    entry["rc"] = 0
                    entry["device"] = None
                entry["host"] = (measure_host_cases(
                    wt, host_cases, args.host_rounds, wt, args.device,
                    venv=wt / ".venv", task_submit=args.task_submit,
                    task_wait_timeout=args.task_wait_timeout,
                    task_max_time=args.task_max_time,
                    cann_env_script=cann_env_script,
                    arch_precheck_script=arch_precheck_script,
                    commit_sha=sha) if host_cases else
                    {"status": "disabled", "cases": {}})
            except TaskStillRunning as exc:
                # Never remove a worktree that an orphaned task may still be
                # using. Stop this shard so it cannot queue more work behind
                # that unknown task.
                preserve_worktree = True
                abort = exc
                entry["rc"] = "task_still_running"
                print(f"  FATAL: {exc}; preserving {wt}", file=sys.stderr,
                      flush=True)
            except Exception as exc:
                entry["rc"] = "driver_failed"
                print(f"  DRIVER FAILED: {exc}", file=sys.stderr, flush=True)
            finally:
                # Preserve logs (build + benchmark) before removing worktree.
                measured = entry.get("device") or "auto"
                keep = log_root / sha[:12] / f"d{measured}-p{os.getpid()}"
                keep.mkdir(parents=True, exist_ok=True)
                logs = [wt / "bench_raw.txt", wt / ".venv" / "build.log"]
                logs.extend(sorted(wt.glob("host_bind_*_raw.txt")))
                for src in logs:
                    if src.exists():
                        shutil.copy(src, keep / src.name)
                if (wt / "outputs").exists():
                    shutil.copytree(wt / "outputs", keep / "outputs",
                                    dirs_exist_ok=True)
                report["commits"].append(entry)
                # Crash-safe incremental append (one commit at a time, locked).
                if args.append_jsonl:
                    append_locked(args.append_jsonl, json.dumps(entry) + "\n")
                if args.append_md:
                    append_locked(args.append_md, commit_md_section(entry))
                if not preserve_worktree:
                    teardown_worktree(wt)
            if abort is not None:
                raise abort

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
