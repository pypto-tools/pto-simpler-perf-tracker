#!/usr/bin/env python3
"""Collect weekly GitHub CI timings and publish the durable Feishu history.

The module is independent from the NPU benchmark path.  It reads completed
pull-request runs through the GitHub REST API, keeps a sanitized local snapshot,
creates one immutable Feishu document per ISO week, maintains a GitHub CI index
linked from the Simpler index, and updates one marker-owned issue comment.
"""

import argparse
from concurrent.futures import as_completed, ThreadPoolExecutor
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import feishu_perf_report as feishu
from nettime import network_utc


TARGET_JOBS = {
    "st-sim-a2a3sim": "st-sim-a2a3",
    "st-sim-a5sim": "st-sim-a5",
    "st-onboard-a2a3": "st-onboard-a2a3",
    "st-onboard-a5": "st-onboard-a5",
}
PHASES = ("setup", "cache", "install_build", "kernel_build", "test", "dfx")
COMMENT_MARKER = "<!-- simpler-ci-weekly:v1 -->"
REPORT_TZ = ZoneInfo("Asia/Shanghai")


class ApiError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, token, api_url="https://api.github.com", retries=3):
        self.token = token
        self.api_url = api_url.rstrip("/")
        self.retries = retries

    def request(self, method, path, body=None):
        url = path if path.startswith("http") else f"{self.api_url}{path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "pto-simpler-perf-tracker",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(body).encode() if body is not None else None
        last = None
        for attempt in range(self.retries):
            req = urllib.request.Request(url, data=data, headers=headers,
                                         method=method)
            try:
                with urllib.request.urlopen(req, timeout=60) as response:
                    raw = response.read()
                    return json.loads(raw.decode()) if raw else {}
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:500]
                if exc.code not in (429, 500, 502, 503, 504):
                    raise ApiError(f"GitHub API HTTP {exc.code}: {detail}") from exc
                last = f"HTTP {exc.code}: {detail}"
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = str(exc)
            if attempt + 1 < self.retries:
                time.sleep(attempt + 1)
        raise ApiError(f"GitHub API failed after {self.retries} attempts: {last}")

    def paginate(self, path, item_key=None):
        items, page = [], 1
        separator = "&" if "?" in path else "?"
        while True:
            payload = self.request("GET", f"{path}{separator}per_page=100&page={page}")
            batch = payload.get(item_key, []) if item_key else payload
            items.extend(batch)
            if len(batch) < 100:
                return items
            page += 1


def parse_timestamp(value):
    if not value:
        return None
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def duration_seconds(start, end):
    a, b = parse_timestamp(start), parse_timestamp(end)
    if a is None or b is None or b < a:
        return None
    return (b - a).total_seconds()


def percentile(values, fraction):
    """Nearest-rank percentile; returns None for an empty sample."""
    ordered = sorted(values)
    if not ordered:
        return None
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def week_bounds(label):
    match = re.fullmatch(r"(\d{4})-W(\d{2})", label)
    if not match:
        raise ValueError(f"invalid ISO week {label!r}; expected YYYY-Www")
    local_start = dt.datetime.combine(
        dt.date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1),
        dt.time(), tzinfo=REPORT_TZ)
    start = local_start.astimezone(dt.timezone.utc)
    return start, start + dt.timedelta(days=7)


def previous_week(now):
    local = now.astimezone(REPORT_TZ)
    monday = local.date() - dt.timedelta(days=local.weekday())
    target = monday - dt.timedelta(days=7)
    year, week, _ = target.isocalendar()
    return f"{year}-W{week:02d}"


def canonical_job(name):
    leaf = name.rsplit("/", 1)[-1].strip()
    for rendered, canonical in TARGET_JOBS.items():
        if leaf == rendered:
            return canonical
    return None


def classify_phase(step_name):
    name = step_name.lower()
    if name.startswith("post ") and "cache" not in name:
        return None
    if "cache" in name:
        return "cache"
    if "set up environment" in name:
        return "install_build"
    if "compile scene-test kernels" in name:
        return "kernel_build"
    if "pytest" in name or "scene tests" in name or "sdma" in name:
        return "test"
    if any(word in name for word in (
            "dfx", "dep_gen", "swimlane", "pmu", "args_dump", "scope_stats")):
        return "dfx"
    if any(word in name for word in (
            "set up job", "checkout", "manual mode", "compiler", "toolchain",
            "set up python", "graphviz")):
        return "setup"
    return None


