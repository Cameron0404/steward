#!/usr/bin/env python3
"""stewardq.py's tests, against a temporary STEWARD_HOME. Nothing launches.

    python3 stewardq_selftest.py

Stdlib only. Compiles under 3.9.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.TemporaryDirectory(prefix="zz-test-stewardq-")
os.environ["STEWARD_HOME"] = _TMP.name
os.environ["STEWARD_TZ"] = "Europe/London"
os.environ.pop("STEWARD_MAX_CONCURRENT", None)
sys.path.insert(0, str(Path(__file__).resolve().parent))
import stewardq as Q  # noqa: E402
import agentctl as A  # noqa: E402

RESULTS: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))


def t_parse() -> None:
    now = dt.datetime(2026, 10, 1, 12, 0, tzinfo=Q.P)
    check("+2h is two hours on", Q.parse_at("+2h", now) == now + dt.timedelta(hours=2))
    check("+90m is ninety minutes on", Q.parse_at("+90m", now) == now + dt.timedelta(minutes=90))
    check("tonight is the next 03:30", Q.parse_at("tonight", now) == now.replace(day=2, hour=3, minute=30))
    check("a wall time already past today is tomorrow", Q.parse_at("11:00", now).day == 2)
    check("a wall time still ahead is today", Q.parse_at("13:15", now).day == 1)
    r = Q.parse_reset("You've hit your session limit · resets 3pm (Europe/London)", now)
    check("parse_reset reads '3pm (Europe/London)'", r is not None and r.hour == 15 and r.day == 1, str(r))
    r = Q.parse_reset("limit reached, resets in 2h 15m", now)
    check("parse_reset reads 'in 2h 15m'", r == now + dt.timedelta(hours=2, minutes=15), str(r))
    check("parse_reset with no time is None", Q.parse_reset("nothing here", now) is None)


def t_entries() -> None:
    past = Q._now() - dt.timedelta(minutes=5)
    a = Q.add_brief("Example build", "# Example\nDo the thing.\n", past, model="claude-haiku-4-5-20251001")
    b = Q.add_brief("Example build", "duplicate\n", past)
    check("the same title on the same day is queued once", a == b and len(list(Q.PENDING.glob("*.md"))) == 1)
    r1 = Q.add_resume("run-x", past, "wound down")
    r2 = Q.add_resume("run-x", Q._now() + dt.timedelta(hours=1), "again")
    check("a resume whose time has passed does not block a new one", r1 != r2, f"{r1} {r2}")
    r3 = Q.add_resume("run-x", Q._now() + dt.timedelta(hours=2), "third")
    check("a future resume of the same run is not queued twice", r3 == r2, f"{r3} {r2}")
    (Q.PENDING / "zz-damaged.md").write_text("---\nrun_at: not a date\nmax_minutes: many\n---\nbody\n")
    d = Q.due()
    check("due puts resumes before briefs", d and Q.entry(d[0])["kind"] == "resume", str(d))
    check("a damaged entry is not lost, and does not stop the queue", any(p.name == a.name for p in d), str(d))
    e = Q.entry(a)
    check("entry reads the model back", e["model"] == "claude-haiku-4-5-20251001", e["model"])


def t_tick() -> None:
    started = []
    real = Q.subprocess.Popen
    Q.subprocess.Popen = lambda argv, **k: started.append(argv)
    saved_cap = A.MAX_CONCURRENT
    try:
        n_dry = Q.tick(dry=True)
        check("a dry tick starts nothing and consumes nothing", n_dry == 0 and not started
              and len(list(Q.PENDING.glob("*.md"))) >= 2)
        A.MAX_CONCURRENT = 0
        Q.tick()
        check("a full cap defers instead of starting", not started and all(
            Q.entry(p)["run_at"] > Q._now() for p in Q.PENDING.glob("*.md") if Q.entry(p)["kind"] == "brief"))
        A.MAX_CONCURRENT = saved_cap
        for p in Q.PENDING.glob("*.md"):
            Q.defer(p, -10)
        n = Q.tick()
        kinds = [argv[2] for argv in started]
        check("a tick starts resumes and briefs as detached agentctl runs", n >= 2 and "resume" in kinds and "run" in kinds,
              str(started))
        check("a started brief's body is written where agentctl reads it",
              any((Q.QUEUE / "briefs").glob("*.md")))
        check("started entries move to done", len([p for p in Q.DONE.glob("*.md") if "duplicate" not in p.name]) == n)
        check("two due resumes of one run start it once", kinds.count("resume") == 1, str(kinds))
    finally:
        Q.subprocess.Popen = real
        A.MAX_CONCURRENT = saved_cap


def main() -> int:
    for t in (t_parse, t_entries, t_tick):
        try:
            t()
        except Exception as e:  # noqa: BLE001
            check(f"{t.__name__} ran to the end", False, f"{type(e).__name__}: {e}")
    failed = 0
    for name, ok, detail in RESULTS:
        failed += not ok
        print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "\n      " + detail[:300]))
    print("\nstewardq selftest: %d/%d passed" % (len(RESULTS) - failed, len(RESULTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
