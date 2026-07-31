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
import re
import sys
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
            with urllib.request.urlopen(req, timeout=60) as r:
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
    # language 1 = PlainText; wrap so wide tables don't overflow.
    return {"block_type": 14,
            "code": {"elements": _elements(text),
                     "style": {"language": 1, "wrap": True}}}


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


def _commit_descendants(entry, prev, counter):
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
        desc.append({"block_id": cb, **_code_block(entry.get("summary") or "(no data)")})
        top.append(cb)
        return top, desc

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
        for c in row:
            add_cell(c)

    # Widen columns: example name is long, metric cells carry "(±x%)".
    col_w = [360] + [150] * (len(headers) - 1)
    tbl = nid()
    desc.append({"block_id": tbl, "block_type": 31,
                 "table": {"property": {"row_size": len(examples) + 1,
                                        "column_size": len(headers),
                                        "column_width": col_w,
                                        "header_row": True}},
                 "children": cell_ids})
    top.append(tbl)
    return top, desc


def _load_state(p):
    try:
        return json.load(open(p))
    except (OSError, ValueError):
        return {"index_doc": None, "months": {}, "pushed": []}


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

    state = _load_state(state_path)

    def save():
        json.dump(state, open(state_path, "w"), indent=2)

    if rebuild:
        # Overwrite: empty the existing month docs and forget what was pushed,
        # so the fresh ordered set fully replaces the old (gappy) content.
        for m, did in state.get("months", {}).items():
            print(f"clearing month doc {m} ({did}) ...")
            clear_doc(token, did)
        state["pushed"] = []
        save()
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
    fresh = [e for e in entries if e["sha"] not in pushed]
    if not fresh:
        print("nothing new to publish (all commits already pushed).")
        return f"https://{domain}/docx/{state['index_doc']}"
    months = {}
    for e in fresh:
        months.setdefault((e.get("date") or "0000-00")[:7], []).append(e)

    for month in sorted(months, reverse=True):
        if month not in state["months"]:
            did = docx_create(token, f"{title_prefix} {month}")
            state["months"][month] = did
            save()  # persist before the (failable) link/table writes
            try:
                set_doc_link_editable(token, did, "anyone")
            except SystemExit:
                pass
            url = f"https://{domain}/docx/{did}"
            link = {"block_id": "il", "block_type": 2,
                    "text": {"elements": _elements(f"📅 {month} → {url}")}}
            # Backfill creates progressively older months -> link at the bottom
            # of the index; the forward daily run prepends newer ones at top.
            li = docx_children_count(token, state["index_doc"]) if append else 0
            docx_append_descendant(token, state["index_doc"], ["il"], [link], li)
            print(f"created month doc {month}: {url}")
        # rebuild writes into freshly-cleared docs, newest-on-top (like the
        # forward run): prepend.
        push_tables(token, state["months"][month], months[month],
                    prepend=not (append and not rebuild))
        state.setdefault("pushed", []).extend(e["sha"] for e in months[month])
        save()  # persist pushed-set incrementally (crash-safe / idempotent)

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


def push_tables(token, doc_id, entries, dry_run=False, prepend=False):
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
        if e.get("date") != last_date:
            last_date = e.get("date")
            if not dry_run:
                docx_append_descendant(token, doc_id, ["dh"],
                                       [_date_heading_block(last_date)], idx)
            idx += 1
        top, desc = _commit_descendants(e, prev, [0])
        if dry_run:
            print(f"  [{e.get('date')}] {e['sha'][:10]} -> "
                  f"{len(top)} top, {len(desc)} descendants")
            continue
        docx_append_descendant(token, doc_id, top, desc, idx)
        idx += len(top)
        print(f"pushed {e['sha'][:10]} ({i + 1}/{len(entries)})")


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
        entries = [json.loads(l) for l in open(args.from_processed) if l.strip()]
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