def runner_dimensions(job, tier_map, anon_salt):
    labels = job.get("labels") or []
    if "self-hosted" not in labels:
        os_name = next((x.removesuffix("-latest") for x in labels
                        if x.startswith(("ubuntu", "macos", "windows"))),
                       "github")
        return os_name, "github-hosted", "github-standard"
    runner_id = str(job.get("runner_id") or "")
    runner_name = job.get("runner_name") or ""
    tier = tier_map.get(runner_id) or tier_map.get(runner_name)
    if not tier:
        digest = hashlib.sha256(
            f"{anon_salt}:{runner_id or runner_name}".encode()).hexdigest()[:8]
        tier = f"unclassified-{digest}"
    return "linux", "self-hosted", tier


def normalize_job(job, run, tier_map, anon_salt):
    canonical = canonical_job(job.get("name", ""))
    if canonical is None or job.get("conclusion") == "skipped":
        return None
    os_name, path, tier = runner_dimensions(job, tier_map, anon_salt)
    phases = {phase: 0.0 for phase in PHASES}
    observed = set()
    for step in job.get("steps") or []:
        phase = classify_phase(step.get("name", ""))
        seconds = duration_seconds(step.get("started_at"), step.get("completed_at"))
        if phase and seconds is not None and step.get("conclusion") != "skipped":
            phases[phase] += seconds
            observed.add(phase)
    phases = {phase: phases[phase] for phase in PHASES if phase in observed}
    return {
        "run_id": run["id"],
        "job_id": job["id"],
        "url": job.get("html_url") or run.get("html_url"),
        "job": canonical,
        "os": os_name,
        "path": path,
        "runner_tier": tier,
        "conclusion": job.get("conclusion"),
        "wall_seconds": duration_seconds(job.get("started_at"),
                                          job.get("completed_at")),
        "phases": phases,
    }


def _day_ranges(start, end):
    cursor = start
    while cursor < end:
        nxt = min(cursor + dt.timedelta(days=1), end)
        yield cursor, nxt
        cursor = nxt


def _iso_z(value):
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect_records(client, repo, workflow, start, end, tier_map, anon_salt,
                    workers=8):
    """Collect in daily slices to stay below GitHub's 1,000-result search cap."""
    workflow_id = urllib.parse.quote(workflow, safe="")
    runs = {}
    for day_start, day_end in _day_ranges(start, end):
        inclusive_end = day_end - dt.timedelta(seconds=1)
        query = urllib.parse.urlencode({
            "event": "pull_request",
            "status": "completed",
            "created": f"{_iso_z(day_start)}..{_iso_z(inclusive_end)}",
        })
        path = f"/repos/{repo}/actions/workflows/{workflow_id}/runs?{query}"
        for run in client.paginate(path, "workflow_runs"):
            runs[run["id"]] = run

    def collect_run(run):
        jobs = client.paginate(
            f"/repos/{repo}/actions/runs/{run['id']}/jobs?filter=latest", "jobs")
        found = []
        for job in jobs:
            record = normalize_job(job, run, tier_map, anon_salt)
            if record is not None:
                found.append(record)
        return found

    records = []
    ordered_runs = sorted(runs.values(), key=lambda r: r["id"])
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(collect_run, run) for run in ordered_runs]
        for index, future in enumerate(as_completed(futures), 1):
            records.extend(future.result())
            if index % 50 == 0:
                print(f"[ci-weekly] collected jobs from {index}/{len(runs)} runs",
                      flush=True)
    return sorted(records, key=lambda record: (record["run_id"], record["job_id"]))


def group_key(record):
    return (record["job"], record["os"], record["path"], record["runner_tier"])


def aggregate_records(records):
    groups = {}
    for record in records:
        bucket = groups.setdefault(group_key(record), {"all": [], "success": []})
        bucket["all"].append(record)
        if record["conclusion"] == "success" and record["wall_seconds"] is not None:
            bucket["success"].append(record)

    result = []
    for key in sorted(groups):
        all_records = groups[key]["all"]
        successful = groups[key]["success"]
        wall = [r["wall_seconds"] for r in successful]
        phases = {}
        for phase in PHASES:
            values = [r["phases"][phase] for r in successful
                      if phase in r["phases"]]
            if values:
                phases[phase] = {
                    "n": len(values),
                    "p50": percentile(values, 0.50),
                    "p90": percentile(values, 0.90),
                }
        conclusions = {}
        for record in all_records:
            value = record["conclusion"] or "unknown"
            conclusions[value] = conclusions.get(value, 0) + 1
        result.append({
            "job": key[0], "os": key[1], "path": key[2], "runner_tier": key[3],
            "n": len(wall), "total": len(all_records), "conclusions": conclusions,
            "wall": {"p50": percentile(wall, 0.50),
                     "p90": percentile(wall, 0.90)},
            "phases": phases,
            "slowest": [
                {"seconds": r["wall_seconds"], "url": r["url"]}
                for r in sorted(successful, key=lambda x: x["wall_seconds"],
                                reverse=True)[:3]
            ],
        })
    return result


