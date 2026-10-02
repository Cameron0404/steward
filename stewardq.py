#!/usr/bin/env python3
"""stewardq.py - Steward's timed work queue, in $STEWARD_HOME/queue/pending/.

A queue entry is a markdown file with simple `key: value` frontmatter and a body. Two kinds:

  kind: brief   the body is an operating brief for a fresh headless session (agentctl.py run)
  kind: resume  `agentctl.py resume <agent id>`: bring a wound-down or limited run back

`run_at` (local time) says when it becomes due. `stewardq.py tick`, run every minute or few by
launchd, cron or by hand, starts whatever is due: resumes first, so a paused build finishes
before new work starts, then briefs, oldest first. An entry with no run_at is due at the next
03:30 after it was written. A start is held back (and the entry deferred) when the concurrency
cap is full or the weekly usage ceiling is reached, so a crowded night queues itself.

  stewardq.py add --at "03:30" --title "Build X" --file brief.md   # or --text, or stdin
  stewardq.py add --at tonight ...       # tonight = next 03:30
  stewardq.py add --at "+2h" ...         # relative; --at 2026-10-10T03:30 absolute; --at now
  stewardq.py list                       # pending with due times, then recent done
  stewardq.py due                        # print due files, exit 3 if none
  stewardq.py tick [--dry-run]           # start what is due, each as a detached agentctl run

Stdlib only, Python 3.9+.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
import zoneinfo
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from steward_config import ROOT, TZ as P  # noqa: E402

QUEUE = ROOT / "queue"
PENDING = QUEUE / "pending"
DONE = QUEUE / "done"
LOGS = QUEUE / "logs"
DEFAULT_HOUR = (3, 30)
NOTHING_DUE = 3
REFUSED_DEFER_MIN = 30
HERE = Path(__file__).resolve().parent


def _now() -> dt.datetime:
    return dt.datetime.now(P)


def _slug(text: str, n: int = 48) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n].rstrip("-") or "work"


def _machine_zone():
    return P if isinstance(P, zoneinfo.ZoneInfo) else dt.datetime.now().astimezone().tzinfo


def next_default(after: dt.datetime) -> dt.datetime:
    h, m = DEFAULT_HOUR
    cand = after.replace(hour=h, minute=m, second=0, microsecond=0)
    if cand <= after:
        cand += dt.timedelta(days=1)
    return cand


def parse_at(text: str, now: dt.datetime | None = None) -> dt.datetime:
    """'03:30' (next occurrence), '2026-10-10T03:30', '2026-10-10 03:30', '+2h', '+90m',
    'tonight' / 'overnight' (next 03:30), 'now'."""
    now = now or _now()
    t = text.strip().lower()
    if t in ("tonight", "overnight", "default"):
        return next_default(now)
    if t == "now":
        return now
    m = re.fullmatch(r"\+(\d+)\s*([hm])", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return now + (dt.timedelta(hours=n) if unit == "h" else dt.timedelta(minutes=n))
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
    if m:
        local_now = now.astimezone(_machine_zone())
        cand = local_now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if cand <= local_now:
            cand += dt.timedelta(days=1)
        return cand.astimezone(P)
    d = dt.datetime.fromisoformat(text.strip().replace(" ", "T"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=_machine_zone())
    return d.astimezone(P)


def parse_reset(text: str, now: dt.datetime | None = None) -> dt.datetime | None:
    """The reset moment named in a claude usage-limit message, or None.
    Handles 'resets 12:30pm (Europe/London)', 'resets 3am', 'resets at 14:00', 'resets in 2h 15m'.
    A time with no zone, or a zone zoneinfo cannot resolve, is read on this machine's clock,
    because that is where the CLI rendered it."""
    now = now or _now()
    m = re.search(r"resets?\s+in\s+(?:(\d+)\s*h(?:ours?)?)?\s*(?:(\d+)\s*m(?:in(?:utes?)?)?)?", text, re.I)
    if m and (m.group(1) or m.group(2)):
        return now + dt.timedelta(hours=int(m.group(1) or 0), minutes=int(m.group(2) or 0))
    m = re.search(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?(?:\s*\(([^)]+)\))?", text, re.I)
    if not m:
        return None
    hour, minute, ampm, tzname = int(m.group(1)), int(m.group(2) or 0), m.group(3), m.group(4)
    if ampm:
        ampm = ampm.lower()
        if ampm == "pm" and hour != 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
    tz = _machine_zone()
    if tzname:
        try:
            tz = zoneinfo.ZoneInfo(tzname.strip())
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            tz = _machine_zone()
    local_now = now.astimezone(tz)
    cand = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if cand <= local_now - dt.timedelta(minutes=2):
        cand += dt.timedelta(days=1)
    return cand.astimezone(P)


# ------------------------------------------------------------------------------ entries

def _split(text: str) -> tuple[dict, str]:
    fm: dict = {}
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            for line in text[4:end].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    fm[k.strip()] = v.strip()
            return fm, text[end + 4:].lstrip("\n")
    return fm, text


def _int(value, fallback: int) -> int:
    """A frontmatter number, or the fallback. One damaged entry must not stop the queue."""
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def entry(path: Path) -> dict:
    fm, body = _split(path.read_text(encoding="utf-8", errors="replace"))
    mtime = dt.datetime.fromtimestamp(path.stat().st_mtime, P)
    try:
        run_at = parse_at(fm["run_at"]) if fm.get("run_at") else next_default(mtime)
    except ValueError:
        run_at = next_default(mtime)
    return {
        "path": path, "fm": fm, "body": body,
        "title": fm.get("title") or (body.splitlines()[0].lstrip("# ").strip() if body.strip() else path.stem),
        "kind": fm.get("kind", "brief"), "model": fm.get("model", ""), "agent": fm.get("agent", ""),
        "run_at": run_at, "reason": fm.get("reason", ""), "repo": fm.get("repo", ""),
        "max_minutes": _int(fm.get("max_minutes"), 0),
    }


def _write(path: Path, fm: dict, body: str) -> Path:
    PENDING.mkdir(parents=True, exist_ok=True)
    DONE.mkdir(parents=True, exist_ok=True)
    lines = ["---"] + [f"{k}: {v}" for k, v in fm.items() if v not in ("", None)] + ["---", ""]
    path.write_text("\n".join(lines) + body.rstrip("\n") + "\n", encoding="utf-8")
    return path


def add_brief(title: str, body: str, run_at: dt.datetime, model: str = "", reason: str = "",
              max_minutes: int = 0, repo: str = "") -> Path:
    PENDING.mkdir(parents=True, exist_ok=True)
    for q in PENDING.glob(f"{run_at:%Y-%m-%d}-*.md"):   # same title, same day, still pending: one is enough
        e = entry(q)
        if e["kind"] == "brief" and e["title"].strip().lower() == title.strip().lower():
            print(f"stewardq: a brief titled {title!r} is already pending for {e['run_at']:%a %d %b %H:%M}: {q.name}")
            return q
    name = f"{run_at:%Y-%m-%d-%H%M}-{_slug(title)}.md"
    fm = {"title": title, "kind": "brief", "run_at": run_at.strftime("%Y-%m-%dT%H:%M"), "model": model,
          "queued": _now().strftime("%Y-%m-%dT%H:%M"), "reason": reason,
          "max_minutes": str(max_minutes) if max_minutes else "", "repo": repo}
    return _write(PENDING / name, fm, body)


def add_resume(agent_id: str, run_at: dt.datetime, reason: str = "") -> Path | None:
    """Queue `agentctl.py resume AGENT_ID` for run_at (a wind-down or a limit hit). Only a FUTURE
    entry counts as already queued: one whose time has passed is the entry being run right now,
    and a resume wound down again within seconds must still get a new one."""
    now = _now()
    PENDING.mkdir(parents=True, exist_ok=True)
    for p in PENDING.glob("*.md"):
        e = entry(p)
        if e["kind"] == "resume" and e["agent"] == agent_id and e["run_at"] > now:
            print(f"stewardq: a resume of {agent_id} is already queued for {e['run_at']:%H:%M}: {p.name}")
            return p
    name = f"{run_at:%Y-%m-%d-%H%M}-resume-{_slug(agent_id)}.md"
    fm = {"title": f"Resume agent {agent_id}", "kind": "resume", "agent": agent_id,
          "run_at": run_at.strftime("%Y-%m-%dT%H:%M"), "queued": _now().strftime("%Y-%m-%dT%H:%M"),
          "reason": reason}
    return _write(PENDING / name, fm, f"agentctl.py resume {agent_id}. {reason}\n")


def defer(path: Path, minutes: int, reason: str = "") -> Path:
    e = entry(path)
    fm = dict(e["fm"])
    fm.update({"title": e["title"], "kind": e["kind"],
               "run_at": (_now() + dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M"),
               "reason": reason or e["reason"]})
    return _write(path, fm, e["body"])


def due(now: dt.datetime | None = None) -> list[Path]:
    """Due entries, resumes first, then by run_at. A damaged entry is named and skipped."""
    now = now or _now()
    out = []
    for p in PENDING.glob("*.md"):
        try:
            e = entry(p)
        except (OSError, ValueError) as ex:
            print(f"stewardq: skipped damaged entry {p.name}: {type(ex).__name__}: {ex}", file=sys.stderr)
            continue
        if e["run_at"] <= now:
            out.append((0 if e["kind"] == "resume" else 1, e["run_at"], p.name, p))
    return [p for *_, p in sorted(out)]


def _finish(path: Path, suffix: str = "") -> Path:
    DONE.mkdir(parents=True, exist_ok=True)
    dest = DONE / f"{_now():%Y-%m-%d_%H%M}_{path.stem}{suffix}.md"
    path.rename(dest)
    return dest


def tick(dry: bool = False) -> int:
    """Start every due entry that fits under the cap, each as a detached agentctl process."""
    import agentctl as A
    A.reap()
    started = 0
    seen: set = set()
    for p in due():
        e = entry(p)
        if e["kind"] == "resume" and e["agent"] in seen:
            print(f"stewardq: {e['agent']} already resumed in this tick, {p.name} closed")
            if not dry:
                _finish(p, "-duplicate")
            continue
        running = A._running_others("")
        if running >= A.MAX_CONCURRENT:
            print(f"stewardq: {running} runs already running (cap {A.MAX_CONCURRENT}), {p.name} deferred")
            if not dry:
                defer(p, REFUSED_DEFER_MIN, f"concurrency cap, retrying in {REFUSED_DEFER_MIN} min")
            continue
        if e["kind"] == "brief" and A.weekly_ceiling_hit(A.usage_now(), e["model"]):
            print(f"stewardq: weekly usage ceiling reached, {p.name} deferred")
            if not dry:
                defer(p, REFUSED_DEFER_MIN, "weekly usage ceiling")
            continue
        if e["kind"] == "resume":
            argv = [sys.executable, str(HERE / "agentctl.py"), "resume", e["agent"]]
            rid = e["agent"]
            seen.add(rid)
        else:
            rid = p.stem
            brief = QUEUE / "briefs" / f"{rid}.md"
            argv = [sys.executable, str(HERE / "agentctl.py"), "run", "--id", rid, "--brief", str(brief)]
            if e["model"]:
                argv += ["--model", e["model"]]
            if e["max_minutes"]:
                argv += ["--max-minutes", str(e["max_minutes"])]
            if e["repo"]:
                argv += ["--repo", e["repo"]]
        print(("would start: " if dry else "start: ") + " ".join(argv[1:]))
        if dry:
            continue
        if e["kind"] == "brief":
            brief.parent.mkdir(parents=True, exist_ok=True)
            brief.write_text(e["body"], encoding="utf-8")
        LOGS.mkdir(parents=True, exist_ok=True)
        with open(LOGS / f"{rid}.log", "a") as log:
            subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=True, cwd=os.getcwd())
        _finish(p)
        started += 1
    return started


# ---------------------------------------------------------------------------------- cli

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("--at", default="tonight")
    a.add_argument("--title", required=True)
    a.add_argument("--file")
    a.add_argument("--text")
    a.add_argument("--model", default="")
    a.add_argument("--reason", default="")
    a.add_argument("--max-minutes", type=int, default=0)
    a.add_argument("--repo", default="")
    sub.add_parser("list")
    sub.add_parser("due")
    t = sub.add_parser("tick")
    t.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.cmd == "add":
        body = Path(args.file).read_text(encoding="utf-8") if args.file else (args.text or sys.stdin.read())
        p = add_brief(args.title, body, parse_at(args.at), model=args.model, reason=args.reason,
                      max_minutes=args.max_minutes, repo=args.repo)
        print(f"queued {p.name} for {entry(p)['run_at']:%a %d %b %H:%M}")
        return 0
    if args.cmd == "list":
        rows = sorted((entry(p) for p in PENDING.glob("*.md")), key=lambda e: e["run_at"])
        print(f"pending: {len(rows)}")
        for e in rows:
            print(f"  {e['run_at']:%a %d %b %H:%M}  {e['kind']:6}  {e['title']}")
        done = sorted(DONE.glob("*.md"))[-10:]
        if done:
            print("recently done:")
            for p in done:
                print(f"  {p.name}")
        return 0
    if args.cmd == "due":
        d = due()
        for p in d:
            print(p)
        return 0 if d else NOTHING_DUE
    if args.cmd == "tick":
        n = tick(dry=args.dry_run)
        print(f"stewardq: {n} started")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
