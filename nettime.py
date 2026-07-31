#!/usr/bin/env python3
"""网络时间源 —— 不依赖本机系统时钟（本服务器时钟被改过，不可信）。

通过 HTTP 响应的 `Date:` 头获取当前 UTC 时间，再按本机配置的时区换算成本地
挂钟时间。时区换算只用 /etc/localtime 的规则、与系统时钟无关，所以即便系统
时间被改，得到的仍是正确的当地时间。

所有网络源都不可达时，回退到本机时间（并标记 ok=False，便于上层提示）。

用法：
    from nettime import network_now
    ts, ok = network_now()              # ("2026-07-13 06:13:00", True/False)

    # 命令行（供 shell 调用）：打印一行时间，stderr 标注来源
    python3 nettime.py                  # 默认 %Y-%m-%d %H:%M:%S
    python3 nettime.py '%F %T'
"""
import datetime
import email.utils
import os
import subprocess
import sys

# 本机实测可达：baidu 直连出 Date 头；google 走 http 代理(4780)出 Date 头。
# curl 自动读取 https_proxy/http_proxy 环境变量，故普通调用即可覆盖代理场景。
_URLS = ["https://www.baidu.com", "https://www.google.com", "https://github.com"]


def _date_from(url, timeout=8):
    try:
        out = subprocess.run(
            ["curl", "-sI", "--max-time", str(timeout), url],
            capture_output=True, text=True, timeout=timeout + 3,
        ).stdout
    except Exception:
        return None
    for line in out.splitlines():
        if line[:5].lower() == "date:":
            try:
                dt = email.utils.parsedate_to_datetime(line.split(":", 1)[1].strip())
            except Exception:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt.astimezone(datetime.timezone.utc)
    return None


def network_utc(timeout=8):
    """返回网络 UTC 时间(aware datetime)，全部源失败返回 None。"""
    # 先按环境代理试一轮，再显式无代理试一轮（直连能通的场景，如 baidu）。
    for env in (os.environ, {**os.environ, "https_proxy": "", "http_proxy": ""}):
        for url in _URLS:
            old = {k: os.environ.get(k) for k in ("https_proxy", "http_proxy")}
            try:
                for k in ("https_proxy", "http_proxy"):
                    if k in env:
                        os.environ[k] = env[k]
                dt = _date_from(url, timeout)
            finally:
                for k, v in old.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
            if dt is not None:
                return dt
    return None


def network_now(fmt="%Y-%m-%d %H:%M:%S", timeout=8):
    """返回 (格式化的当地时间字符串, 是否来自网络)。失败回退本机时间。"""
    u = network_utc(timeout)
    if u is None:
        return datetime.datetime.now().strftime(fmt), False
    return u.astimezone().strftime(fmt), True


if __name__ == "__main__":
    fmt = sys.argv[1] if len(sys.argv) > 1 else "%Y-%m-%d %H:%M:%S"
    s, ok = network_now(fmt)
    sys.stderr.write("[nettime] source=%s\n" % ("network" if ok else "LOCAL-FALLBACK"))
    print(s)