def _fmt_seconds(value):
    if value is None:
        return "—"
    minutes, seconds = divmod(round(value), 60)
    return f"{minutes}m{seconds:02d}s" if minutes else f"{seconds}s"


def _fmt_delta(current, baseline):
    if current is None or baseline in (None, 0):
        return "—"
    return f"{(current - baseline) / baseline * 100:+.1f}%"


def aggregate_map(aggregates):
    return {(a["job"], a["os"], a["path"], a["runner_tier"]): a
            for a in aggregates}


def issue_markdown(label, generated_at, aggregates, baseline, feishu_url):
    baseline_by_key = aggregate_map(baseline)
    lines = [
        COMMENT_MARKER,
        "## GitHub CI 每周性能",
        "",
        f"最后更新：{generated_at} · 统计周期：`{label}`（北京时间周一至周日）",
        "",
        f"[飞书完整历史与周报]({feishu_url})",
        "",
        "| Job / OS / runner tier | n | p50 | p90 | p50 vs 前4周 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for item in aggregates:
        if not item["n"]:
            continue
        key = (item["job"], item["os"], item["path"], item["runner_tier"])
        old = baseline_by_key.get(key, {}).get("wall", {}).get("p50")
        name = f"{item['job']} / {item['os']} / {item['runner_tier']}"
        lines.append(
            f"| {name} | {item['n']} | {_fmt_seconds(item['wall']['p50'])} | "
            f"{_fmt_seconds(item['wall']['p90'])} | "
            f"{_fmt_delta(item['wall']['p50'], old)} |")
    lines += [
        "",
        "> p50/p90 仅使用成功 job；失败、取消和样本量记录在飞书周报中。"
        " `Set up environment` 当前按 install+build 合并统计。",
    ]
    return "\n".join(lines)


def weekly_markdown(label, generated_at, aggregates):
    lines = [
        f"# GitHub CI 性能周报 {label}",
        "",
        f"生成时间：{generated_at}；统计周期为北京时间周一 00:00 至周日 24:00。",
        "",
        "## Job wall time",
        "",
        "```text",
        "Job | OS | runner tier | success/total | p50 | p90",
    ]
    for item in aggregates:
        lines.append(
            f"{item['job']} | {item['os']} | {item['runner_tier']} | "
            f"{item['n']}/{item['total']} | {_fmt_seconds(item['wall']['p50'])} | "
            f"{_fmt_seconds(item['wall']['p90'])}")
    lines += ["```", "", "## Phase timing", "", "```text",
              "Job / OS / tier | phase | n | p50 | p90"]
    for item in aggregates:
        name = f"{item['job']} / {item['os']} / {item['runner_tier']}"
        for phase in PHASES:
            stats = item["phases"].get(phase)
            if stats:
                lines.append(
                    f"{name} | {phase} | {stats['n']} | "
                    f"{_fmt_seconds(stats['p50'])} | {_fmt_seconds(stats['p90'])}")
    lines += ["```", "", "## Slowest successful runs", ""]
    for item in aggregates:
        name = f"{item['job']} / {item['os']} / {item['runner_tier']}"
        links = ", ".join(
            f"{_fmt_seconds(run['seconds'])}: {run['url']}"
            for run in item["slowest"])
        lines.append(f"- {name}: {links or '—'}")
    lines += ["", "## Notes", "",
              "- p50/p90 use the nearest-rank method and successful jobs only.",
              "- Self-hosted runner names and IDs are never persisted or published.",
              "- `install_build` is the current combined `Set up environment` step."]
    return "\n".join(lines) + "\n"


def _state_dir():
    script_dir = Path(__file__).resolve().parent
    if os.environ.get("PTO_TOOL_ROOT"):
        return Path(os.environ["PTO_TOOL_ROOT"]) / "state"
    if script_dir.name == "app":
        return script_dir.parent / "state"
    return script_dir / "runtime" / "state"


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent,
                                text=True)
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


