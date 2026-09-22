#!/usr/bin/env python3
"""Build the top-level index for all Simpler performance and CI reports."""

import json
import os
from pathlib import Path

import feishu_perf_report as feishu


def _load(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def sync_report_hub(token, state_root, domain="hw-native-sys.feishu.cn"):
    """Create/update the managed hub and return its document URL.

    The hub contains links only; detailed data stays in the existing
    performance, weekly-CI, and daily-error indexes.
    """
    state_root = Path(state_root)
    hub_state_path = state_root / "report-hub-state.json"
    hub_state = _load(hub_state_path, {})
    hub_doc = (os.environ.get("FEISHU_REPORT_HUB_DOCX_TOKEN")
               or hub_state.get("doc_id"))
    if not hub_doc:
        hub_doc = feishu.docx_create(token, "Simpler 性能与 CI 报告总索引")
        hub_state = {"doc_id": hub_doc, "managed": True}
        hub_state_path.write_text(json.dumps(hub_state, ensure_ascii=False, indent=2) + "\n")
        try:
            feishu.set_doc_link_editable(token, hub_doc, "tenant")
        except SystemExit:
            pass

    performance = _load(state_root / "work" / "feishu_state.json", {})
    weekly = _load(state_root / "ci-weekly-state.json", {})
    daily = _load(state_root / "ci-daily" / "feishu-state.json", {})
    performance_doc = performance.get("index_doc")
    weekly_doc = weekly.get("ci_index_doc")
    daily_doc = daily.get("index_doc") or daily.get("doc_id")

    def link(label, doc_id):
        target = f"https://{domain}/docx/{doc_id}" if doc_id else "（尚未生成）"
        return feishu._text_block(f"{label} → {target}")

    blocks = [feishu._heading_block(2, "Simpler 性能与 CI 报告总索引")]
    blocks += [feishu._heading_block(3, "每日性能"),
               link("性能报告索引", performance_doc)]
    blocks += [feishu._heading_block(3, "每周 CI Job"),
               link("CI Job 周报索引", weekly_doc)]
    blocks += [feishu._heading_block(3, "每日 CI 报错"),
               link("CI 报错日报索引", daily_doc)]
    feishu.clear_doc(token, hub_doc)
    feishu.docx_append(token, hub_doc, blocks)
    return f"https://{domain}/docx/{hub_doc}"

