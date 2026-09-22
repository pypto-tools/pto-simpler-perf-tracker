#!/usr/bin/env python3
# Copyright (c) PyPTO Contributors.
# Send a Feishu interactive card to configured recipients (private 1:1 messages,
# not a group). Used by run.sh to alert on a failed perf run.
#
# Reuses the app credentials and HTTP helper already in feishu_perf_report.py.
# Recipient is read from the environment (set in .env):
#   FEISHU_APP_ID, FEISHU_APP_SECRET  — the app (sender)
#   NOTIFY_RECEIVE_ID                 — legacy single recipient open_id (ou_...) or, if
#                                       NOTIFY_RECEIVE_TYPE=email, a tenant email
#   NOTIFY_RECEIVE_TYPE               — open_id (default) | email | user_id | union_id
#   NOTIFY_SUBSCRIBERS_FILE           — JSON subscriber state; if present, all
#                                       listed open_ids receive the notification.
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


def send_text(app_id, app_secret, receive_id, text):
    token = tenant_token(app_id, app_secret)
    url = f"{BASE}/im/v1/messages?receive_id_type=open_id"
    _api("POST", url, token=token, body={
        "receive_id": receive_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    })


def _subscriber_ids():
    path = os.environ.get(
        "NOTIFY_SUBSCRIBERS_FILE",
        "/home/pypto-tools/pto-simpler-perf-tracker/state/subscribers.json",
    )
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return []
    subscribers = data.get("subscribers", {}) if isinstance(data, dict) else {}
    return [key for key, value in subscribers.items()
            if isinstance(key, str) and isinstance(value, dict)]


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
    subscriber_ids = _subscriber_ids()
    if subscriber_ids:
        failures = []
        for subscriber_id in subscriber_ids:
            try:
                send_card(app_id, app_secret, subscriber_id, "open_id",
                          args.title, args.lines, args.status)
            except Exception as exc:
                failures.append(f"{subscriber_id}: {exc}")
        if failures:
            print("some Feishu subscribers failed: " + "; ".join(failures),
                  file=sys.stderr)
            sys.exit(1)
        print(f"feishu notify sent to {len(subscriber_ids)} subscribers")
        return
    if not rid:
        # No target configured yet -> soft no-op so the caller doesn't crash.
        print("NOTIFY_RECEIVE_ID not set; skipping Feishu notify", file=sys.stderr)
        sys.exit(3)

    send_card(app_id, app_secret, rid, rtype, args.title, args.lines, args.status)
    print("feishu notify sent")


if __name__ == "__main__":
    main()
