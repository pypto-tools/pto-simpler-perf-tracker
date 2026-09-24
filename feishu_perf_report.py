#!/usr/bin/env python3
# Copyright (c) PyPTO Contributors.
# Push a perf_history.json (produced by tools/perf_history.py) to a Feishu
# document via the Open Platform API. Append-only, so repeated runs build a
# history.
#
# Two targets:
#   --target docx  (default)  Append "heading + code block" per commit to a
#                             Feishu doc. Accepts a wiki node OR a raw docx id.
#   --target sheet            Append one row per commit to a spreadsheet.
#
# Credentials / target are read from the environment -- never hardcode them:
#   FEISHU_APP_ID       app id of a self-built Feishu app
#   FEISHU_APP_SECRET   app secret
#   FEISHU_WIKI_TOKEN   wiki node token, the id in .../wiki/<TOKEN>
#                       (resolved to its underlying docx). docx target.
#   FEISHU_DOCX_TOKEN   raw docx document_id (.../docx/<TOKEN>). docx target.
#   FEISHU_DOC_TOKEN    spreadsheet token (.../sheets/<TOKEN>). sheet target.
#   FEISHU_SHEET_ID     (optional) sheet/tab id; defaults to the first sheet.
#
# App scopes required:
#   docx target:  wiki:wiki(readonly) + docx:document   (and the app must be
#                 added as a collaborator on the wiki space / doc with edit)
#   sheet target: sheets:spreadsheet
#
# Usage:
#   FEISHU_WIKI_TOKEN=... python tools/feishu_perf_report.py --input tmp/perf_history.json
#   python tools/feishu_perf_report.py --input tmp/perf_history.json --dry-run

import argparse
import datetime
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import urllib.request
import urllib.error

# 报告时间戳取自网络时间（本服务器时钟被改过，不可信）；失败回退本机时间。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from nettime import network_now
except Exception:  # 兜底：缺失时退回本机时间，不阻断报告
    def network_now(fmt="%Y-%m-%d %H:%M:%S", timeout=8):  # type: ignore[misc]
        return datetime.datetime.now().strftime(fmt), bool(False)

BASE = "https://open.feishu.cn/open-apis"

# Feishu is reachable directly from the benchmark host.  urllib's default
# opener silently inherits HTTP(S)_PROXY from the login environment, which can
# route open.feishu.cn through a local GitHub proxy and make CONNECT fail with
# 403.  Keep Feishu traffic isolated from those unrelated proxy settings.  The
# shared _api helper is also used by notify_feishu.py, so report publication and
# failure notifications follow the same direct-connect policy.
_FEISHU_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# One row per commit; the benchmark's summary block is stored verbatim in the
# last cell (format-agnostic -- whatever columns the run printed).
HEADER = ["timestamp", "ref", "runtime", "platform", "rounds",
          "commit", "subject", "benchmark_summary"]


