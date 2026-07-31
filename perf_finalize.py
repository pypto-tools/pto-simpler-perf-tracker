#!/usr/bin/env python3
# Copyright (c) PyPTO Contributors.
# Post-process a parallel perf_history run WITHOUT touching the raw data.
#
# Reads (read-only):
#   tmp/perf_history.jsonl             raw per-commit records (append-only)
#   tmp/perf_shard_*.log               to recover which NPU each commit ran on
#   tmp/perf_history_processed.jsonl   the PRIOR run's output, for one baseline
#                                      commit (the cross-run Δ anchor)
# Writes (separate files; raw md/jsonl are never modified):
#   tmp/perf_history_processed.jsonl   device-tagged, parsed, ordered, +deltas
#   tmp/perf_history_processed.md      markdown tables, newest commit first
#
# Processing:
#   - back-fill the NPU device per commit (authoritative, from task records)
#   - parse the verbatim summary into per-example metrics
#   - order newest commit first (by commit time)
#   - compute each metric's change vs the previous (older) commit
#   - carry one baseline commit across runs: the daily run truncates the raw
#     jsonl to a single ~36h window, so the window's oldest commit has no
#     in-window predecessor. We re-read the previous run's out-jsonl and pull in
#     its newest still-relevant commit purely to anchor that boundary Δ.
#
# Device recovery: each shard log records its task id + locked device; the
# task's recorded command (.sh) holds the exact --commit-list -> sha->device.

import argparse
import glob
import json
import re
import subprocess
from pathlib import Path

# REPO (the target clone) is set in main() from --repo; used only for commit
# timestamps when ordering. TASKLOG_DIRS is auto-probed to recover device ids.
REPO = Path(".")
TASKLOG_DIRS = ["/var/lib/taskqueue/logs",
                str(Path.home() / ".taskqueue" / "logs")]
METRICS = ["Host", "Device", "Total", "Sched", "Orch"]


def task_cmd_text(task_id):
    for d in TASKLOG_DIRS:
        for ext in (".sh", ".log"):
            p = Path(d) / f"{task_id}{ext}"
            if p.exists():
                return p.read_text(errors="replace")
    return ""


def sha_to_device(shard_glob):
    mapping = {}
    for log in sorted(glob.glob(shard_glob)):
        text = Path(log).read_text(errors="replace")
        tid = re.search(r"(task_\d+_\d+_\d+)", text)
        dev = re.search(r"device:\s*(\d+)", text)
        if not (tid and dev):
            continue
        m = re.search(r"--commit-list '([^']*)'", task_cmd_text(tid.group(1)))
        if not m:
            continue
        for sha in re.split(r"[\s,]+", m.group(1)):
            if sha:
                mapping[sha] = dev.group(1)
    return mapping


def parse_summary(text):
    """Verbatim summary block -> {example: {metric: value}} (best-effort).

    Maps by the header's *positional* "(us)" columns so an extra column the
    benchmark prints but METRICS doesn't track (e.g. "Effective (us)") and an
    extra non-numeric leading column ("Mode") don't shift the mapping. The data
    row's trailing N numbers (N = number of "(us)" columns) align 1:1 with the
    header's "(us)" labels in order; we then keep only the METRICS we track.
    """
    lines = text.splitlines()
    hdr_i = next((i for i, l in enumerate(lines) if "Example" in l and "(us)" in l), None)
    if hdr_i is None:
        return {}
    hdr_cols = re.findall(r"(\w+) \(us\)", lines[hdr_i])  # ordered, incl. untracked
    if not hdr_cols:
        return {}
    ncol = len(hdr_cols)
    res = {}
    for line in lines[hdr_i + 1:]:
        if not line.strip() or set(line.strip()) <= {"-", " "} or "====" in line:
            continue
        toks = line.split()
        if len(toks) < ncol + 1:
            continue
        vals = toks[-ncol:]
        if not all(re.fullmatch(r"-|[0-9.]+", v) for v in vals):
            continue
        name = " ".join(toks[:len(toks) - ncol])
        row = {hdr_cols[k]: (None if vals[k] == "-" else float(vals[k]))
               for k in range(ncol)}
        res[name] = {m: row[m] for m in METRICS if m in row}
    return res


def commit_field(sha, fmt):
    r = subprocess.run(["git", "-C", str(REPO), "show", "-s", f"--format={fmt}", sha],
                       capture_output=True, text=True)
    return r.stdout.strip()


def commit_time(sha):
    try:
        return int(commit_field(sha, "%ct"))
    except ValueError:
        return 0


def entry_time(e):
    """Commit time, cached on the entry so baseline commits aren't re-git'd."""
    if not e.get("ct"):
        e["ct"] = commit_time(e["sha"])
    return e["ct"]