def load_baseline_records(output_dir, label, count=4):
    current_start, _ = week_bounds(label)
    records, found = [], 0
    cursor = current_start - dt.timedelta(days=7)
    while found < count:
        year, week, _ = cursor.date().isocalendar()
        path = output_dir / f"{year}-W{week:02d}.json"
        if path.exists():
            records.extend(load_json(path, {}).get("records", []))
            found += 1
        cursor -= dt.timedelta(days=7)
        if (current_start - cursor).days > 365:
            break
    return records


def _block_text(block):
    for key in ("text", "heading1", "heading2", "heading3"):
        value = block.get(key)
        if value:
            return "".join(
                element.get("text_run", {}).get("content", "")
                for element in value.get("elements", []))
    return ""


def doc_root_blocks(token, doc_id):
    items, page = [], None
    while True:
        url = (f"{feishu.BASE}/docx/v1/documents/{doc_id}/blocks/{doc_id}/children"
               "?page_size=500" + (f"&page_token={page}" if page else ""))
        data = feishu._api("GET", url, token=token)["data"]
        items.extend(data.get("items", []))
        if not data.get("has_more"):
            return items
        page = data.get("page_token")


def ensure_total_index_link(token, total_index_doc, ci_index_url):
    if any(ci_index_url in _block_text(block)
           for block in doc_root_blocks(token, total_index_doc)):
        return
    feishu.docx_append(token, total_index_doc, [
        feishu._text_block(f"📈 GitHub CI 性能索引 → {ci_index_url}")])


def sync_ci_index(token, index_doc, weeks, domain):
    feishu.clear_doc(token, index_doc)
    blocks = [feishu._heading_block(2, "GitHub CI 每周性能")]
    blocks.extend(
        feishu._text_block(
            f"📅 {label} → https://{domain}/docx/{weeks[label]['doc_id']}")
        for label in sorted(weeks, reverse=True)
        if weeks[label].get("complete"))
    feishu.docx_append(token, index_doc, blocks)


def publish_feishu(report_md, label, state, state_path, total_index_doc,
                   token, domain, force=False):
    if not state.get("ci_index_doc"):
        state["ci_index_doc"] = feishu.docx_create(token, "Simpler GitHub CI 性能 · 索引")
        state["ci_index_managed"] = True
        save_json(state_path, state)
        try:
            feishu.set_doc_link_editable(token, state["ci_index_doc"], "tenant")
        except SystemExit:
            pass
    elif not state.get("ci_index_managed"):
        raise SystemExit("refusing to rewrite an unowned Feishu CI index")
    week = state.setdefault("weeks", {}).setdefault(label, {})
    if not week.get("doc_id"):
        week["doc_id"] = feishu.docx_create(token, f"Simpler GitHub CI 性能 {label}")
        week["managed"] = True
        week["complete"] = False
        save_json(state_path, state)
        try:
            feishu.set_doc_link_editable(token, week["doc_id"], "tenant")
        except SystemExit:
            pass
    elif not week.get("managed"):
        raise SystemExit(f"refusing to rewrite unowned Feishu document for {label}")
    if force or not week.get("complete"):
        feishu.clear_doc(token, week["doc_id"])
        feishu.docx_append(token, week["doc_id"], feishu.md_to_blocks(report_md))
        week["complete"] = True
        save_json(state_path, state)
    sync_ci_index(token, state["ci_index_doc"], state["weeks"], domain)
    index_url = f"https://{domain}/docx/{state['ci_index_doc']}"
    ensure_total_index_link(token, total_index_doc, index_url)
    return index_url, f"https://{domain}/docx/{week['doc_id']}"


def upsert_issue_comment(client, repo, issue_number, body, state, state_path):
    comment_id = state.get("issue_comment_id")
    if not comment_id:
        comments = client.paginate(f"/repos/{repo}/issues/{issue_number}/comments")
        existing = next((comment for comment in comments
                         if COMMENT_MARKER in (comment.get("body") or "")), None)
        comment_id = existing and existing["id"]
    if comment_id:
        client.request("PATCH", f"/repos/{repo}/issues/comments/{comment_id}",
                       {"body": body})
    else:
        created = client.request(
            "POST", f"/repos/{repo}/issues/{issue_number}/comments", {"body": body})
        comment_id = created["id"]
    state["issue_comment_id"] = comment_id
    save_json(state_path, state)
    return comment_id


def resolve_total_index(token):
    doc = os.environ.get("FEISHU_SIMPLER_INDEX_DOCX_TOKEN")
    wiki = os.environ.get("FEISHU_SIMPLER_INDEX_WIKI_TOKEN")
    if doc:
        return doc
    if wiki:
        return feishu.wiki_node_to_docx(token, wiki)
    raise SystemExit("Set FEISHU_SIMPLER_INDEX_DOCX_TOKEN or "
                     "FEISHU_SIMPLER_INDEX_WIKI_TOKEN for the Simpler index.")


