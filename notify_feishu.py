#!/usr/bin/env python3
# Copyright (c) PyPTO Contributors.
# Send a Feishu interactive card to a single configured recipient (a private
# 1:1 message, not a group). Used by run.sh to alert on a failed perf run.
#
# Reuses the app credentials and HTTP helper already in feishu_perf_report.py.
# Recipient is read from the environment (set in .env):
#   FEISHU_APP_ID, FEISHU_APP_SECRET  — the app (sender)
#   NOTIFY_RECEIVE_ID                 — who to DM: an open_id (ou_...) or, if
#                                       NOTIFY_RECEIVE_TYPE=email, a tenant email
#   NOTIFY_RECEIVE_TYPE               — open_id (default) | email | user_id | union_id
#
# Exit codes: 0 sent; 3 recipient not configured (caller treats as soft —
# a missing target must never turn a perf failure into a cron crash).
#
# Usage:
#   python notify_feishu.py --title "perf-tracker FAILED" --status fail \
#       --line "stage: benchmark shards" --line "see work/cron.log"

import argparse
import json
import os
import sys

from feishu_perf_report import BASE, _api, tenant_token

# red = failure/alert, green = recovery/ok, blue = neutral info.
_TEMPLATE = {"fail": "red", "ok": "green", "info": "blue"}


def send_card(app_id, app_secret, receive_id, receive_type, title, lines, status):
    token = tenant_token(app_id, app_secret)
    body_md = "\n".join(lines) if lines else "_(no detail)_"
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": _TEMPLATE.get(status, "blue"),
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": body_md}}],
    }
    url = f"{BASE}/im/v1/messages?receive_id_type={receive_type}"
    _api("POST", url, token=token, body={
        "receive_id": receive_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
    })


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True)
    ap.add_argument("--status", default="info", choices=["fail", "ok", "info"])
    ap.add_argument("--line", action="append", default=[], dest="lines")
    args = ap.parse_args()

    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    rid = os.environ.get("NOTIFY_RECEIVE_ID")
    rtype = os.environ.get("NOTIFY_RECEIVE_TYPE", "open_id")
    if not (app_id and app_secret):
        sys.exit("FEISHU_APP_ID / FEISHU_APP_SECRET not set")
    if not rid:
        # No target configured yet -> soft no-op so the caller doesn't crash.
        print("NOTIFY_RECEIVE_ID not set; skipping Feishu notify", file=sys.stderr)
        sys.exit(3)

    send_card(app_id, app_secret, rid, rtype, args.title, args.lines, args.status)
    print("feishu notify sent")


if __name__ == "__main__":
    main()