def carry_baseline(prev_jsonl, window_shas):
    """The single newest already-processed commit to anchor the window's oldest
    commit's Δ across runs.

    The daily run truncates the raw jsonl to one 36h window, so the window's
    oldest commit has no in-window predecessor and its Δ would be blank. We
    recover exactly one neighbour: the newest commit from the previous run's
    output (our local copy of what's in the Feishu doc) that has metrics and is
    not itself in this window. Returns None when there's nothing usable.
    """
    p = Path(prev_jsonl)
    if not p.exists():
        return None
    prev = []
    for line in p.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("sha") in window_shas or not e.get("metrics"):
            continue
        prev.append(e)
    if not prev:
        return None
    return max(prev, key=entry_time)


def published_metrics(prev_jsonl):
    """{sha: prior-run entry} for every already-published commit (has metrics).

    The prior run's out-jsonl is our local mirror of what's in the Feishu doc.
    A published commit's row is frozen there (push dedups by sha), so its metrics
    must also be frozen as the delta baseline — otherwise a fresh re-measurement
    of that commit (the 36h overlap re-benchmarks it) would make the value used
    to compute a newer commit's Δ disagree with the value displayed in the doc.
    Commits that failed to produce metrics last run are absent here, so they are
    still re-measured this run (crash recovery preserved).
    """
    p = Path(prev_jsonl)
    if not p.exists():
        return {}
    pub = {}
    for line in p.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("sha") and e.get("metrics"):
            pub[e["sha"]] = e
    return pub


def _fmt(v):
    return "" if v is None else f"{v:.1f}"


# Flag a change as a regression / improvement when it crosses this magnitude.
DELTA_FLAG_PCT = 5.0


def _cellv(cur, prev):
    """'2704.0 (-3.0%)' — value with inline change vs previous commit."""
    s = _fmt(cur)
    if cur is not None and prev not in (None, 0):
        pct = (cur - prev) / prev * 100
        mark = " 🔺" if pct >= DELTA_FLAG_PCT else (" 🔻" if pct <= -DELTA_FLAG_PCT else "")
        s += f" ({pct:+.1f}%{mark})"
    return s


# Annotate the inline change on every metric column.
DELTA_COLS = set(METRICS)
# Commit-level alert when a device-side metric regresses by >= this much
# (Host excluded — host wall is noisy).
ALERT_PCT = 10.0
ALERT_COLS = {"Device", "Total", "Sched", "Orch"}
ALERT_MIN_US = 50.0  # ignore tiny-baseline metrics (e.g. ~6µs Orch)


def commit_alerts(metrics, prev):
    al = []
    for ex, m in (metrics or {}).items():
        pm = (prev or {}).get(ex, {})
        for c in ALERT_COLS:
            cur, p = m.get(c), pm.get(c)
            if cur is not None and p and p >= ALERT_MIN_US:
                pct = (cur - p) / p * 100
                if pct >= ALERT_PCT:
                    al.append((pct, ex, c))
    al.sort(reverse=True)
    return al


def alert_line(al):
    top = "; ".join(f"{ex} {c} +{pct:.0f}%" for pct, ex, c in al[:5])
    return f"⚠️ 重点回归: {top}" + (" …" if len(al) > 5 else "")