def resolve_github_token():
    """Reuse the caller's token or the host's existing GitHub CLI login."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return token
    try:
        result = subprocess.run(
            ["gh", "auth", "token", "--hostname", "github.com"],
            capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        result = None
    if result is not None and result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    raise SystemExit("No GitHub authentication available. Log in with `gh auth login` "
                     "or provide GITHUB_TOKEN/GH_TOKEN.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="hw-native-sys/simpler")
    parser.add_argument("--workflow", default="ci.yml")
    parser.add_argument("--issue", type=int, default=1772)
    parser.add_argument("--week", help="Beijing ISO week YYYY-Www; default previous week")
    parser.add_argument("--state", type=Path,
                        default=_state_dir() / "ci-weekly-state.json")
    parser.add_argument("--output-dir", type=Path,
                        default=_state_dir() / "ci-weekly")
    parser.add_argument("--workers", type=int, default=8,
                        help="concurrent GitHub job requests (default: 8)")
    parser.add_argument("--publish", action="store_true",
                        help="publish Feishu docs and update the fixed issue comment")
    parser.add_argument("--force", action="store_true",
                        help="rebuild an already-published Feishu weekly document")
    args = parser.parse_args()
    if args.workers < 1 or args.workers > 16:
        parser.error("--workers must be between 1 and 16")

    generated = network_utc()
    if generated is None:
        raise SystemExit("network time unavailable; refusing to generate a report")
    label = args.week or previous_week(generated)
    start, end = week_bounds(label)
    generated_text = generated.astimezone(REPORT_TZ).strftime(
        "%Y-%m-%d %H:%M Asia/Shanghai")

    state = load_json(args.state, {"ci_index_doc": None, "weeks": {}})
    published_week = state.get("weeks", {}).get(label, {})
    if (args.publish and not args.force and published_week.get("complete")
            and published_week.get("issue_updated")):
        print(f"[ci-weekly] skip: {label} already published")
        return

    github_token = resolve_github_token()
    try:
        tier_map = json.loads(os.environ.get("CI_RUNNER_TIERS_JSON", "{}"))
    except ValueError as exc:
        raise SystemExit(f"CI_RUNNER_TIERS_JSON is not valid JSON: {exc}") from exc
    if not isinstance(tier_map, dict):
        raise SystemExit("CI_RUNNER_TIERS_JSON must be a JSON object.")

    client = GitHubClient(github_token, os.environ.get(
        "GITHUB_API_URL", "https://api.github.com"))
    records = collect_records(
        client, args.repo, args.workflow, start, end, tier_map,
        os.environ.get("CI_RUNNER_ANON_SALT", args.repo), args.workers)
    aggregates = aggregate_records(records)
    baseline = aggregate_records(load_baseline_records(args.output_dir, label))
    report_md = weekly_markdown(label, generated_text, aggregates)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "schema_version": 1, "week": label, "generated_at": generated_text,
        "window_start": _iso_z(start), "window_end": _iso_z(end),
        "repository": args.repo, "workflow": args.workflow,
        "records": records, "aggregates": aggregates,
    }
    save_json(args.output_dir / f"{label}.json", snapshot)
    (args.output_dir / f"{label}.md").write_text(report_md)
    print(f"[ci-weekly] {label}: {len(records)} target job(s), "
          f"{len(aggregates)} bucket(s)")

    if not args.publish:
        print(report_md)
        return

    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    if not (app_id and app_secret):
        raise SystemExit("Set FEISHU_APP_ID and FEISHU_APP_SECRET.")
    token = feishu.tenant_token(app_id, app_secret)
    total_index_doc = resolve_total_index(token)
    domain = os.environ.get("FEISHU_DOMAIN", "hw-native-sys.feishu.cn")
    index_url, week_url = publish_feishu(
        report_md, label, state, args.state, total_index_doc, token, domain,
        force=args.force)
    body = issue_markdown(label, generated_text, aggregates, baseline, index_url)
    upsert_issue_comment(client, args.repo, args.issue, body, state, args.state)
    state["weeks"][label]["issue_updated"] = True
    save_json(args.state, state)
    print(f"[ci-weekly] Feishu week: {week_url}")
    print(f"[ci-weekly] Feishu index: {index_url}")
    print(f"[ci-weekly] GitHub issue #{args.issue} updated")


if __name__ == "__main__":
    main()
