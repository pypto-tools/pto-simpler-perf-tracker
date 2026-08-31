#!/usr/bin/env python3
"""Run a command once inside an allowed *network Beijing time* window.

The host clock on the benchmark server is not trusted.  This launcher gets UTC
from HTTP Date headers via nettime.network_utc(), converts it explicitly to
Asia/Shanghai, and refuses to run when no network time source is available.

It also provides a non-blocking process lock and a success stamp so a cron job
may invoke it frequently without starting duplicate benchmark runs.
"""

import argparse
import datetime as dt
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from zoneinfo import ZoneInfo

from nettime import network_utc


BEIJING = ZoneInfo("Asia/Shanghai")
DEFAULT_WINDOWS = ("12:30-13:30", "22:00-08:00")


def parse_clock(value):
    try:
        hour, minute = (int(x) for x in value.split(":"))
        return dt.time(hour, minute)
    except (TypeError, ValueError):
        raise ValueError(f"invalid clock {value!r}; expected HH:MM") from None


def parse_window(value):
    try:
        start, end = value.split("-", 1)
        return parse_clock(start), parse_clock(end)
    except ValueError as exc:
        if str(exc).startswith("invalid clock"):
            raise
        raise ValueError(f"invalid window {value!r}; expected HH:MM-HH:MM") from None


def matching_window(now, windows):
    """Return the matching (start, end), using start-inclusive/end-exclusive."""
    current = now.timetz().replace(tzinfo=None)
    for start, end in windows:
        if start < end:
            inside = start <= current < end
        else:
            inside = current >= start or current < end
        if inside:
            return start, end
    return None


def logical_date(now, window):
    """Assign after-midnight hours to the date on which the night window began."""
    start, end = window
    current = now.timetz().replace(tzinfo=None)
    if start > end and current < end:
        return now.date() - dt.timedelta(days=1)
    return now.date()


def remaining_minutes(now, window):
    """Whole minutes left before the matching window closes."""
    start, end = window
    current = now.timetz().replace(tzinfo=None)
    end_date = now.date()
    if start > end and current >= start:
        end_date += dt.timedelta(days=1)
    end_at = dt.datetime.combine(end_date, end, tzinfo=now.tzinfo)
    return max(0, int((end_at - now).total_seconds() // 60))


def stamp_has_run(stamp_path, run_id):
    try:
        return stamp_path.read_text().splitlines()[0] == run_id
    except (OSError, IndexError):
        return False


def write_stamp(stamp_path, run_id, now):
    stamp_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{stamp_path.name}.",
                                    dir=stamp_path.parent, text=True)
    try:
        with os.fdopen(fd, "w") as tmp:
            tmp.write(f"{run_id}\n{now.isoformat()}\n")
        os.replace(tmp_name, stamp_path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window", action="append", dest="windows",
                        help="allowed Beijing-time window HH:MM-HH:MM; repeatable")
    parser.add_argument("--lock", required=True, type=Path,
                        help="shared non-blocking process lock")
    parser.add_argument("--stamp", required=True, type=Path,
                        help="success stamp used for idempotency")
    parser.add_argument("--attempt-stamp", type=Path,
                        help="optional once-per-logical-day attempt stamp")
    parser.add_argument("--run-id",
                        help="fixed one-shot id; default is daily:<logical date>")
    parser.add_argument("--network-timeout", type=int, default=8)
    parser.add_argument("--min-remaining-minutes", type=int, default=0,
                        help="do not start too close to the end of a window")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()

    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")
    try:
        windows = tuple(parse_window(w) for w in (args.windows or DEFAULT_WINDOWS))
    except ValueError as exc:
        parser.error(str(exc))

    # A completed fixed one-shot can retire without even touching the network.
    # Daily ids depend on the network date, so they are checked after lookup.
    if args.run_id and stamp_has_run(args.stamp, args.run_id):
        print(f"[schedule] skip: {args.run_id} already succeeded")
        return 0

    network_time = network_utc(args.network_timeout)
    if network_time is None:
        print("[schedule] skip: network time unavailable (local clock rejected)")
        return 0
    now = network_time.astimezone(BEIJING)
    window = matching_window(now, windows)
    if window is None:
        print(f"[schedule] skip: network Beijing time {now:%F %T} outside window")
        return 0
    remaining = remaining_minutes(now, window)
    if remaining < args.min_remaining_minutes:
        print(f"[schedule] skip: only {remaining} minute(s) remain in window")
        return 0

    run_id = args.run_id or f"daily:{logical_date(now, window):%F}"
    attempt_id = f"{run_id}:attempt:{logical_date(now, window):%F}"
    if args.attempt_stamp and stamp_has_run(args.attempt_stamp, attempt_id):
        print(f"[schedule] skip: {attempt_id} already attempted")
        return 0
    args.lock.parent.mkdir(parents=True, exist_ok=True)
    with args.lock.open("a+") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"[schedule] skip: another perf run holds {args.lock}")
            return 0
        if stamp_has_run(args.stamp, run_id):
            print(f"[schedule] skip: {run_id} already succeeded")
            return 0
        if args.attempt_stamp and stamp_has_run(args.attempt_stamp, attempt_id):
            print(f"[schedule] skip: {attempt_id} already attempted")
            return 0

        print(f"[schedule] start {run_id} at network Beijing time {now:%F %T}",
              flush=True)
        if args.attempt_stamp:
            write_stamp(args.attempt_stamp, attempt_id, now)
        result = subprocess.run(command)
        if result.returncode == 0:
            write_stamp(args.stamp, run_id, now)
            print(f"[schedule] success: {run_id}", flush=True)
        else:
            print(f"[schedule] failed: {run_id}, rc={result.returncode}",
                  file=sys.stderr, flush=True)
        return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