def _api(method, url, token=None, body=None, retries=3):
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    last = None
    for _ in range(retries):
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with _FEISHU_OPENER.open(req, timeout=60) as r:
                payload = json.loads(r.read().decode())
            if payload.get("code", 0) != 0:
                sys.exit(f"Feishu API error {payload.get('code')}: {payload.get('msg')}")
            return payload
        except urllib.error.HTTPError as e:
            sys.exit(f"Feishu API HTTP {e.code}: {e.read().decode()[:500]}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e  # transient (timeout / connection); retry
    sys.exit(f"Feishu API network error after {retries} tries: {last}")


def tenant_token(app_id, app_secret):
    p = _api("POST", f"{BASE}/auth/v3/tenant_access_token/internal",
             body={"app_id": app_id, "app_secret": app_secret})
    return p["tenant_access_token"]


def first_sheet_id(token, ss_token):
    p = _api("GET", f"{BASE}/sheets/v3/spreadsheets/{ss_token}/sheets/query",
             token=token)
    sheets = p["data"]["sheets"]
    return sheets[0]["sheet_id"]


def read_first_cell(token, ss_token, sheet_id):
    """Return the value of A1 to decide whether the header row exists."""
    rng = f"{sheet_id}!A1:A1"
    p = _api("GET", f"{BASE}/sheets/v2/spreadsheets/{ss_token}/values/{rng}",
             token=token)
    vals = p["data"]["valueRange"]["values"]
    return vals[0][0] if vals and vals[0] else None


def append_rows(token, ss_token, sheet_id, rows):
    body = {"valueRange": {"range": f"{sheet_id}!A1", "values": rows}}
    url = (f"{BASE}/sheets/v2/spreadsheets/{ss_token}/values_append"
           "?insertDataOption=INSERT_ROWS")
    return _api("POST", url, token=token, body=body)


def report_to_rows(report, timestamp):
    common = [report.get("ref"), report.get("runtime"),
              report.get("platform"), report.get("rounds")]
    rows = []
    for c in report["commits"]:
        summary = c.get("summary") or f"(no summary, rc={c.get('rc')})"
        rows.append([timestamp, *common, c["sha"][:12], c["subject"], summary])
    return rows


# --- docx / wiki target ----------------------------------------------------

# docx block_type ints: 4=heading2, 5=heading3, 14=code.
def _elements(text):
    return [{"text_run": {"content": text}}]


def _heading_block(level, text):
    return {"block_type": level + 2,
            f"heading{level}": {"elements": _elements(text)}}


def _code_block(text):
    # language 1 = PlainText.  Keep wide reports on one line so Feishu can
    # scroll them horizontally instead of squeezing the document content.
    return {"block_type": 14,
            "code": {"elements": _elements(text),
                     "style": {"language": 1, "wrap": False}}}


def _text_block(text):
    return {"block_type": 2, "text": {"elements": _elements(text)}}


def md_to_blocks(md):
    """Convert a markdown doc to docx blocks: #/##/### -> headings,
    ```fenced``` -> code blocks, other non-blank lines -> text."""
    blocks, lines, i = [], md.splitlines(), 0
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("```"):
            buf, j = [], i + 1
            while j < len(lines) and not lines[j].lstrip().startswith("```"):
                buf.append(lines[j])
                j += 1
            blocks.append(_code_block("\n".join(buf)))
            i = j + 1
            continue
        m = re.match(r"(#{1,3})\s+(.*)", line)
        if m:
            blocks.append(_heading_block(len(m.group(1)), m.group(2).strip()))
        elif line.strip():
            blocks.append(_text_block(line.strip()))
        i += 1
    return blocks


def report_to_blocks(report, timestamp):
    meta = (f"Perf history — {report.get('ref')} / {report.get('runtime')} / "
            f"{report.get('platform')} / rounds={report.get('rounds')} / "
            f"pin={(report.get('pto_isa_commit') or '')[:10]} / {timestamp}")
    blocks = [_heading_block(2, meta)]
    for c in report["commits"]:
        blocks.append(_heading_block(3, f"{c['sha'][:10]} — {c['subject']}"))
        summary = c.get("summary") or f"(no summary, rc={c.get('rc')})"
        blocks.append(_code_block(summary))
    return blocks


def wiki_node_to_docx(token, node_token):
    p = _api("GET", f"{BASE}/wiki/v2/spaces/get_node?token={node_token}",
             token=token)
    node = p["data"]["node"]
    if node.get("obj_type") != "docx":
        sys.exit(f"wiki node obj_type={node.get('obj_type')}, expected docx")
    return node["obj_token"]


def docx_children_count(token, doc_id):
    """Count direct children of the document root (append index)."""
    count, page = 0, None
    while True:
        url = (f"{BASE}/docx/v1/documents/{doc_id}/blocks/{doc_id}/children"
               "?page_size=500" + (f"&page_token={page}" if page else ""))
        d = _api("GET", url, token=token)["data"]
        count += len(d.get("items", []))
        page = d.get("page_token")
        if not d.get("has_more"):
            return count


def docx_root_sha_prefixes(token, doc_id):
    """Return commit prefixes already present in root-level metadata blocks."""
    prefixes, page = set(), None
    while True:
        url = (f"{BASE}/docx/v1/documents/{doc_id}/blocks/{doc_id}/children"
               "?page_size=500" + (f"&page_token={page}" if page else ""))
        data = _api("GET", url, token=token)["data"]
        for item in data.get("items", []):
            for element in (item.get("text") or {}).get("elements", []):
                content = (element.get("text_run") or {}).get("content", "")
                match = re.match(r"^([0-9a-f]{10})\s+·", content)
                if match:
                    prefixes.add(match.group(1))
        page = data.get("page_token")
        if not data.get("has_more"):
            return prefixes


def docx_append(token, doc_id, blocks):
    idx = docx_children_count(token, doc_id)
    url = f"{BASE}/docx/v1/documents/{doc_id}/blocks/{doc_id}/children"
    return _api("POST", url, token=token,
                body={"children": blocks, "index": idx})


def clear_doc(token, doc_id):
    """Delete ALL root-level children of a docx (rebuild prep).

    Deletes the tail in windows so index math stays simple and we never exceed
    the batch_delete range cap. Nested blocks (table cells) go with their
    parent table, so deleting root children empties the whole document.
    """
    url = (f"{BASE}/docx/v1/documents/{doc_id}/blocks/{doc_id}"
           "/children/batch_delete")
    while True:
        n = docx_children_count(token, doc_id)
        if n == 0:
            return
        k = min(n, 50)
        _api("DELETE", url, token=token,
            body={"start_index": n - k, "end_index": n})


def delete_file(token, file_token, file_type="docx"):
    """Move a cloud document to the Feishu recycle bin."""
    return _api("DELETE", f"{BASE}/drive/v1/files/{file_token}?type={file_type}",
                token=token)


def sync_month_index(token, index_doc, months,
                     domain="hw-native-sys.feishu.cn", month_parts=None):
    """Rewrite the small month index in newest-first order.

    Month documents can be created later than their neighbours when an old
    benchmark is retried.  Ordering links by creation time therefore drifts;
    derive it from the month keys every time instead.
    """
    clear_doc(token, index_doc)
    links = []
    month_parts = month_parts or {}
    for month in sorted(months, reverse=True):
        parts = month_parts.get(month) or [months[month]]
        if len(parts) == 1:
            links.append(_text_block(
                f"📅 {month} → https://{domain}/docx/{parts[0]}"))
            continue
        for part_no in range(len(parts), 0, -1):
            links.append(_text_block(
                f"📅 {month} · 卷 {part_no} → "
                f"https://{domain}/docx/{parts[part_no - 1]}"))
    if links:
        docx_append(token, index_doc, links)


# --- native Feishu tables (from processed jsonl) ---------------------------

METRICS = ["Host", "Device", "Total", "Sched", "Orch"]
# Annotate the inline change on every metric column.
DELTA_COLS = set(METRICS)
DELTA_FLAG_PCT = 5.0
# Commit-level alert when a device-side metric regresses (slows) by this much.
# Host is excluded — host wall is noisy and not the perf signal.
ALERT_PCT = 10.0
ALERT_COLS = {"Device", "Total", "Sched", "Orch"}
# Per-metric baseline floor, as a fraction of that metric's median baseline in
# the commit. Auto-scales across metrics of different magnitude and drops the
# tiny-baseline noise (e.g. ~6µs Orch vs ~600µs others) without hardcoding µs.
ALERT_MIN_FRAC = 0.2

# Feishu rejects a document at roughly 40,000 total blocks.  A single commit
# report contains many nested table-cell blocks, so a busy month can hit that
# limit even when the number of root blocks still looks modest.  Rotate early
# enough to leave room for a complete commit and for small manual additions.
DOC_BLOCK_SOFT_LIMIT = 35_000


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return 0
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def commit_alerts(metrics, prev):
    """Sorted [(pct, example, metric)] for meaningful regressions >= ALERT_PCT."""
    metrics, prev = metrics or {}, prev or {}
    floors = {}
    for c in ALERT_COLS:
        base = [b for ex in metrics if (b := prev.get(ex, {}).get(c))]
        floors[c] = ALERT_MIN_FRAC * _median(base) if base else 0
    al = []
    for ex, m in metrics.items():
        pm = prev.get(ex, {})
        for c in ALERT_COLS:
            cur, p = m.get(c), pm.get(c)
            if cur is not None and p and p >= floors[c]:
                pct = (cur - p) / p * 100
                if pct >= ALERT_PCT:
                    al.append((pct, ex, c))
    al.sort(reverse=True)
    return al


def alert_line(al):
    top = "; ".join(f"{ex} {c} +{pct:.0f}%" for pct, ex, c in al[:5])
    return f"⚠️ 重点回归: {top}" + (" …" if len(al) > 5 else "")


def _fmt(v):
    return "" if v is None else f"{v:.1f}"


def _cellv(cur, prev):
    """'2704.0 (-3.0%)' — value with inline change vs previous commit."""
    s = _fmt(cur)
    if cur is not None and prev not in (None, 0):
        pct = (cur - prev) / prev * 100
        mark = " 🔺" if pct >= DELTA_FLAG_PCT else (" 🔻" if pct <= -DELTA_FLAG_PCT else "")
        s += f" ({pct:+.1f}%{mark})"
    return s


def _commit_descendants(entry, prev, counter, prev_host=None):
    """Build (children_ids, descendants) for one commit: heading + meta + table."""
    def nid():
        counter[0] += 1
        return f"b{counter[0]}"

    alerts = commit_alerts(entry.get("metrics"), prev)
    desc, top = [], []
    h = nid()
    title = ("⚠️ " if alerts else "") + entry["subject"]
    desc.append({"block_id": h, "block_type": 5,
                 "heading3": {"elements": _elements(title)}})
    top.append(h)
    meta = " · ".join(filter(None, [entry["sha"][:10], entry.get("date", ""),
                                    f"NPU {entry.get('device')}" if entry.get("device") else ""]))
    mt = nid()
    desc.append({"block_id": mt, **_text_block(meta)})
    top.append(mt)
    if alerts:
        at = nid()
        desc.append({"block_id": at, **_text_block(alert_line(alerts))})
        top.append(at)

    metrics = entry.get("metrics") or {}
    if not metrics:
        cb = nid()
        if entry.get("device_status") == "disabled":
            block = _text_block("Device benchmark disabled; host-only run.")
        else:
            block = _code_block(entry.get("summary") or "(no data)")
        desc.append({"block_id": cb, **block})
        top.append(cb)
    else:
        cols = entry.get("present") or METRICS
        headers = ["Example"] + [f"{c}(us)" for c in cols]
        examples = sorted(metrics)
        cell_ids = []

        def add_cell(text):
            tb, cb = nid(), nid()
            desc.append({"block_id": tb, **_text_block(str(text))})
            desc.append({"block_id": cb, "block_type": 32, "table_cell": {},
                         "children": [tb]})
            cell_ids.append(cb)

        for hdr in headers:
            add_cell(hdr)
        for ex in examples:
            m = metrics[ex]
            pm = (prev or {}).get(ex, {})
            row = [ex] + [_cellv(m.get(c), pm.get(c)) if c in DELTA_COLS
                          else _fmt(m.get(c)) for c in cols]
            for value in row:
                add_cell(value)

        col_w = [360] + [150] * (len(headers) - 1)
        tbl = nid()
        desc.append({"block_id": tbl, "block_type": 31,
                     "table": {"property": {
                         "row_size": len(examples) + 1,
                         "column_size": len(headers),
                         "column_width": col_w,
                         "header_row": True}},
                     "children": cell_ids})
        top.append(tbl)

    previous_cases = (prev_host or {}).get("cases", {})
    for name, case in (entry.get("host") or {}).get("cases", {}).items():
        label = nid()
        if case.get("status") != "ok":
            reason = case.get("error") or f"rc={case.get('rc')}"
            text = f"Host bind · {name} · {case.get('status')}: {reason}"
            desc.append({"block_id": label, **_text_block(text)})
            top.append(label)
            continue
        text = (f"Host bind · {name} · {case.get('binds')} binds / "
                f"{case.get('warm_binds')} warm · NPU {case.get('device', '?')}")
        desc.append({"block_id": label, **_text_block(text)})
        top.append(label)

        previous = previous_cases.get(name) or {}
        if previous.get("device") != case.get("device"):
            previous = {}
        previous_metrics = previous.get("metrics") or {}
        host_rows = [["Phase", "Min(us, Δ)", "Median(us)", "Max(us)"]]
        for phase in ("control_plane", "host_orch", "graph_upload",
                      "arena_h2d"):
            metric = (case.get("metrics") or {}).get(phase)
            if not metric:
                continue
            prior = previous_metrics.get(phase) or {}
            host_rows.append([
                phase,
                _cellv(metric.get("min_us"), prior.get("min_us")),
                _fmt(metric.get("median_us")),
                _fmt(metric.get("max_us")),
            ])
        host_cells = []
        for row in host_rows:
            for value in row:
                tb, cb = nid(), nid()
                desc.append({"block_id": tb, **_text_block(str(value))})
                desc.append({"block_id": cb, "block_type": 32,
                             "table_cell": {}, "children": [tb]})
                host_cells.append(cb)
        host_table = nid()
        desc.append({
            "block_id": host_table,
            "block_type": 31,
            "table": {"property": {
                "row_size": len(host_rows), "column_size": 4,
                "column_width": [220, 180, 150, 150], "header_row": True,
            }},
            "children": host_cells,
        })
        top.append(host_table)
    return top, desc


def _load_state(p):
    try:
        with open(p) as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return {"index_doc": None, "months": {}, "pushed": []}


def _save_state(path, state):
    """Atomically persist publish state beside the destination file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _normalise_month_parts(state):
    """Upgrade legacy month->doc state without touching existing documents.

    Old documents have no trustworthy persisted total-block count.  Treat them
    as sealed and start the next successful publish in a new part.  This avoids
    an expensive full-document scan and never risks another write against a
    document that may already be at Feishu's hard limit.
    """
    changed = False
    parts = state.setdefault("month_parts", {})
    counts = state.setdefault("doc_blocks", {})
    sealed = state.setdefault("sealed_docs", [])
    sealed_set = set(sealed)
    for month, doc_id in state.setdefault("months", {}).items():
        if month not in parts:
            parts[month] = [doc_id]
            changed = True
        for part_doc in parts[month]:
            if part_doc not in counts:
                counts[part_doc] = DOC_BLOCK_SOFT_LIMIT
                changed = True
            if (part_doc not in sealed_set
                    and counts[part_doc] >= DOC_BLOCK_SOFT_LIMIT):
                sealed.append(part_doc)
                sealed_set.add(part_doc)
                changed = True
    return changed


def _entry_block_count(entry):
    prev = entry.get("_prev_metrics")
    prev_host = entry.get("_prev_host")
    _top, descendants = _commit_descendants(entry, prev, [0], prev_host)
    return len(descendants)


def _suffix_for_budget(entries, budget):
    """Return the largest oldest suffix that fits a forward-publish part."""
    chosen = []
    used, last_date = 0, object()
    for entry in reversed(entries):
        date = entry.get("date")
        added = _entry_block_count(entry) + (date != last_date)
        if used + added > budget:
            return chosen
        chosen.insert(0, entry)
        used += added
        last_date = date
    return chosen


def _prefix_for_budget(entries, budget):
    """Return the largest newest prefix that fits an append/backfill part."""
    chosen = []
    used, last_date = 0, object()
    for entry in entries:
        date = entry.get("date")
        added = _entry_block_count(entry) + (date != last_date)
        if used + added > budget:
            return chosen
        chosen.append(entry)
        used += added
        last_date = date
    return chosen


def unpublished_with_metrics(entries, pushed):
    """Return entries safe to publish and permanently deduplicate.

    A failed benchmark may still emit a partial summary and therefore a
    non-empty metrics mapping.  Publish only strict successes; the next daily
    overlap must be allowed to remeasure every partial or failed result.
    """
    def complete(e):
        host = e.get("host")
        host_ok = host is None or host.get("status") in {
            "ok", "unsupported", "disabled"
        }
        device_measured = bool(e.get("metrics"))
        device_ok = (e.get("device_status") == "disabled"
                     or (e.get("rc") == 0 and device_measured))
        host_measured = host is not None and host.get("status") == "ok"
        return device_ok and host_ok and (device_measured or host_measured)

    return [e for e in entries if e["sha"] not in pushed and complete(e)]


def publish_monthly(token, entries, state_path, domain="hw-native-sys.feishu.cn",
                    append=False, rebuild=False, no_delta=False,
                    title_prefix="Simpler 性能历史"):
    """Route commits into per-month docs + maintain an index doc.

    Bounds each doc to one month of tables so the web page stays responsive.
    `entries` must be globally ordered newest->oldest (perf_finalize output) —
    the full ordered set is always passed so each commit's "vs previous"
    delta is correct, but only commits NOT already in state["pushed"] are
    actually written, so re-running over a growing archive (the backfill) is
    idempotent and never duplicates.

    append=False (forward/daily): newest commits prepend at the top of their
    month doc. append=True (backfill of progressively OLDER commits): they
    append at the bottom so the newest-on-top ordering is preserved.

    rebuild=True: OVERWRITE — clear every existing month doc and reset the
    pushed-set, then write the whole `entries` set fresh, newest-on-top. Use it
    to rebuild the doc cleanly from a complete, ordered archive (no gaps, no
    scrambled order) without touching the index doc's URL.

    Returns the index doc URL.
    """
    # Precompute the true previous-commit metrics from the global ordering,
    # BEFORE filtering out already-pushed commits (so boundary deltas survive).
    # no_delta: a values-only doc (no Δ, no regression alerts) — pin every
    # baseline to None so each cell renders the raw measured number alone.
    for i, e in enumerate(entries):
        e["_prev_metrics"] = None if no_delta else (
            entries[i + 1].get("metrics") if i + 1 < len(entries) else None)
        e["_prev_host"] = None if no_delta else (
            entries[i + 1].get("host") if i + 1 < len(entries) else None)

    state_path = Path(state_path)
    state = _load_state(state_path)

    def save():
        _save_state(state_path, state)

    if _normalise_month_parts(state):
        save()

    if rebuild:
        # Overwrite: empty the existing month docs and forget what was pushed,
        # so the fresh ordered set fully replaces the old (gappy) content.
        retained = {}
        for m, docs in state.get("month_parts", {}).items():
            for did in docs:
                print(f"clearing month doc {m} ({did}) ...")
                clear_doc(token, did)
            if docs:
                # Reuse the original stable monthly URL.  Extra volume docs
                # become unreferenced rather than leaving empty links behind.
                retained[m] = [docs[0]]
        state["month_parts"] = retained
        state["doc_blocks"] = {
            docs[0]: 0 for docs in retained.values() if docs
        }
        state["sealed_docs"] = []
        state["reconciled_docs"] = []
        state["pushed"] = []
        save()

    if not rebuild:
        # A previous version checkpointed only after an entire month batch.
        # If the process failed mid-batch, some commits exist remotely but not
        # in state.  Reconcile each sealed legacy document once before writing
        # to its successor so recovery cannot duplicate those commits.
        reconciled = state.setdefault("reconciled_docs", [])
        reconciled_set = set(reconciled)
        sealed_set = set(state.get("sealed_docs", []))
        input_months = {(e.get("date") or "0000-00")[:7] for e in entries}
        for month in sorted(input_months):
            for doc_id in state.get("month_parts", {}).get(month, []):
                if doc_id not in sealed_set or doc_id in reconciled_set:
                    continue
                remote = docx_root_sha_prefixes(token, doc_id)
                pushed_list = state.setdefault("pushed", [])
                pushed_set = set(pushed_list)
                adopted = [e["sha"] for e in entries
                           if (e.get("date") or "0000-00")[:7] == month
                           and e["sha"] not in pushed_set
                           and e["sha"][:10] in remote]
                pushed_list.extend(adopted)
                reconciled.append(doc_id)
                reconciled_set.add(doc_id)
                save()
                if adopted:
                    print(f"reconciled {len(adopted)} already-published "
                          f"commit(s) from {month} legacy document")
    pushed = set(state.get("pushed", []))

    if not state.get("index_doc"):
        state["index_doc"] = docx_create(token, f"{title_prefix} · 索引")
        save()  # persist id before anything else can fail
        try:
            set_doc_link_editable(token, state["index_doc"], "anyone")
        except SystemExit:
            pass

    # Only push commits not already published; keep month buckets in the
    # global newest->oldest order so append/prepend land correctly.
    unpushed = [e for e in entries if e["sha"] not in pushed]
    fresh = unpublished_with_metrics(entries, pushed)
    skipped = len(unpushed) - len(fresh)
    if skipped:
        print(f"skipping {skipped} unmeasured commit(s); they remain retryable")
    if not fresh:
        sync_month_index(token, state["index_doc"], state.get("months", {}),
                         domain, state.get("month_parts"))
        print("nothing new to publish (all measured commits already pushed).")
        return f"https://{domain}/docx/{state['index_doc']}"
    months = {}
    for e in fresh:
        months.setdefault((e.get("date") or "0000-00")[:7], []).append(e)

    for month in sorted(months, reverse=True):
        if month not in state["months"]:
            did = docx_create(token, f"{title_prefix} {month}")
            state["months"][month] = did
            state["month_parts"][month] = [did]
            state["doc_blocks"][did] = 0
            save()  # persist before the (failable) link/table writes
            try:
                set_doc_link_editable(token, did, "anyone")
            except SystemExit:
                pass
            print(f"created month doc {month}: "
                  f"https://{domain}/docx/{did}")
        pending = list(months[month])
        forward = not (append and not rebuild)
        while pending:
            part_docs = state["month_parts"][month]
            doc_id = part_docs[-1] if forward else part_docs[0]
            used = state["doc_blocks"].get(doc_id, DOC_BLOCK_SOFT_LIMIT)
            sealed = doc_id in set(state.get("sealed_docs", []))
            budget = 0 if sealed else max(0, DOC_BLOCK_SOFT_LIMIT - used)
            choose = _suffix_for_budget if forward else _prefix_for_budget
            batch = choose(pending, budget) if budget else []

            if not batch:
                part_no = len(part_docs) + 1
                doc_id = docx_create(
                    token, f"{title_prefix} {month} · 卷 {part_no}")
                if forward:
                    part_docs.append(doc_id)
                else:
                    part_docs.insert(0, doc_id)
                state["doc_blocks"][doc_id] = 0
                save()
                try:
                    set_doc_link_editable(token, doc_id, "anyone")
                except SystemExit:
                    pass
                print(f"created month part {month} #{part_no}: "
                      f"https://{domain}/docx/{doc_id}")
                budget = DOC_BLOCK_SOFT_LIMIT
                batch = choose(pending, budget)

            def checkpoint(entry, block_count):
                state.setdefault("pushed", []).append(entry["sha"])
                state["doc_blocks"][doc_id] = (
                    state["doc_blocks"].get(doc_id, 0) + block_count)
                save()

            push_tables(token, doc_id, batch, prepend=forward,
                        on_pushed=checkpoint)
            if forward:
                pending = pending[:-len(batch)]
            else:
                pending = pending[len(batch):]

    sync_month_index(token, state["index_doc"], state.get("months", {}), domain,
                     state.get("month_parts"))
    try:
        from report_hub import sync_report_hub
        sync_report_hub(token, state_path.parents[1], domain)
    except ImportError:
        pass
    return f"https://{domain}/docx/{state['index_doc']}"


def docx_create(token, title, folder_token=None):
    """Create a new docx owned by the app; returns its document_id."""
    body = {"title": title}
    if folder_token:
        body["folder_token"] = folder_token
    p = _api("POST", f"{BASE}/docx/v1/documents", token=token, body=body)
    return p["data"]["document"]["document_id"]


def set_doc_link_editable(token, doc_token, scope="anyone"):
    """Open link-share (needs drive:drive).

    scope='anyone'  -> anyone with the link can read+edit (open to everyone)
    scope='tenant'  -> only org members with the link
    """
    entity = "anyone_editable" if scope == "anyone" else "tenant_editable"
    url = f"{BASE}/drive/v2/permissions/{doc_token}/public?type=docx"
    return _api("PATCH", url, token=token,
                body={"link_share_entity": entity,
                      "comment_entity": "anyone_can_view",
                      "share_entity": "anyone",
                      "manage_collaborator_entity": "collaborator_can_view"})


def docx_append_descendant(token, doc_id, children_id, descendants, index):
    url = f"{BASE}/docx/v1/documents/{doc_id}/blocks/{doc_id}/descendant"
    return _api("POST", url, token=token,
                body={"index": index, "children_id": children_id,
                      "descendants": descendants})


def _date_heading_block(date):
    return {"block_id": "dh", "block_type": 4,
            "heading2": {"elements": _elements(f"📅 {date or '未知日期'}")}}


def push_tables(token, doc_id, entries, dry_run=False, prepend=False,
                on_pushed=None):
    """Push date-grouped commit tables (native tables, newest first).

    prepend=True inserts at the very top (index 0, daily run lands above older
    content); otherwise appends at the end.
    """
    idx = 0 if (dry_run or prepend) else docx_children_count(token, doc_id)
    last_date = None
    for i, e in enumerate(entries):
        # Prefer a precomputed prev (set by publish from the global ordering)
        # so per-month grouping does not break the "vs previous commit" delta.
        prev = e["_prev_metrics"] if "_prev_metrics" in e else (
            entries[i + 1].get("metrics") if i + 1 < len(entries) else None)
        prev_host = e["_prev_host"] if "_prev_host" in e else (
            entries[i + 1].get("host") if i + 1 < len(entries) else None)
        add_date = e.get("date") != last_date
        if add_date:
            last_date = e.get("date")
        top, desc = _commit_descendants(e, prev, [0], prev_host)
        if dry_run:
            print(f"  [{e.get('date')}] {e['sha'][:10]} -> "
                  f"{len(top)} top, {len(desc)} descendants")
            continue
        children = list(top)
        descendants = list(desc)
        if add_date:
            children.insert(0, "dh")
            descendants.insert(0, _date_heading_block(last_date))
        # One API call per commit makes the remote write atomic at commit
        # granularity.  Checkpoint it locally immediately after the API returns.
        docx_append_descendant(token, doc_id, children, descendants, idx)
        idx += len(children)
        print(f"pushed {e['sha'][:10]} ({i + 1}/{len(entries)})")
        if on_pushed:
            on_pushed(e, len(descendants))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", default="tmp/perf_history.json")
    ap.add_argument("--md", help="push this markdown file verbatim (docx only) "
                                 "instead of building blocks from --input")
    ap.add_argument("--from-processed",
                    help="push native Feishu tables from a processed jsonl "
                         "(perf_finalize.py output); docx only")
    ap.add_argument("--create-doc", metavar="TITLE",
                    help="create a NEW app-owned docx with this title, write "
                         "tables into it, and open link-edit (needs drive:drive)")
    ap.add_argument("--limit", type=int,
                    help="only push the first N commits (for testing)")
    ap.add_argument("--publish", action="store_true",
                    help="route commits into per-month docs (prepended) + index "
                         "doc; bounds doc size. Needs --from-processed + --state")
    ap.add_argument("--state", default="feishu_state.json",
                    help="json mapping month->doc_id and index doc (for --publish)")
    ap.add_argument("--append", action="store_true",
                    help="backfill mode: append progressively-older commits at "
                         "the BOTTOM of each month doc (default prepends newest "
                         "at top). Pass the full archive each run; already-"
                         "pushed commits are skipped via state['pushed'].")
    ap.add_argument("--rebuild", action="store_true",
                    help="OVERWRITE mode: clear every existing month doc and "
                         "reset the pushed-set, then rewrite the whole archive "
                         "fresh, newest-on-top (clean, ordered, no gaps). Keeps "
                         "the index doc URL. Needs --from-processed + --state.")
    ap.add_argument("--no-delta", action="store_true",
                    help="values-only doc: render the raw measured numbers with "
                         "no vs-previous Δ and no regression alerts.")
    ap.add_argument("--title-prefix", default="Simpler 性能历史",
                    help="title prefix for the auto-created index/month docs "
                         "(use a distinct one to keep a second --state's docs "
                         "separate from the main report).")
    ap.add_argument("--target", choices=["docx", "sheet"], default="docx")
    ap.add_argument("--dry-run", action="store_true",
                    help="print what would be sent; no API calls / no creds")
    args = ap.parse_args()

    ts, ts_net = network_now("%Y-%m-%d %H:%M:%S")
    if not ts_net:
        sys.stderr.write("[nettime] 警告: 网络时间不可达，报告时间戳回退本机时钟(可能不准)\n")
    if args.md and args.target != "docx":
        sys.exit("--md is only supported with --target docx")

    if args.from_processed:
        with open(args.from_processed) as stream:
            entries = [json.loads(line) for line in stream if line.strip()]
        if args.limit:
            entries = entries[:args.limit]
        if args.dry_run:
            push_tables(None, None, entries, dry_run=True)
            return
        app_id = os.environ.get("FEISHU_APP_ID")
        app_secret = os.environ.get("FEISHU_APP_SECRET")
        wiki_token = os.environ.get("FEISHU_WIKI_TOKEN")
        docx_token = os.environ.get("FEISHU_DOCX_TOKEN")
        # --create-doc makes its own doc, so it needs no target token.
        if not (app_id and app_secret):
            sys.exit("Set FEISHU_APP_ID and FEISHU_APP_SECRET.")
        if not (args.create_doc or args.publish) and not (wiki_token or docx_token):
            sys.exit("Set FEISHU_WIKI_TOKEN or FEISHU_DOCX_TOKEN "
                     "(or use --create-doc / --publish).")
        token = tenant_token(app_id, app_secret)
        if args.publish:
            index_url = publish_monthly(token, entries, args.state,
                                        append=args.append, rebuild=args.rebuild,
                                        no_delta=args.no_delta,
                                        title_prefix=args.title_prefix)
            print(f"done: {len(entries)} commit(s) published. index -> {index_url}")
            return
        if args.create_doc:
            doc_id = docx_create(token, args.create_doc)
            print(f"created docx {doc_id}")
            push_tables(token, doc_id, entries)
            try:
                set_doc_link_editable(token, doc_id, scope="anyone")
                share = "link: anyone editable"
            except SystemExit as e:
                share = f"(link-share not set: {e})"
            print(f"done: {len(entries)} table(s) -> "
                  f"https://hw-native-sys.feishu.cn/docx/{doc_id}  [{share}]")
            return
        doc_id = docx_token or wiki_node_to_docx(token, wiki_token)
        push_tables(token, doc_id, entries)
        print(f"done: {len(entries)} commit table(s) -> docx {doc_id}")
        return

    if args.target == "sheet":
        report = json.loads(open(args.input).read())
        rows = report_to_rows(report, ts)
        if args.dry_run:
            print("HEADER:", HEADER)
            for r in rows:
                print(r)
            print(f"\n{len(rows)} row(s) would be appended.")
            return
        app_id = os.environ.get("FEISHU_APP_ID")
        app_secret = os.environ.get("FEISHU_APP_SECRET")
        ss_token = os.environ.get("FEISHU_DOC_TOKEN")
        if not (app_id and app_secret and ss_token):
            sys.exit("Set FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_DOC_TOKEN.")
        token = tenant_token(app_id, app_secret)
        sheet_id = (os.environ.get("FEISHU_SHEET_ID")
                    or first_sheet_id(token, ss_token))
        to_write = []
        if read_first_cell(token, ss_token, sheet_id) != HEADER[0]:
            to_write.append(HEADER)
        to_write.extend(rows)
        append_rows(token, ss_token, sheet_id, to_write)
        print(f"Appended {len(rows)} row(s) to sheet {sheet_id}.")
        return

    # docx target (wiki node or raw docx id)
    if args.md:
        blocks = md_to_blocks(open(args.md).read())
    else:
        blocks = report_to_blocks(json.loads(open(args.input).read()), ts)
    if args.dry_run:
        print(json.dumps(blocks, ensure_ascii=False, indent=2))
        print(f"\n{len(blocks)} block(s) would be appended.")
        return
    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    wiki_token = os.environ.get("FEISHU_WIKI_TOKEN")
    docx_token = os.environ.get("FEISHU_DOCX_TOKEN")
    if not (app_id and app_secret and (wiki_token or docx_token)):
        sys.exit("Set FEISHU_APP_ID, FEISHU_APP_SECRET, and "
                 "FEISHU_WIKI_TOKEN (.../wiki/<id>) or FEISHU_DOCX_TOKEN.")
    token = tenant_token(app_id, app_secret)
    doc_id = docx_token or wiki_node_to_docx(token, wiki_token)
    docx_append(token, doc_id, blocks)
    print(f"Appended {len(blocks)} block(s) to docx {doc_id}.")


if __name__ == "__main__":
    main()
