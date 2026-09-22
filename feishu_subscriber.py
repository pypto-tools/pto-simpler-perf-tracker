#!/usr/bin/env python3
"""Keep a Feishu long connection and manage private notification subscribers.

Users can send ``订阅`` or ``退订`` to the app bot.  The long-connection mode
does not require a public HTTP endpoint; the process dials out to Feishu.
"""

import json
import os
import sys
from pathlib import Path


def _state_path():
    return Path(os.environ.get(
        "NOTIFY_SUBSCRIBERS_FILE",
        "/home/pypto-tools/pto-simpler-perf-tracker/state/subscribers.json",
    ))


def _load(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"subscribers": {}}
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read subscriber state: {exc}", file=sys.stderr)
        return {"subscribers": {}}
    if not isinstance(data, dict) or not isinstance(data.get("subscribers"), dict):
        return {"subscribers": {}}
    return data


def _save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    os.replace(temp, path)


def update_subscription(open_id, display_name, subscribe):
    path = _state_path()
    data = _load(path)
    subscribers = data.setdefault("subscribers", {})
    if subscribe:
        subscribers[open_id] = {"name": display_name or "", "open_id": open_id}
    else:
        subscribers.pop(open_id, None)
    _save(path, data)
    return len(subscribers)


def subscribed(open_id):
    return open_id in _load(_state_path()).get("subscribers", {})


def _message_text(message):
    try:
        content = json.loads(message.content or "{}")
    except (TypeError, json.JSONDecodeError):
        return ""
    return str(content.get("text", "")).strip()


def _reply(app_id, app_secret, open_id, text):
    # Import lazily so ordinary report publishing does not require lark-oapi.
    from notify_feishu import send_text
    send_text(app_id, app_secret, open_id, text)


def handle_message(data):
    event = data.event
    message = event.message
    sender = event.sender.sender_id
    open_id = sender.open_id
    text = _message_text(message)
    if not text:
        return

    app_id = os.environ["FEISHU_APP_ID"]
    app_secret = os.environ["FEISHU_APP_SECRET"]
    if text in {"订阅", "subscribe"}:
        count = update_subscription(open_id, "", True)
        _reply(app_id, app_secret, open_id, f"已订阅 simpler 通知，当前订阅人数：{count}")
    elif text in {"退订", "取消订阅", "unsubscribe"}:
        update_subscription(open_id, "", False)
        _reply(app_id, app_secret, open_id, "已取消 simpler 通知订阅")
    elif text in {"订阅状态", "status"}:
        status = "已订阅" if subscribed(open_id) else "未订阅"
        _reply(app_id, app_secret, open_id, f"当前状态：{status}")


def main():
    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        raise SystemExit("FEISHU_APP_ID / FEISHU_APP_SECRET not set")
    try:
        import lark_oapi as lark
        from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
    except ImportError as exc:
        raise SystemExit("install dependency: python3 -m pip install lark-oapi") from exc

    def on_message(data: P2ImMessageReceiveV1):
        try:
            handle_message(data)
        except Exception as exc:  # keep the long connection alive after one bad event
            print(f"subscriber event failed: {exc}", file=sys.stderr)

    handler = (lark.EventDispatcherHandler.builder("", "")
               .register_p2_im_message_receive_v1(on_message)
               .build())
    client = lark.ws.Client(
        app_id, app_secret, event_handler=handler,
        log_level=lark.LogLevel.INFO,
    )
    client.start()


if __name__ == "__main__":
    main()