def commit_table_md(entry, prev_metrics):
    """Markdown table for one commit: metrics + Δ vs previous commit."""
    sha, dev, date = entry["sha"][:10], entry.get("device"), entry.get("date", "")
    meta = " · ".join(filter(None, [f"`{sha}`", date,
                                    f"NPU {dev}" if dev else ""]))
    metrics = entry.get("metrics") or {}
    alerts = commit_alerts(metrics, prev_metrics)
    title = ("⚠️ " if alerts else "") + entry["subject"]
    head = [f"### {title}", "", meta, ""]
    if alerts:
        head += [f"> {alert_line(alerts)}", ""]
    if not metrics:
        return "\n".join(head + [f"_no parseable summary (rc={entry.get('rc')})_",
                                 "", "```text", entry.get("summary") or "", "```", ""])
    cols = entry.get("present") or METRICS
    header = ["Example"] + [f"{c} (us)" for c in cols]
    rows = ["| " + " | ".join(header) + " |",
            "|" + ":--|" + "--:|" * (len(header) - 1)]
    for ex in sorted(metrics):
        m = metrics[ex]
        pm = (prev_metrics or {}).get(ex, {})
        cells = [ex] + [_cellv(m.get(c), pm.get(c)) if c in DELTA_COLS
                        else _fmt(m.get(c)) for c in cols]
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(head + rows + [""])


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo", required=True,
                    help="target clone (for commit timestamps when ordering)")
    ap.add_argument("--jsonl", required=True, help="raw input jsonl (read-only)")
    ap.add_argument("--out-jsonl", required=True)
    ap.add_argument("--out-md", required=True)
    ap.add_argument("--shard-glob", required=True,
                    help="glob for shard logs, e.g. <workdir>/perf_shard_*.log")
    args = ap.parse_args()

    global REPO
    REPO = Path(args.repo).resolve()
    dev_map = sha_to_device(args.shard_glob)
    published = published_metrics(args.out_jsonl)
    # The raw jsonl may hold several lines for one sha: a card that wedged
    # mid-run writes an rc=1 line, then the commit is re-benchmarked on a fresh
    # card (see perf_history_parallel.sh's card-switch retry) and writes an
    # rc=0 line. Collapse to one entry per sha, preferring a successful
    # measurement over a failure, else the latest line.
    def _good(e):
        return e.get("rc") == 0 and bool((e.get("summary") or "").strip())
    by_sha = {}
    for l in open(args.jsonl):
        if not l.strip():
            continue
        e = json.loads(l)
        prev = by_sha.get(e["sha"])
        if prev is None or _good(e) or not _good(prev):
            by_sha[e["sha"]] = e
    entries = list(by_sha.values())
    for e in entries:
        if not e.get("device"):
            e["device"] = dev_map.get(e["sha"])
        e["date"] = commit_field(e["sha"], "%cs")  # YYYY-MM-DD
        frozen = published.get(e["sha"])
        if frozen:
            # Already published: freeze its metrics so the value displayed in the
            # doc stays identical to the baseline used for newer commits' Δ.
            e["metrics"] = frozen.get("metrics") or {}
            e["present"] = frozen.get("present") or []
            if frozen.get("device"):
                e["device"] = frozen["device"]
        else:
            m = parse_summary(e.get("summary") or "")
            e["metrics"] = m
            e["present"] = [k for k in METRICS
                            if any(k in v for v in m.values())] if m else []
        entry_time(e)  # cache ct so the next run can reuse it as a baseline

    # Anchor the window's oldest commit to the newest commit from the previous
    # run's output, so the first commit of each daily window still gets a Δ.
    carry = carry_baseline(args.out_jsonl, {e["sha"] for e in entries})
    if carry is not None:
        entries.append(carry)
    entries.sort(key=entry_time, reverse=True)

    # Deltas compare each commit to the next (older) one in the ordering.
    Path(args.out_jsonl).write_text(
        "".join(json.dumps(e) + "\n" for e in entries))

    devs = sorted({e.get("device") for e in entries if e.get("device")})
    dates = [e["date"] for e in entries if e.get("date")]
    span = f"{dates[-1]} → {dates[0]}" if dates else "?"
    ok = sum(1 for e in entries if e.get("metrics"))

    # Top-level watchlist of commits with a large regression.
    flagged = []
    for i, e in enumerate(entries):
        prev = entries[i + 1]["metrics"] if i + 1 < len(entries) else None
        al = commit_alerts(e.get("metrics"), prev)
        if al:
            flagged.append((e, al))

    out = [
        "# Simpler 性能历史",
        "",
        f"> {len(entries)} 个 commit({ok} 个有数据) · {span} · "
        f"NPU {', '.join(devs) or '?'} · 新→旧",
        ">",
        "> Δ = 相对上一个(更老)commit 的变化 · "
        f"🔺 变慢 ≥{DELTA_FLAG_PCT:.0f}% · 🔻 变快 ≥{DELTA_FLAG_PCT:.0f}% · "
        f"⚠️ = 设备侧指标回归 ≥{ALERT_PCT:.0f}%",
        "",
    ]
    if flagged:
        out += [f"## ⚠️ 重点关注({len(flagged)} 个 commit)", ""]
        for e, al in flagged:
            worst = al[0]
            out.append(f"- ⚠️ `{e['sha'][:10]}` {e['subject']} — "
                       f"{worst[1]} {worst[2]} **+{worst[0]:.0f}%**"
                       + (f"(+{len(al) - 1} 项)" if len(al) > 1 else ""))
        out += ["", "---", ""]
    else:
        out += ["---", ""]
    last_date = None
    for i, e in enumerate(entries):
        prev = entries[i + 1]["metrics"] if i + 1 < len(entries) else None
        if e.get("date") != last_date:
            last_date = e.get("date")
            out.append(f"## 📅 {last_date or '未知日期'}\n")
        out.append(commit_table_md(e, prev))
    Path(args.out_md).write_text("\n".join(out))
    print(f"processed {len(entries)} commits (NPU {devs})")
    print(f"  raw kept:   {args.jsonl}  (untouched)")
    print(f"  processed:  {args.out_jsonl}")
    print(f"  md:         {args.out_md}")


if __name__ == "__main__":
    main()
