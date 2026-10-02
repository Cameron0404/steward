#!/usr/bin/env python3
"""usage_statusline.py - a Claude Code status line command that doubles as Steward's usage sensor.

Claude Code pipes a JSON document to the status line command on every assistant message and on
its refresh timer. For Claude subscription accounts it carries rate_limits.five_hour and
rate_limits.seven_day, each with used_percentage (0 to 100) and resets_at (Unix seconds). This
script saves the whole document atomically to $STEWARD_HOME/health/usage.json, which is what
agentctl.py's watchdog reads, and prints one short line for the terminal.

Wire it up in ~/.claude/settings.json:

  "statusLine": {"type": "command", "command": "python3 /path/to/steward/statusline/usage_statusline.py"}

Fast on purpose: Claude Code cancels a status line command that is still running when the next
update fires. Stdlib only.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(os.environ.get("STEWARD_HOME") or (Path.home() / ".steward")).expanduser()
OUT = ROOT / "health" / "usage.json"


def merge_previous(data: dict, now: dt.datetime) -> dict:
    """Some updates arrive without rate_limits, or without one window. Keep the last known
    window while its reset is still ahead, so the watchdog never flaps to 'unknown'."""
    if not isinstance(data, dict) or not OUT.exists():
        return data
    try:
        prev = json.loads(OUT.read_text()).get("data", {}).get("rate_limits") or {}
    except (OSError, ValueError, AttributeError):
        return data
    cur = data.get("rate_limits") if isinstance(data.get("rate_limits"), dict) else {}
    merged = dict(cur)
    for key in ("five_hour", "seven_day"):
        if key in cur:
            continue
        reset = (prev.get(key) or {}).get("resets_at")
        if isinstance(reset, (int, float)) and reset > now.timestamp():
            merged[key] = prev[key]
    if merged:
        data["rate_limits"] = merged
    return data


def save(data: dict, now: dt.datetime) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"_written": now.isoformat(timespec="seconds"), "_source": "statusline",
                       "data": merge_previous(data, now)})
    fd, tmp = tempfile.mkstemp(dir=str(OUT.parent), prefix=".usage-")
    with os.fdopen(fd, "w") as fh:
        fh.write(body)
    os.replace(tmp, OUT)


def line(data: dict) -> str:
    model = ((data.get("model") or {}).get("display_name")) or "claude"
    rl = data.get("rate_limits") or {}
    parts = [model]
    for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
        pct = (rl.get(key) or {}).get("used_percentage")
        if isinstance(pct, (int, float)):
            parts.append(f"{label} {pct:.0f}%")
    return "  |  ".join(parts)


def main() -> int:
    now = dt.datetime.now().astimezone()
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        data = {}
    try:
        save(data, now)
    except Exception:  # noqa: BLE001  a status line must never fail loudly
        pass
    print(line(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
