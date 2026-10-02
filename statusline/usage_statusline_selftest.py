#!/usr/bin/env python3
"""The status line sensor writes what agentctl.py's watchdog reads. Temporary STEWARD_HOME only."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    rows = []
    with tempfile.TemporaryDirectory(prefix="zz-test-statusline-") as td:
        env = {**os.environ, "STEWARD_HOME": td}
        reset = int(time.time()) + 3600
        doc = {"model": {"display_name": "Example"}, "rate_limits": {
            "five_hour": {"used_percentage": 42, "resets_at": reset},
            "seven_day": {"used_percentage": 17, "resets_at": reset + 86400}}}
        out = subprocess.run([sys.executable, str(HERE / "usage_statusline.py")], input=json.dumps(doc),
                             capture_output=True, text=True, env=env).stdout
        rows.append(("prints the windows", "5h 42%" in out and "7d 17%" in out, out))
        # a later update with no rate_limits keeps the windows whose reset is still ahead
        subprocess.run([sys.executable, str(HERE / "usage_statusline.py")], input='{"model": {}}',
                       capture_output=True, text=True, env=env)
        saved = json.loads((Path(td) / "health" / "usage.json").read_text())
        rows.append(("an update with no windows keeps the live ones",
                     saved["data"]["rate_limits"]["five_hour"]["used_percentage"] == 42, json.dumps(saved)[:200]))
        probe = ("import sys, json; sys.path.insert(0, %r); import agentctl as A; u = A.usage_now(); "
                 "print(json.dumps({'pct': u['pct'], 'fresh': u['fresh'], 'week': u['week_pct']}))" % str(HERE.parent))
        got = json.loads(subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=env).stdout)
        rows.append(("agentctl reads it as fresh, 42% and 17%", got == {"pct": 42.0, "fresh": True, "week": 17.0}, str(got)))
        bad = subprocess.run([sys.executable, str(HERE / "usage_statusline.py")], input="not json",
                             capture_output=True, text=True, env=env)
        rows.append(("bad input never fails loudly", bad.returncode == 0, bad.stderr[-200:]))
    failed = 0
    for name, ok, detail in rows:
        failed += not ok
        print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "\n      " + detail))
    print("\nstatusline selftest: %d/%d passed" % (len(rows) - failed, len(rows)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
