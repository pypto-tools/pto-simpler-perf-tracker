#!/usr/bin/env python3
"""Produce a read-only daily report of flaky GitHub CI failures.

The scanner records failed target jobs and revisits them on later days.  A
failure is marked as resolved when a later attempt of the same workflow run,
commit, and target job succeeds.  It never starts, cancels, or reruns Actions.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ci_weekly_report import (
    GitHubClient,
    TARGET_JOBS,
    canonical_job,
    network_utc,
    resolve_github_token,
)


REPORT_TZ = ZoneInfo("Asia/Shanghai")
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SECRET_RE = re.compile(
    r"(?i)(bearer\s+|token[=:]\s*|password[=:]\s*|secret[=:]\s*)\S+")
FAILURE_RE = re.compile(
    r"(?i)(error|exception|failed|failure|fatal|timeout|timed out|"
    r"connection reset|no such file|segmentation fault|assertionerror|"
    r"device.*(error|fault|timeout)|exit code)"
)


def _state_dir():
    script_dir = Path(__file__).resolve().parent
    if os.environ.get("PTO_TOOL_ROOT"):
        return Path(os.environ["PTO_TOOL_ROOT"]) / "state" / "ci-daily"
    if script_dir.name == "app":
        return script_dir.parent / "state" / "ci-daily"
    return script_dir / "runtime" / "state" / "ci-daily"


def _iso(value):
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def day_bounds(label):
    day = dt.date.fromisoformat(label)
    start = dt.datetime.combine(day, dt.time(), tzinfo=REPORT_TZ)
    return start.astimezone(dt.timezone.utc), (start + dt.timedelta(days=1)).astimezone(dt.timezone.utc)


def previous_day(now):
    return (now.astimezone(REPORT_TZ).date() - dt.timedelta(days=1)).isoformat()


def _save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _normalise_line(line):
    line = ANSI_RE.sub("", line).strip()
    line = re.sub(r"\b[0-9a-f]{7,40}\b", "<sha>", line, flags=re.I)
    line = re.sub(r"\b(?:run|job|runner|attempt)[_ -]?\d+\b", "<id>", line, flags=re.I)
    line = re.sub(r"\b\d{4}-\d\d-\d\d[T ][0-9:.+-]+Z?\b", "<time>", line)
    line = re.sub(r"/(?:tmp|home|runner|workspace|build)/[^ ]+", "<path>", line)
    line = SECRET_RE.sub(r"\1<redacted>", line)
    return line[:500]


def failure_signature(log_text):
    """Return a stable, short signature and a few useful sanitized lines."""
    lines = [_normalise_line(line) for line in log_text.splitlines()]
    lines = [line for line in lines if line]
    candidates = [line for line in lines if FAILURE_RE.search(line)]
    selected = candidates[-8:] if candidates else lines[-8:]
    selected = list(dict.fromkeys(selected))
    basis = "\n".join(selected) or "no-log-content"
    return {
        "pattern_id": hashlib.sha256(basis.encode()).hexdigest()[:16],
        "summary": selected[-1] if selected else "no log content",
        "evidence": selected[-4:],
    }


def _run_jobs(client, repo, run_id, attempt):
    path = f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs"
    return client.paginate(path, "jobs")


def _download_job_log(client, repo, job_id, max_bytes=512 * 1024):
    path = f"{client.api_url}/repos/{repo}/actions/jobs/{job_id}/logs"
    request = urllib.request.Request(path, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {client.token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "pto-simpler-perf-tracker",
    })
    # GitHub returns a short-lived redirect to object storage.  Do not forward
    # the GitHub bearer token to that second host.
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, new):
            return None

    try:
        response = urllib.request.build_opener(_NoRedirect()).open(request, timeout=60)
    except urllib.error.HTTPError as exc:
        if exc.code not in (301, 302, 303, 307, 308):
            raise
        location = exc.headers.get("Location")
        if not location:
            raise
        response = urllib.request.urlopen(urllib.request.Request(location), timeout=60)
    with response:
        data = response.read()
    if len(data) > max_bytes:
        data = data[-max_bytes:]
    return data.decode(errors="replace")


def _job_record(client, repo, run, attempt, job):
    name = canonical_job(job.get("name", ""))
    if name is None or job.get("conclusion") == "skipped":
        return None
    result = {
        "run_id": run["id"],
        "run_attempt": attempt,
        "head_sha": run.get("head_sha"),
        "head_branch": run.get("head_branch"),
        "run_url": run.get("html_url"),
        "pr_number": run.get("pr_number") or next(
            (pr.get("number") for pr in (run.get("pull_requests") or [])
             if pr.get("number") is not None), None),
        "job_id": job["id"],
        "job": name,
        "conclusion": job.get("conclusion"),
        "failed_step": next((s.get("name") for s in reversed(job.get("steps") or [])
                             if s.get("conclusion") == "failure"), None),
    }
    if result["conclusion"] == "failure":
        try:
            result["pattern"] = failure_signature(
                _download_job_log(client, repo, job["job_id"] if "job_id" in job else job["id"]))
        except (OSError, ValueError, TimeoutError) as exc:
            result["pattern"] = failure_signature(
                f"log unavailable: {type(exc).__name__}")
    return result


def _attempt_records(client, repo, run):
    if not run.get("pr_number") and run.get("head_sha"):
        try:
            pulls = client.request(
                "GET", f"/repos/{repo}/commits/{run['head_sha']}/pulls")
            if pulls:
                run = {**run, "pr_number": pulls[0].get("number")}
        except Exception:
            # PR lookup is enrichment only; CI failure collection must continue.
            pass
    attempts = max(1, int(run.get("run_attempt") or 1))
    records = []
    for attempt in range(1, attempts + 1):
        for job in _run_jobs(client, repo, run["id"], attempt):
            record = _job_record(client, repo, run, attempt, job)
            if record is not None:
                records.append(record)
    return records


def list_day_runs(client, repo, workflow, start, end):
    import urllib.parse
    workflow_id = urllib.parse.quote(workflow, safe="")
    query = urllib.parse.urlencode({
        "event": "pull_request", "status": "completed",
        "created": f"{_iso(start)}..{_iso(end - dt.timedelta(seconds=1))}",
    })
    return client.paginate(
        f"/repos/{repo}/actions/workflows/{workflow_id}/runs?{query}",
        "workflow_runs")


def scan_day(client, repo, workflow, label, pending):
    start, end = day_bounds(label)
    runs = {run["id"]: run for run in list_day_runs(client, repo, workflow, start, end)}
    pending_ids = {int(item["run_id"]) for item in pending}
    for run_id in pending_ids:
        if run_id not in runs:
            payload = client.request("GET", f"/repos/{repo}/actions/runs/{run_id}")
            runs[run_id] = payload

    failures, resolved = [], []
    for run in runs.values():
        records = _attempt_records(client, repo, run)
        by_job = {}
        for record in records:
            by_job.setdefault(record["job"], []).append(record)
        for job, attempts in by_job.items():
            failed = [r for r in attempts if r["conclusion"] == "failure"]
            succeeded = [r for r in attempts if r["conclusion"] == "success"]
            if not failed:
                continue
            last_failure = failed[-1]
            later_success = next((r for r in succeeded
                                  if r["run_attempt"] > last_failure["run_attempt"]), None)
            if later_success:
                resolved.append({
                    "status": "resolved_by_rerun",
                    "pattern_id": last_failure.get("pattern", {}).get("pattern_id"),
                    "pattern": last_failure.get("pattern", {}),
                    "failed": last_failure,
                    "resolved": later_success,
                })
            else:
                failures.append(last_failure)
    return failures, resolved


def summarize_patterns(state_dir, current_label, failures, resolved, lookback=3):
    """Aggregate the same normalized failure across distinct PRs and runs."""
    events = []
    current_day = dt.date.fromisoformat(current_label)
    cutoff = current_day - dt.timedelta(days=lookback - 1)
    for path in sorted(state_dir.glob("*.json")):
        if path.name in {"pending.json", f"{current_label}.json"}:
            continue
        try:
            report_day = dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        if report_day < cutoff or report_day > current_day:
            continue
        payload = _load_json(path, {})
        events.extend(payload.get("failures", []))
        events.extend({**item.get("failed", {}), "_resolved": True}
                      for item in payload.get("resolved", []))
    events.extend(failures)
    events.extend({**item.get("failed", {}), "_resolved": True} for item in resolved)
    buckets = {}
    for event in events:
        pattern = event.get("pattern", {})
        pattern_id = pattern.get("pattern_id")
        if not pattern_id:
            continue
        bucket = buckets.setdefault(pattern_id, {
            "pattern_id": pattern_id,
            "summary": pattern.get("summary", "—"),
            "evidence": pattern.get("evidence", []),
            "runs": set(), "prs": set(), "jobs": set(),
            "occurrences": 0, "resolved": 0,
        })
        bucket["occurrences"] += 1
        if event.get("_resolved"):
            bucket["resolved"] += 1
        bucket["runs"].add(str(event.get("run_id")))
        change_ref = (event.get("pr_number") or event.get("head_branch")
                      or event.get("head_sha") or event.get("run_id"))
        bucket["prs"].add(str(change_ref))
        if event.get("job"):
            bucket["jobs"].add(event["job"])
    result = []
    for bucket in buckets.values():
        if len(bucket["prs"]) < 2:
            continue
        result.append({
            "pattern_id": bucket["pattern_id"],
            "summary": bucket["summary"],
            "evidence": bucket["evidence"],
            "pr_count": len(bucket["prs"]),
            "run_count": len(bucket["runs"]),
            "occurrences": bucket["occurrences"],
            "resolved_by_rerun": bucket["resolved"],
            "jobs": sorted(bucket["jobs"]),
        })
    if not result:
        # A full-log signature can be too specific when two PRs hit the same
        # terminal error with different surrounding output.  Use the stable
        # summary as a fallback grouping key so the section does not miss
        # obvious cross-PR repetition.
        fallback = {}
        for event in events:
            pattern = event.get("pattern", {})
            summary = pattern.get("summary") or "—"
            bucket = fallback.setdefault(summary, {
                "summary": summary, "evidence": pattern.get("evidence", []),
                "runs": set(), "prs": set(), "jobs": set(),
                "occurrences": 0, "resolved": 0,
            })
            bucket["occurrences"] += 1
            bucket["resolved"] += int(bool(event.get("_resolved")))
            bucket["runs"].add(str(event.get("run_id")))
            change_ref = (event.get("pr_number") or event.get("head_branch")
                          or event.get("head_sha") or event.get("run_id"))
            bucket["prs"].add(str(change_ref))
            if event.get("job"):
                bucket["jobs"].add(event["job"])
        for bucket in fallback.values():
            if len(bucket["prs"]) < 2:
                continue
            result.append({
                "pattern_id": "summary-" + hashlib.sha256(
                    bucket["summary"].encode()).hexdigest()[:12],
                "summary": bucket["summary"],
                "evidence": bucket["evidence"],
                "pr_count": len(bucket["prs"]),
                "run_count": len(bucket["runs"]),
                "occurrences": bucket["occurrences"],
                "resolved_by_rerun": bucket["resolved"],
                "jobs": sorted(bucket["jobs"]),
            })
    return sorted(result, key=lambda item: (-item["pr_count"], -item["occurrences"], item["pattern_id"]))


def render_report(label, failures, resolved, common_patterns=()):
    def cell(value, limit=180):
        text = str(value or "—").replace("|", "\\|").replace("\n", " ")
        return text if len(text) <= limit else text[:limit - 1] + "…"

    def failure_detail(item):
        pattern = item.get("pattern", {})
        evidence = pattern.get("evidence") or []
        detail = [pattern.get("summary", "—"), *evidence]
        return list(dict.fromkeys(str(line) for line in detail if line))

    def table(headers, rows):
        widths = [max(len(str(headers[i])), *(len(str(row[i])) for row in rows))
                  for i in range(len(headers))]
        render = lambda row: "  ".join(str(row[i]).ljust(widths[i])
                                        for i in range(len(headers))).rstrip()
        return [render(headers), "  ".join("-" * width for width in widths)] + [
            render(row) for row in rows]

    lines = [f"# CI 日报 {label}", "", "## 问题汇总", "", "```text"]
    entries = []
    for item in resolved:
        failed = item["failed"]
        entries.append(("重跑恢复", failed))
    for item in failures:
        entries.append(("待复查", item))
    rows = []
    for status, item in entries:
        pattern = item.get("pattern", {})
        run_id = str(item.get("run_id") or "—")
        error = " | ".join(failure_detail(item))
        pattern_id = pattern.get("pattern_id")
        if pattern_id:
            error = f"{error} (`{pattern_id}`)"
        rows.append((status, cell(item.get("job"), 24), cell(item.get("failed_step"), 32),
                     cell(error, 240), run_id))
    if not rows:
        rows.append(("—", "—", "—", "无问题", "—"))
    lines += table(("状态", "Job", "失败步骤", "错误摘要", "Run ID"), rows)
    lines += ["```", ""]

    lines += ["## 失败详情", "", "```text"]
    details = []
    detail_entries = [("重跑恢复", entry["failed"]) for entry in resolved]
    detail_entries.extend(("待复查", entry) for entry in failures)
    for status, item in detail_entries:
        details.append(
            f"[{status}] {item.get('job', '—')} / "
            f"{item.get('failed_step') or '未知步骤'} / "
            f"Run {item.get('run_id') or '—'} / "
            f"{item.get('run_url') or '无链接'}")
        details.extend(f"  {line}" for line in failure_detail(item)[:8])
        details.append("")
    lines += details or ["无失败详情"]
    lines += ["```"]

    lines += ["## 多个 PR 的共性问题", "", "```text"]
    common_rows = []
    for item in common_patterns:
        common_summary = " | ".join(dict.fromkeys(
            [str(item.get("summary", "—")), *(item.get("evidence") or [])]))
        common_rows.append((cell(item['pattern_id'], 20), cell(common_summary, 180),
                            item['pr_count'], item['run_count'], item['occurrences'],
                            item['resolved_by_rerun'], cell(', '.join(item['jobs']), 24)))
    if not common_rows:
        common_rows.append(("—", "近 3 天没有同一失败模式出现在多个 PR", 0, 0, 0, 0, "—"))
    lines += table(
        ("Pattern", "错误摘要", "PR/分支数", "Run 数", "出现次数", "重跑恢复", "Jobs"),
        common_rows)
    lines += ["```"]
    return "\n".join(lines) + "\n"


def publication_url(state_dir):
    configured = os.environ.get("FEISHU_CI_DAILY_URL")
    if configured:
        return configured
    state = state_dir / "feishu-state.json"
    payload = _load_json(state, {})
    index_doc = payload.get("index_doc")
    if index_doc:
        domain = os.environ.get("FEISHU_DOMAIN", "hw-native-sys.feishu.cn")
        return f"https://{domain}/docx/{index_doc}"
    doc_id = payload.get("doc_id")
    if doc_id:
        domain = os.environ.get("FEISHU_DOMAIN", "hw-native-sys.feishu.cn")
        return f"https://{domain}/docx/{doc_id}"
    return None


def _week_key(label):
    day = dt.date.fromisoformat(label)
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def _sync_daily_index(token, index_doc, periods, legacy_doc, domain):
    from feishu_perf_report import clear_doc, docx_append, _heading_block, _text_block

    clear_doc(token, index_doc)
    blocks = [_heading_block(2, "Simpler GitHub CI 每日扫描")]
    if legacy_doc:
        blocks.append(_text_block(
            f"📦 历史单文档（旧版） → https://{domain}/docx/{legacy_doc}"))
    blocks.extend(
        _text_block(
        f"📅 {period} → https://{domain}/docx/{periods[period]['doc_id']}")
        for period in sorted(periods, reverse=True)
        if periods[period].get("doc_id"))
    docx_append(token, index_doc, blocks)


def publish_daily_report(state_dir, label, report):
    """Publish one dated report into an ISO-week document behind an index."""
    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    if not (app_id and app_secret):
        return None
    from feishu_perf_report import (
        docx_append, docx_create, md_to_blocks, set_doc_link_editable,
        tenant_token,
    )
    state_path = state_dir / "feishu-state.json"
    state = _load_json(state_path, {
        "doc_id": None, "index_doc": None, "weeks": {}, "published_days": {},
    })
    if label in state.get("published_days", {}):
        return publication_url(state_dir)
    token = tenant_token(app_id, app_secret)
    domain = os.environ.get("FEISHU_DOMAIN", "hw-native-sys.feishu.cn")
    legacy_doc = (os.environ.get("FEISHU_CI_DAILY_DOCX_TOKEN")
                  or state.get("doc_id"))
    index_doc = state.get("index_doc")
    if not index_doc:
        index_doc = docx_create(token, "Simpler GitHub CI 每日扫描 · 索引")
        state["index_doc"] = index_doc
        state["index_managed"] = True
        try:
            set_doc_link_editable(token, index_doc, "tenant")
        except SystemExit:
            pass

    week = _week_key(label)
    week_state = state.setdefault("weeks", {}).setdefault(week, {})
    week_doc = week_state.get("doc_id")
    if not week_doc:
        week_doc = docx_create(token, f"Simpler GitHub CI 报错 {week}")
        week_state["doc_id"] = week_doc
        week_state["managed"] = True
        week_state.setdefault("days", {})
        try:
            set_doc_link_editable(token, week_doc, "tenant")
        except SystemExit:
            pass

    blocks = md_to_blocks(f"## CI 每日扫描 {label}\n\n{report}")
    docx_append(token, week_doc, blocks)
    week_state.setdefault("days", {})[label] = True
    state.setdefault("published_days", {})[label] = True
    _sync_daily_index(token, index_doc, state["weeks"], legacy_doc, domain)
    _save_json(state_path, state)
    return publication_url(state_dir)


def notify_if_needed(script_dir, state_dir, label, failures, resolved, common_patterns, report_url=None):
    """Send one soft-failing Feishu DM when the report contains a finding."""
    if not (failures or resolved or common_patterns):
        return
    lines = [f"**日期**: {label}"]
    if failures:
        lines.append(f"**待复查失败**: {len(failures)} 条")
    if resolved:
        lines.append(f"**重跑后恢复**: {len(resolved)} 条")
    if common_patterns:
        lines.append(f"**多个 PR 共性 pattern**: {len(common_patterns)} 个")
        for item in common_patterns[:5]:
            lines.append(
                f"- `{item['pattern_id']}`: {item['pr_count']} PR / "
                f"{item['occurrences']} 次 · {item['summary']}")
    url = report_url or publication_url(state_dir)
    lines.append(f"日报: {url or f'{label}.md'}")
    subprocess.run([
        sys.executable, str(Path(script_dir) / "notify_feishu.py"),
        "--status", "fail" if (failures or common_patterns) else "info",
        "--title", f"CI 每日扫描发现问题 ({label})",
        *sum((["--line", line] for line in lines), []),
    ], check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="hw-native-sys/simpler")
    parser.add_argument("--workflow", default="ci.yml")
    parser.add_argument("--day", help="Beijing date YYYY-MM-DD; default previous day")
    parser.add_argument("--state-dir", type=Path, default=_state_dir())
    parser.add_argument("--notify", action="store_true",
                        help="send a Feishu DM when the report has findings")
    args = parser.parse_args()
    now = network_utc()
    if now is None:
        raise SystemExit("network time unavailable; refusing to scan")
    label = args.day or previous_day(now)
    day_bounds(label)  # validate before making API calls
    args.state_dir.mkdir(parents=True, exist_ok=True)
    pending_path = args.state_dir / "pending.json"
    pending = _load_json(pending_path, [])
    client = GitHubClient(resolve_github_token(), os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    failures, resolved = scan_day(client, args.repo, args.workflow, label, pending)
    common_patterns = summarize_patterns(args.state_dir, label, failures, resolved)
    report = render_report(label, failures, resolved, common_patterns)
    _save_json(args.state_dir / f"{label}.json", {
        "schema_version": 1, "day": label, "repository": args.repo,
        "workflow": args.workflow, "failures": failures, "resolved": resolved,
        "common_patterns": common_patterns,
    })
    (args.state_dir / f"{label}.md").write_text(report)
    _save_json(pending_path, failures)
    if args.notify:
        report_url = publish_daily_report(args.state_dir, label, report)
        from report_hub import sync_report_hub
        from feishu_perf_report import tenant_token
        report_url = sync_report_hub(
            tenant_token(os.environ["FEISHU_APP_ID"],
                         os.environ["FEISHU_APP_SECRET"]),
            args.state_dir.parent,
            os.environ.get("FEISHU_DOMAIN", "hw-native-sys.feishu.cn"),
        )
        notify_if_needed(Path(__file__).resolve().parent, args.state_dir, label,
                         failures, resolved, common_patterns, report_url)
    print(report, end="")


if __name__ == "__main__":
    raise SystemExit(main())
