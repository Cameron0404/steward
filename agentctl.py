#!/usr/bin/env python3
"""agentctl.py - Steward, a manager for unattended (headless) Claude Code runs.

Every unattended `claude -p` goes through here so that it has:

  a run directory     $STEWARD_HOME/runs/<id>/ with brief.md, state.json, stream.jsonl (every
                      event the session emitted), log.md (the readable build log, written
                      live), progress.md (the agent's own checkpoint), stderr.log, crash.md
  a watchdog          reads $STEWARD_HOME/health/usage.json (written by the status line of any
                      interactive Claude Code session through statusline/usage_statusline.py, or
                      by `agentctl usage --set`); at the threshold (default 90% of the five-hour
                      window) it winds the agent down: SIGTERM, state dormant, a `resume` entry in
                      the work queue for two minutes after the reset. The session id is kept, so
                      resume brings back the FULL conversation, not just the files on disk.
  limit detection     a session that hits the limit anyway (usage.json stale or absent) is
                      recognised from its stream and re-queued the same way.
  context rotation    past ROTATE_AT_TOKENS of context the session is ended at the next tool
                      result and a fresh one is started from the brief and the checkpoint.
  a time cap          --max-minutes, default 90, SIGTERM then SIGKILL, state timed-out.
  crash forensics     any abnormal end writes crash.md: exit code, terminal reason, the last
                      forty log lines, stderr, the checkpoint, and the exact resume command.

Usage:
  agentctl.py run     --id ID --brief FILE [--model M] [--max-minutes 90] [--threshold 90]
                      [--repo DIR] [--max-budget-usd 25]   # dollar ceiling, 0 for none
  agentctl.py resume  ID [--message TEXT]        # continue a dormant / limited / timed-out run
  agentctl.py pause   ID [--at WHEN]             # manual wind-down, resume queued for WHEN
  agentctl.py kill    ID                         # stop for good
  agentctl.py status  [--all]                    # every run, newest first, plus usage
  agentctl.py logs    ID [--tail 40] [--follow]  # the build log, live with --follow
  agentctl.py report  ID                         # state, checkpoint, last lines, resume command
  agentctl.py render  ID                         # rebuild log.md from stream.jsonl
  agentctl.py usage   [--set PCT --resets ISO]   # what the watchdog sees
  agentctl.py gc      [--days 14] [--stream-days 14]  # archive finished runs into
                      runs/archive/, then drop stream.jsonl from archived runs older than
                      --stream-days (log.md and progress.md are kept forever)

Exit codes for `run`/`resume`: 0 done, 1 failed, 76 dormant (wound down), 77 limited
(hit the usage limit, re-queued), 78 timed out, 79 killed or refused a start (cap or weekly
ceiling), 80 interrupted (API unreachable, the Mac slept or lost network; resume queued five
minutes later, at most 12 times, no crash report), 82 out-of-credits (billing or credits, every
model on the ladder refused: no retry, one notification), 83 no-op (the session ended "done"
with progress.md unchanged), 84 refused (a continuation wrote `status: refused` in progress.md:
the brief is re-queued once, never done).

The session's cwd is `--repo`, else a `**Repo: <path>**` line of the brief, else
$STEWARD_WORKDIR, else the directory agentctl was started from. A session that has not
written progress.md for 20 minutes (STEWARD_NUDGE_MINUTES) is stopped at its next tool result
and resumed in place with one message asking for the checkpoint, once per run.

Configuration is by environment variable (see .env.example). Stdlib only, Python 3.9+.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from steward_config import ROOT, TZ as P, CLAUDE, DEFAULT_WORKDIR, env_float  # noqa: E402

RUNS = ROOT / "runs"
HEALTH = ROOT / "health"
STREAM_KEEP_DAYS = 14   # after this, an archived run keeps log.md and progress.md only
USAGE = HEALTH / "usage.json"


def _default_threshold() -> float:
    """STEWARD_THRESHOLD, else a number in $STEWARD_HOME/health/threshold, else 90. Read at
    launch, so `agentctl.py run` picks a change up without a code edit; --threshold still wins."""
    env = os.environ.get("STEWARD_THRESHOLD")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    try:
        return float((HEALTH / "threshold").read_text().strip())
    except (OSError, ValueError):
        return 90.0


DEFAULT_THRESHOLD = _default_threshold()
USAGE_STALE_S = 45 * 60          # usage older than this cannot justify a wind-down
WATCH_EVERY_S = 20
LIMIT_RE = re.compile(r"hit your (?:session|usage) limit|usage limit reached|out of usage credits"
                      r"|cc_cli_limit_message|rate.?limit(?:ed)?\b.*(?:reset|try again)", re.I)
# "You're out of usage credits. Switch to another model..." carries no reset time. A limit with
# no time in it resumes at the next window reset read from the usage state, and never counts
# against MAX_AUTO_RESUMES.
NETWORK_RE = re.compile(r"can't reach the API server|ENOTFOUND|ECONNRESET|ECONNREFUSED|ETIMEDOUT|EAI_AGAIN|fetch failed|network error", re.I)
# "You're out of usage credits. Switch to another model" is not a window limit: the window can
# be at 2% and one model still refuse while the others answer. Seen in practice: one model
# refused every headless run for hours while the other three all answered.
CREDITS_RE = re.compile(r"out of usage credits|switch to another model", re.I)
# An API error body about the account, not one model: "Your credit balance is too low", a billing
# error. No model and no wait fixes it, so the run stops with status out-of-credits and one
# notification, instead of being read as api_error and relaunched every ten minutes.
BILLING_RE = re.compile(r"credit|billing", re.I)
# A continuation that finds the brief out of scope says so in its checkpoint.
REFUSED_RE = re.compile(r"^\s*status:\s*refused\b", re.I | re.M)
# A brief can name its own repo on a line of its own, `**Repo: <path>**`. Only that exact bold
# form counts, so a plain "Repo: ..." line in a shared brief header does not.
BRIEF_REPO_RE = re.compile(r"^\*\*Repo:\s*`?([^`*\n]+?)`?\s*\*\*\s*$", re.M)
# No checkpoint write for this long while the session is working: one nudge per run. The session
# is stopped at the next tool-result boundary, as for a rotation, and resumed in place with one
# message. STEWARD_NUDGE_MINUTES=0 turns it off.
NUDGE_AFTER_S = int(env_float("STEWARD_NUDGE_MINUTES", 20) * 60)
NUDGE_MSG = ("Manager nudge: your checkpoint `{progress}` has not been written for {mins} minutes. "
             "Overwrite it now with Done, In progress, Next, Files touched and How to verify, then carry "
             "on from where you were. If something blocks you, say what under In progress. If the brief "
             "is out of scope, write `status: refused` on its own line in it and stop. Do not mark it done.")
NETWORK_RETRY_MIN = 5       # ENOTFOUND and friends: the Mac slept or lost Wi-Fi, come back soon
MAX_NETWORK_RETRIES = 12    # an hour of five-minute retries, then it is a real failure
# Tried in order after the model a run asked for. Cheap last, so a long build does not quietly
# become expensive: the ladder is about finishing at all, and the report names the model used.
# Override with STEWARD_MODEL_LADDER (comma-separated full model ids).
MODEL_LADDER = [m.strip() for m in (os.environ.get("STEWARD_MODEL_LADDER") or
                "claude-opus-5-5,claude-sonnet-5,claude-haiku-4-5-20251001").split(",") if m.strip()]
# Every turn re-sends the whole conversation, so a run's cost grows with the square of its length.
# When the last main-thread turn carries this many tokens, the manager ends the session at the
# next tool-result boundary and starts a fresh one seeded with the checkpoint. At most
# MAX_ROTATIONS per manager process, one log line each.
ROTATE_AT_TOKENS = int(env_float("STEWARD_ROTATE_TOKENS", 130_000))
MAX_ROTATIONS = 4
# List-price multipliers against a reference model ($5 in, $0.50 cache read, $10 one-hour write,
# $25 out per million). Used ONLY to estimate a run that ends with no `result` event (timed out,
# orphaned, killed). The state then says cost_estimated. Adjust for your own price list.
RATE_PER_M = {"input": 5.0, "read": 0.5, "write": 10.0, "out": 25.0}
MODEL_FACTOR = (("fable", 1.5), ("opus-5-5", 0.56), ("opus", 1.0), ("sonnet", 0.37), ("haiku", 0.1))
MAX_AUTO_RESUMES = 3
# The seven-day window locks the whole account out for days, not hours, and can be nearly full
# while the five-hour window looks fine.
WEEKLY_WINDDOWN = env_float("STEWARD_WEEKLY_WINDDOWN", 95.0)    # wound down here, resume at the weekly reset
STARTING_GRACE_MIN = 15    # a run at `starting` this long with no live manager is reaped
# log.md or progress.md written this recently means the run is alive, whatever the pids say
# (pids can be unreadable under a sandbox, and a session can outlive its manager).
LIVE_FRESH_MIN = env_float("STEWARD_LIVE_FRESH_MINUTES", 10)
WEEKLY_STALE_S = 6 * 3600  # a weekly reading older than this refuses nothing
WEEKLY_NO_NEW_RUNS = env_float("STEWARD_WEEKLY_NO_NEW_RUNS", 92.0)  # no new Opus/Sonnet run above this; haiku still may
# While this file exists no managed run is stopped for time; the usage wind-down still applies.
# Delete the file to bring the caps back.
NO_TIME_LIMIT_FLAG = HEALTH / "no-time-limit"
EXIT = {"done": 0, "failed": 1, "dormant": 76, "limited": 77, "timed-out": 78, "killed": 79, "interrupted": 80,
        "out-of-credits": 82, "no-op": 83, "refused": 84}

CHECKPOINT_RULES = """
## How you are being run (read this, it matters)
You are a headless Claude Code session run by agentctl.py, the repo's agent manager. No human
is present. Two things follow.

1. Keep a checkpoint. After every meaningful step (a file written, a test run, a decision
   taken) overwrite `{progress}` with five short sections: Done, In progress, Next, Files
   touched, How to verify. Write it as instructions to a future you who has lost all memory.
   Read the file once before your first write to it (the Write tool insists).
2. You may be paused at any moment when the account usage limit nears, then resumed later
   with your conversation intact. On resume, re-read `{progress}` and the working tree
   before continuing, because time has passed and other jobs may have run.

Never wait for a human answer: skip, note it in the checkpoint, and continue. Never run git
push. Commit only if your brief says to.

Every turn re-sends the whole conversation, so turns are the cost:
issue independent reads, greps and commands together in one turn, make related edits together,
never re-read a file you just wrote, and read the part of a large file you need, not all of it.

When your context grows past about 130k tokens the manager may end this session and start a fresh
one that sees only your brief, `{progress}` and the operator's notes. So the checkpoint must carry what
you decided and why, the exact paths of files you own, and the commands that verify your work.
"""


def build_prompt(brief: str, progress: Path, preamble: str = "") -> str:
    """Constant blocks first, the brief last, so the shared prefix can be a cache hit."""
    return (CHECKPOINT_RULES.format(progress=progress) + stretch_block() + style_block()
            + "\n\n" + preamble + "# Your brief\n\n" + brief.strip() + "\n")


def rotation_preamble(progress: Path, tokens: int, k: int, notes: str) -> str:
    try:
        cp = progress.read_text(encoding="utf-8", errors="replace")[-30000:]
    except OSError:
        cp = "(no checkpoint was written)"
    return (f"# Session rotation {k} of {MAX_ROTATIONS}\nThe manager ended your previous session at "
            f"{tokens:,} tokens of context and started this fresh one, because every turn re-sends the "
            f"whole conversation. Nothing else carries over. Your checkpoint is below. Do not re-read "
            f"it; run `git status --short` once, then continue from its Next section and do not "
            f"redo what it lists as Done. You are a continuation. If the brief seems out of scope, write "
            f"`status: refused` on its own line in the checkpoint and stop. Do not mark it done.\n\n## Your checkpoint (`{progress}`)\n\n{cp.strip()}\n{notes}\n\n")


def _factor(model: str) -> float:
    m = (model or "").lower()
    for key, f in MODEL_FACTOR:
        if key in m:
            return f
    return 1.0


def stream_cost(path: Path) -> float | None:
    """Estimated list-price dollars for a whole run, from the usage on every assistant event of
    its stream (deduplicated by message id, last event of a message wins, subagents included).
    None when the stream has no usage. An estimate: streamed output counts can lag the final."""
    seen: dict = {}
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return None
    with fh:
        for n, line in enumerate(fh):
            if not line.startswith("{") or '"usage"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            m = ev.get("message") if ev.get("type") == "assistant" else None
            if isinstance(m, dict) and isinstance(m.get("usage"), dict):
                seen[m.get("id") or f"n{n}"] = (m.get("model", ""), m["usage"])
    if not seen:
        return None
    total = 0.0
    for model, u in seen.values():
        total += _factor(model) * (
            (u.get("input_tokens") or 0) * RATE_PER_M["input"] + (u.get("cache_read_input_tokens") or 0) * RATE_PER_M["read"]
            + (u.get("cache_creation_input_tokens") or 0) * RATE_PER_M["write"] + (u.get("output_tokens") or 0) * RATE_PER_M["out"]) / 1e6
    return round(total, 3)


def fallback_cost(rdir: Path, st: dict) -> dict:
    """The cost fields for a run that ends on a path with no result event (reaped, killed, a
    launch that raised). Cut-off runs used to record `cost None` ): now the
    stream's own usage, else 0.0, always flagged cost_estimated. {} when a number is already there."""
    if isinstance(st.get("cost_usd"), (int, float)) and st["cost_usd"] > 0:
        return {}  # a 0 is what a cut-off run used to record, so it is re-estimated
    est = stream_cost(Path(rdir) / "stream.jsonl")
    return {"cost_usd": est if est is not None else 0.0, "cost_estimated": True}


def context_tokens(usage: dict) -> int:
    return int((usage.get("input_tokens") or 0) + (usage.get("cache_read_input_tokens") or 0)
               + (usage.get("cache_creation_input_tokens") or 0))

# An optional standing goal, quoted whole at every launch, so an edit to the file reaches the next
# run without touching this script. Set STEWARD_STRETCH_FILE, or put it at
# $STEWARD_HOME/stretch.md. Missing file, no block.
STRETCH = Path(os.environ.get("STEWARD_STRETCH_FILE") or (ROOT / "stretch.md")).expanduser()


def stretch_block() -> str:
    try:
        text = STRETCH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return (f"\n\n{text}\n\nThis standing goal is in `{STRETCH}`. Re-read it if you are resumed.\n") if text else ""


# An optional house style, quoted whole at every launch like the stretch file. Set
# STEWARD_STYLE_FILE, or put it at $STEWARD_HOME/style.md. Missing file, no block.
STYLE = Path(os.environ.get("STEWARD_STYLE_FILE") or (ROOT / "style.md")).expanduser()


def style_block() -> str:
    try:
        text = STYLE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return ("\n\n# House style\n" + text + "\n") if text else ""


# ------------------------------------------------------------------------------- helpers

def _now() -> dt.datetime:
    return dt.datetime.now(P)


def _iso(d: dt.datetime | None) -> str:
    return d.astimezone(P).isoformat(timespec="seconds") if d else ""


def _parse(s: str) -> dt.datetime | None:
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return (d.replace(tzinfo=P) if d.tzinfo is None else d).astimezone(P)


def run_dir(rid: str) -> Path:
    return RUNS / rid


def _read(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def run_cwd(st: dict) -> str:
    """The session's working directory: the run's `repo` field when it names a directory, else
    the run's recorded `cwd`, else STEWARD_WORKDIR or the directory agentctl was started from."""
    st = st or {}
    for key in ("repo", "cwd"):
        val = st.get(key) or ""
        if val and Path(val).expanduser().is_dir():
            return str(Path(val).expanduser())
    return str(DEFAULT_WORKDIR)


def brief_repo(brief: str) -> str:
    """The path on a `**Repo: <path>**` line of the brief, "" when there is none."""
    m = BRIEF_REPO_RE.search(brief or "")
    return str(Path(m.group(1).strip()).expanduser()) if m else ""


def settings_findings(cwd: str = "") -> list[str]:
    """An optional hook for checking Claude Code settings before a launch. Steward ships none, so
    this returns no findings. Replace it (or monkeypatch it) to log warnings into a run's log."""
    return []


def log_settings_lint(r: "Runner", cwd: str) -> None:
    found = settings_findings(cwd)
    for f in found:
        r.log(f"{_now():%H:%M:%S}  MANAGER: settings lint WARNING: {f}")
    save_state(r.rid, settings_lint=len(found))


def load_state(rid: str) -> dict:
    p = run_dir(rid) / "state.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (ValueError, OSError):
        # A torn read (a writer between the size check and ours) or a run directory that
        # `gc` archived a moment ago. Callers treat {} as "no state", which is honest.
        return {}


def save_state(rid: str, **fields) -> dict:
    """Read-modify-write under a file lock, written through a temp file and os.replace().

    An earlier version was an unlocked load + in-place write called from the stream
    reader thread on every event and from _finish() in the main thread, and from status,
    pause and kill in other processes. Two of five trial runs corrupted the JSON or lost the
    final status. flock serialises threads and processes
    alike (each open() is its own file description); the rename means no reader ever sees
    a half-written file."""
    import fcntl
    touch = fields.pop("_touch", True)   # False: a bookkeeping fill that must not look like activity
    d = run_dir(rid)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "state.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        st = load_state(rid)
        st.update(fields)
        if touch or not st.get("updated"):
            st["updated"] = _iso(_now())
        tmp = d / "state.json.tmp"
        tmp.write_text(json.dumps(st, indent=1, ensure_ascii=False))
        os.replace(tmp, d / "state.json")
    return st


def pid_state(pid) -> str:
    """`alive`, `dead` or `unknown` for one pid, from os.kill(pid, 0) alone.

    Under a sandboxed shell `ps` and `pgrep` can fail and os.kill on a process outside the
    caller's tree raises PermissionError. Reading every OSError as dead once marked four live
    runs orphaned and a supervisor queued second and third attempts on top of each of them. PermissionError means the process
    exists and we may not signal it: alive. Only ProcessLookupError is a dead reading."""
    if pid in (None, "", 0, "0"):
        return "unknown"
    try:
        n = int(pid)
    except (TypeError, ValueError):
        return "unknown"
    if n <= 0:
        return "unknown"
    try:
        os.kill(n, 0)
        return "alive"
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "alive"
    except OSError:
        return "unknown"


def _alive(pid) -> bool:
    return pid_state(pid) == "alive"


def _fresh_file(rdir: Path, minutes: float) -> tuple[str, float] | None:
    """(name, minutes ago) of log.md or progress.md when either was written in the last `minutes`."""
    best = None
    for name in ("log.md", "progress.md"):
        try:
            age = (time.time() - (Path(rdir) / name).stat().st_mtime) / 60
        except OSError:
            continue
        if age <= minutes and (best is None or age < best[1]):
            best = (name, max(age, 0.0))
    return best


def run_liveness(st: dict, rdir: Path | None = None, fresh_min: float | None = None) -> tuple[str, str]:
    """(`alive` | `dead` | `unknown`, why) for a run, from three signals:

      the manager's pid and the Claude session's own pid (`claude_pid`, else `pid`), each read
      with pid_state(); and log.md or progress.md written in the last LIVE_FRESH_MIN minutes.

    Any live signal makes the run alive, including a session that outlived its manager (it can
    still be editing files). Dead needs a positive dead reading of the manager AND of the session
    (or no session pid ever recorded) AND no fresh file. Anything else is unknown, and a caller
    leaves the state as it is: only `dead` may write `orphaned`."""
    rdir = Path(rdir) if rdir else run_dir(st.get("id") or "")
    fresh_min = LIVE_FRESH_MIN if fresh_min is None else fresh_min
    mpid = st.get("manager_pid")
    cpid = st.get("claude_pid") or st.get("pid")
    mgr = pid_state(mpid)
    ses = pid_state(cpid) if cpid else "none"
    if mgr == "alive":
        return "alive", f"manager pid {mpid} alive"
    if ses == "alive":
        return "alive", f"session pid {cpid} alive, manager pid {mpid or '?'} {mgr}"
    fresh = _fresh_file(rdir, fresh_min)
    if fresh:
        return "alive", f"{fresh[0]} written {fresh[1]:.0f} min ago"
    if mgr == "dead" and ses in ("dead", "none"):
        return "dead", f"manager pid {mpid} dead" + (f", session pid {cpid} dead" if cpid else ", no session pid recorded")
    if not mpid and not cpid and st.get("status") == "starting":
        # A launch that never recorded a process: cmd_run writes `starting` before the manager
        # records itself (older runs), and the session pid only once claude has spawned.
        return "dead", "no manager or session pid was ever recorded"
    return "unknown", f"manager pid {mpid or 'none'} {mgr}, session pid {cpid or 'none'} {ses}, no file written in {fresh_min:.0f} min"


# Optional one-run-per-worktree locks (`$STEWARD_HOME/builds/<slug>/worktree.lock`, JSON with an
# `entry` naming the run). A pipeline that hands worktrees to runs can write them; the run a lock
# names releases it when it ends, so the next attempt need not wait for a stale-lock sweep.
BUILD_LOCKS = ROOT / "builds"


def release_stage_locks(rid: str) -> int:
    """Remove every build worktree lock held for run `rid`. Returns how many were removed."""
    n = 0
    for f in BUILD_LOCKS.glob("*/worktree.lock"):
        try:
            if json.loads(f.read_text(encoding="utf-8")).get("entry") == rid:
                f.unlink()
                n += 1
        except (OSError, ValueError):
            continue
    return n


def _running_others(rid: str) -> int:
    """How many other managed runs are running right now. A run whose liveness cannot be read
    counts: it may be spending the window, and an extra slot is the cheaper mistake."""
    n = 0
    for p in RUNS.glob("*/state.json"):
        if p.parent.name == rid:
            continue
        try:
            st = json.loads(p.read_text())
        except ValueError:
            continue
        if st.get("status") == "running" and run_liveness(st, p.parent)[0] != "dead":
            n += 1
    return n


def reap(quiet: bool = True) -> list:
    """Close every run whose manager process is gone, and say so in its state and log.

    A managed run is a manager process watching a claude process. Kill the manager (the
    scheduler's own timeout taking its process group with it, a Terminal window closing, a
    reboot) and the state file says `running` for ever: `status` shows "running (DEAD)", the
    concurrency counter will not count it, and nobody ever learns what happened. This runs at the top of `status` and before any new
    run starts, so a corpse is reported once and then closed.

    Only a positive dead reading from run_liveness() closes a run. A run whose
    liveness is unknown (the pids unreadable, nothing written lately) is left exactly as it is,
    and a session that outlived its manager is alive, not orphaned, and is never killed here.
    """
    closed = []
    for f in sorted(RUNS.glob("*/state.json")):
        try:
            st = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        status = st.get("status")
        if status not in ("running", "starting"):
            continue
        if status == "starting":
            # A resume writes `starting` while the manager pid is still the previous, dead one,
            # so a run is only a corpse once it has sat there past the grace period.
            upd = _parse(st.get("updated", ""))
            if not upd or upd > _now() - dt.timedelta(minutes=STARTING_GRACE_MIN):
                continue
        verdict, why = run_liveness(st, f.parent)
        if verdict != "dead":
            continue
        rid = st.get("id") or f.parent.name
        reason = (f"the manager process ({st.get('manager_pid')}) is gone, so this run was "
                  f"orphaned ({why}). Nothing was recorded about how it ended.") if status == "running" else (
                  f"it was left at starting for over {STARTING_GRACE_MIN} minutes with no live manager ({why}), "
                  f"so the launch never happened.")
        extra = fallback_cost(f.parent, st)
        save_state(rid, status="orphaned", reason=reason, pid=None, **extra)
        release_stage_locks(rid)
        try:
            with (f.parent / "log.md").open("a") as fh:
                fh.write(f"{_now():%H:%M:%S}  MANAGER: reaped, {reason}\n")
        except OSError:
            pass
        closed.append(rid)
        if not quiet:
            print(f"agentctl: {rid} orphaned, {reason}")
    return closed


def _next_model(current: str) -> str:
    """The next model to try when `current` is out of credits, or "" when the ladder is spent.
    An empty `current` means the run asked for no model and got the CLI default, so start at
    the top of the ladder."""
    cur = (current or "").strip().lower()
    for m in MODEL_LADDER:
        if m.lower() == cur:
            continue
        if cur and MODEL_LADDER.index(m) <= _ladder_index(cur):
            continue
        return m
    return ""


def _ladder_index(model: str) -> int:
    """Where `model` sits on the ladder, matched by family, so short aliases (`opus`, `sonnet`,
    `haiku`) and an undated full name find their rung. With exact names only, every alias sat at
    -1, and -1 restarts the ladder at the top: an Opus run out of credits was retried on Opus and
    a haiku run escalated to Opus (agentctl_selftest.py). A model not on the ladder at all is -1,
    which rightly starts at the top."""
    cur = (model or "").strip().lower()
    for i, m in enumerate(MODEL_LADDER):
        if m.lower() == cur:
            return i
    for i, m in enumerate(MODEL_LADDER):
        family = m.split("-")[1]                     # claude-opus-5 -> opus
        if re.search(r"(?<![a-z])%s(?![a-z])" % family, cur):
            return i
    return -1


def _usage_reset() -> "dt.datetime | None":
    """When the five-hour window next resets, from the status line snapshot kept in
    `$STEWARD_HOME/health/usage.json` (`data.rate_limits.five_hour.resets_at`, a unix time). A limit
    message with no time in it (the out-of-credits one) has nowhere else to look."""
    try:
        u = json.loads(USAGE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    limits = ((u.get("data") or {}).get("rate_limits") or {})
    best = None
    for key in ("five_hour", "seven_day"):
        raw = (limits.get(key) or {}).get("resets_at")
        if not raw:
            continue
        try:
            when = dt.datetime.fromtimestamp(float(raw)).astimezone()
        except (TypeError, ValueError, OSError):
            continue
        if when > _now() and (best is None or when < best):
            best = when
    return best


def _clean_env() -> dict:
    """The environment for a child claude: launchd gives none of these, but an interactive
    session launching agentctl by hand would otherwise make the child think it is nested."""
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("CLAUDE") or k in ("CLAUDE_PID",):
            env.pop(k, None)
    # A child bills the subscription, never an API key, unless STEWARD_KEEP_API_KEY=1 says so.
    if not os.environ.get("STEWARD_KEEP_API_KEY"):
        env.pop("ANTHROPIC_API_KEY", None)
    env.pop("STEWARD_RUN_ID", None)
    env.setdefault("TERM", "dumb")
    env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + str(Path.home() / ".local/bin") + ":/usr/bin:/bin:/usr/sbin:/sbin"
    return env


# --------------------------------------------------------------------------------- usage

def _dig(d, *paths):
    """First value found at any of several dotted paths."""
    for path in paths:
        cur = d
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur is not None:
            return cur
    return None


def usage_now() -> dict:
    """{pct, resets_at, age_s, source, fresh}. pct is the 5-hour window percentage or None.
    Tolerant to the exact field names the status line provides: it looks in several places."""
    if not USAGE.exists():
        return {"pct": None, "resets_at": None, "age_s": None, "source": "none", "fresh": False}
    try:
        raw = json.loads(USAGE.read_text())
    except ValueError:
        return {"pct": None, "resets_at": None, "age_s": None, "source": "corrupt", "fresh": False}
    written = _parse(raw.get("_written", ""))
    age = (_now() - written).total_seconds() if written else None
    data = raw.get("data", raw)
    pct = _dig(data, "rate_limits.five_hour.used_percentage", "rate_limits.five_hour.utilization",
               "rateLimits.fiveHour.usedPercentage", "five_hour.used_percentage", "pct", "used_percentage")
    resets = _dig(data, "rate_limits.five_hour.resets_at", "rateLimits.fiveHour.resetsAt",
                  "five_hour.resets_at", "resets_at")
    if isinstance(resets, (int, float)):
        resets_dt = dt.datetime.fromtimestamp(resets if resets < 1e12 else resets / 1000, P)
    else:
        resets_dt = _parse(str(resets)) if resets else None
    try:
        pct = float(pct) if pct is not None else None
        if pct is not None and pct <= 1.0 and "utilization" in json.dumps(data):
            pct *= 100
    except (TypeError, ValueError):
        pct = None
    week = _dig(data, "rate_limits.seven_day.used_percentage")
    week_reset = _dig(data, "rate_limits.seven_day.resets_at")
    try:
        week = float(week) if week is not None else None
    except (TypeError, ValueError):
        week = None
    week_reset_dt = (dt.datetime.fromtimestamp(week_reset, P) if isinstance(week_reset, (int, float))
                     else (_parse(str(week_reset)) if week_reset else None))
    return {"pct": pct, "resets_at": resets_dt, "age_s": age, "source": raw.get("_source", "statusline"),
            "fresh": age is not None and age < USAGE_STALE_S and pct is not None,
            "week_pct": week, "week_resets_at": week_reset_dt}


def set_usage(pct: float, resets: str | None, source: str = "manual") -> None:
    USAGE.parent.mkdir(parents=True, exist_ok=True)
    USAGE.write_text(json.dumps({"_written": _iso(_now()), "_source": source,
                                 "data": {"rate_limits": {"five_hour": {
                                     "used_percentage": pct,
                                     "resets_at": _iso(_parse(resets)) if resets else None}}}}, indent=1))


def _notify(title: str, text: str) -> None:
    """A desktop notification on macOS (osascript). Silent anywhere else."""
    if not Path("/usr/bin/osascript").exists():
        return
    try:
        subprocess.run(["/usr/bin/osascript", "-e",
                        f'display notification "{text.replace(chr(34), chr(39))}" with title "Steward: {title.replace(chr(34), chr(39))}"'],
                       capture_output=True, timeout=10)
    except Exception:  # noqa: BLE001
        pass


def _push(text: str, note: str, subject: str) -> None:
    """The outcomes that need a person (failed, timed out, limited, out of credits). Runs the
    command in STEWARD_NOTIFY_CMD, if set, with three arguments: text, note, subject (a stable key
    you can use to de-duplicate). Point it at anything: a phone push, a mail, a chat webhook.
    Every error is caught here, and the command is given at most 40 seconds."""
    cmd = os.environ.get("STEWARD_NOTIFY_CMD", "").strip()
    if not cmd:
        return
    try:
        import shlex
        subprocess.run(shlex.split(cmd) + [text, note, subject], capture_output=True, timeout=40)
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------------------ logging

def _short(text, n=220) -> str:
    text = re.sub(r"\s+", " ", str(text)).strip()
    if len(text) <= n:
        return text
    cut = text[: n - 1]
    space = cut.rfind(" ")
    return (cut[:space] if space > n // 2 else cut).rstrip() + "…"   # a word boundary when there is one


def render_event(ev: dict) -> list[str]:
    """One or more human lines for a stream-json event. Empty for noise."""
    ts = _now().strftime("%H:%M:%S")
    t = ev.get("type")
    if t == "system":
        sub = ev.get("subtype")
        if sub == "init":
            return [f"{ts}  session {ev.get('session_id')} started, model {ev.get('model', '?')}, cwd {ev.get('cwd')}"]
        return []
    if t == "assistant":
        out = []
        for block in ev.get("message", {}).get("content", []):
            bt = block.get("type")
            if bt == "text" and block.get("text", "").strip():
                out.append(f"{ts}  says: {_short(block['text'], 400)}")
            elif bt == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input", {})
                key = inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("prompt") \
                    or inp.get("description") or json.dumps(inp)[:200]
                out.append(f"{ts}  tool {name}: {_short(key, 260)}")
        return out
    if t == "user":
        out = []
        for block in ev.get("message", {}).get("content", []) if isinstance(ev.get("message", {}).get("content"), list) else []:
            if block.get("type") == "tool_result":
                content = block.get("content")
                if isinstance(content, list):
                    content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                flag = " ERROR" if block.get("is_error") else ""
                out.append(f"{ts}    result{flag}: {_short(content, 200)}")
        return out
    if t == "result":
        cost = ev.get("total_cost_usd")
        return [f"{ts}  END: {ev.get('subtype', '')} turns={ev.get('num_turns', '?')} "
                f"cost=${cost:.3f} " if isinstance(cost, (int, float)) else f"{ts}  END: {ev.get('subtype', '')} "
                + f"is_error={ev.get('is_error')} reason={ev.get('terminal_reason', '')} "
                + f"duration={round((ev.get('duration_ms') or 0) / 1000)}s"]
    return []


def render_all(rid: str) -> str:
    lines = [f"# Build log: {rid}", ""]
    for raw in (run_dir(rid) / "stream.jsonl").read_text(errors="replace").splitlines():
        if raw.startswith("#"):
            lines.append(raw)
            continue
        try:
            lines.extend(render_event(json.loads(raw)))
        except ValueError:
            continue
    text = "\n".join(lines) + "\n"
    (run_dir(rid) / "log.md").write_text(text)
    return text


# -------------------------------------------------------------------------------- runner

class Runner:
    def __init__(self, rid: str, threshold: float, max_minutes: int):
        self.rid = rid
        self.dir = run_dir(rid)
        self.threshold = threshold
        self.max_s = max_minutes * 60
        self.proc: subprocess.Popen | None = None
        self.outcome: str | None = None
        self.reason = ""
        self.resume_at: dt.datetime | None = None
        self.limit_text = ""
        self.network_text = ""
        self.credits_text = ""
        self.billing_text = ""
        self.progress_at_start: str | None = None   # progress.md when this manager first launched
        self.no_crash = False                       # a network retry writes no crash.md
        self.last_result: dict = {}
        self.lock = threading.Lock()
        self.rotate_pending = False     # context passed ROTATE_AT_TOKENS mid-tool-call
        self.rotations = 0
        self.cost_prior = 0.0           # result cost of sessions this manager already rotated out
        self.last_ctx = 0
        self.argv_for = None            # (prompt, session_id) -> argv, set by cmd_run / cmd_resume
        self.resume_argv_for = None     # (message, session_id, model) -> argv, for the nudge
        self.brief_text = ""
        self.nudged = False             # one nudge per run: carried in state.json across launches
        self.nudge_pending = False      # no checkpoint write for NUDGE_AFTER_S, waiting for a boundary
        self.launched_wall = 0.0        # time.time() of this launch, the nudge clock's floor

    # --- logging
    def log(self, line: str) -> None:
        with self.lock:
            with (self.dir / "log.md").open("a") as fh:
                fh.write(line.rstrip("\n") + "\n")

    def _consume(self, stream) -> None:
        with (self.dir / "stream.jsonl").open("a") as raw:
            for line in stream:
                raw.write(line)
                raw.flush()
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    self.log(f"{_now():%H:%M:%S}  (unparsed) {_short(line, 200)}")
                    continue
                for out in render_event(ev):
                    self.log(out)
                if ev.get("type") == "result":
                    self.last_result = ev
                    res = str(ev.get("result", ""))
                    if ev.get("is_error") and LIMIT_RE.search(res):
                        self.limit_text = res
                    if CREDITS_RE.search(res):
                        self.credits_text = res
                    if ev.get("is_error") and BILLING_RE.search(res) and not LIMIT_RE.search(res):
                        self.billing_text = res
                    if NETWORK_RE.search(res):
                        self.network_text = res
                # A genuine limit message arrives as a SHORT assistant text block. Tool results
                # are never inspected: a session reading a file that quoted the limit message
                # was once misclassified as limited.
                self._watch_context(ev)
                if ev.get("type") == "assistant":
                    for block in ev.get("message", {}).get("content", []):
                        if block.get("type") == "text" and len(block.get("text", "")) < 300:
                            if LIMIT_RE.search(block["text"]):
                                self.limit_text = block["text"]
                            if CREDITS_RE.search(block["text"]):
                                self.credits_text = block["text"]
                            if NETWORK_RE.search(block["text"]):
                                self.network_text = block["text"]
                save_state(self.rid, last_event=_iso(_now()))

    def _watch_context(self, ev: dict) -> None:
        """Arm a rotation when a main-thread turn passes ROTATE_AT_TOKENS while the agent is
        still calling tools, and fire it at the next main-thread tool result, so no call is cut off."""
        if (self.nudge_pending and not ev.get("parent_tool_use_id") and not self.outcome
                and ev.get("type") == "user"):
            self.outcome, self.reason = "nudging", "no checkpoint write"
            threading.Thread(target=self._stop_child, args=("nudging the session",), daemon=True).start()
            return
        if ev.get("parent_tool_use_id") or self.outcome or self.rotations >= MAX_ROTATIONS:
            return
        t = ev.get("type")
        if t == "assistant":
            m = ev.get("message") or {}
            if isinstance(m.get("usage"), dict):
                self.last_ctx = context_tokens(m["usage"])
            if self.last_ctx >= ROTATE_AT_TOKENS and any(
                    b.get("type") == "tool_use" for b in m.get("content", []) if isinstance(b, dict)):
                self.rotate_pending = True
        elif t == "user" and self.rotate_pending:
            self.outcome, self.reason = "rotating", f"context {self.last_ctx:,} tokens"
            threading.Thread(target=self._stop_child, args=("rotating the session",), daemon=True).start()

    def _bank_cost(self) -> None:
        res = self.last_result.get("total_cost_usd")
        est = None if isinstance(res, (int, float)) else stream_cost(self.dir / "stream.jsonl")
        # a stopped session has no result event, so its cost is the stream's, less what earlier
        # rotations already banked
        self.cost_prior = (est if est is not None else self.cost_prior + (res or 0))

    def stalled_for(self) -> float:
        """Seconds since the checkpoint was last written, counted from this launch at the earliest."""
        try:
            last = max((self.dir / "progress.md").stat().st_mtime, self.launched_wall)
        except OSError:
            last = self.launched_wall
        return time.time() - last if last else 0.0

    def _check_nudge(self) -> None:
        if (NUDGE_AFTER_S > 0 and not self.nudged and not self.nudge_pending and self.resume_argv_for
                and self.stalled_for() > NUDGE_AFTER_S):
            self.nudge_pending = True
            self.log(f"{_now():%H:%M:%S}  MANAGER: no checkpoint write for {NUDGE_AFTER_S // 60} min, "
                     f"nudge at the next tool result")

    def _nudge(self, rc: int) -> int:
        """Same session, resumed in place with one message asking for the checkpoint. Once per run."""
        st = load_state(self.rid)
        self._bank_cost()
        mins = int(self.stalled_for() // 60)
        self.nudged, self.nudge_pending = True, False
        self.outcome, self.reason = None, ""
        self.last_result, self.last_ctx = {}, 0
        save_state(self.rid, nudged=_iso(_now()), cost_usd=self.cost_prior, cost_estimated=True)
        self.log(f"{_now():%H:%M:%S}  MANAGER: nudge sent, checkpoint unwritten for {mins} min")
        msg = NUDGE_MSG.format(progress=self.dir / "progress.md", mins=mins)
        return self.launch(self.resume_argv_for(msg, st.get("session_id"), st.get("model", "")), "nudge")

    def _rotate(self, rc: int) -> int:
        """Fresh session, same run: the brief, the checkpoint and the operator's notes, and nothing else."""
        st = load_state(self.rid)
        self._bank_cost()
        self.rotations += 1
        sid, tokens = str(uuid.uuid4()), self.last_ctx
        self.log(f"{_now():%H:%M:%S}  MANAGER: rotation {self.rotations} of {MAX_ROTATIONS}: context reached "
                 f"{tokens:,} tokens, fresh session {sid[:8]} seeded with the brief, checkpoint and notes")
        prompt = build_prompt(self.brief_text, self.dir / "progress.md",
                              rotation_preamble(self.dir / "progress.md", tokens, self.rotations, resume_notes(self.dir)))
        save_state(self.rid, session_id=sid, rotations=int(st.get("rotations", 0)) + 1, cost_usd=self.cost_prior,
                   cost_estimated=True)
        self.outcome, self.reason, self.rotate_pending = None, "", False
        self.last_result, self.last_ctx = {}, 0
        return self.launch(self.argv_for(prompt, sid, load_state(self.rid).get("model", "")), f"rotation #{self.rotations}")

    # --- stopping
    def _stop_child(self, why: str) -> None:
        if not self.proc or self.proc.poll() is not None:
            return
        self.log(f"{_now():%H:%M:%S}  MANAGER: {why}, sending SIGTERM")
        try:
            self.proc.send_signal(signal.SIGTERM)
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.log(f"{_now():%H:%M:%S}  MANAGER: still alive after 15 s, SIGKILL")
            self.proc.kill()
        except OSError:
            pass

    def _watch(self, proc) -> None:
        started = time.monotonic()
        while proc.poll() is None:
            time.sleep(WATCH_EVERY_S)
            if proc.poll() is not None:
                break
            if self.max_s > 0 and not NO_TIME_LIMIT_FLAG.exists() and time.monotonic() - started > self.max_s:
                self.outcome, self.reason = "timed-out", f"exceeded {self.max_s // 60} minutes"
                self._stop_child(self.reason)
                break
            u = usage_now()
            if u["fresh"] and u.get("week_pct") is not None and u["week_pct"] >= WEEKLY_WINDDOWN:
                self.outcome = "dormant"
                self.reason = f"weekly usage at {u['week_pct']:.0f}% (weekly threshold {WEEKLY_WINDDOWN:.0f}%)"
                self.resume_at = (u.get("week_resets_at") or (_now() + dt.timedelta(hours=6))) + dt.timedelta(minutes=5)
                self._stop_child(self.reason)
                break
            # Five parallel Opus agents took the window from 53% to 100% in ten minutes in
            # one test, faster than a 60 s sensor and a 20 s poll could catch at 90%. The
            # effective threshold therefore drops 4 points for every OTHER managed run that
            # is running, never below 60.
            others = _running_others(self.rid)
            effective = max(60.0, self.threshold - 4.0 * others)
            # $STEWARD_HOME/health/threshold-override holds one flat number that replaces the
            # per-run cut for every run, running or resumed. Delete the file to go back.
            try:
                effective = float((HEALTH / "threshold-override").read_text().strip())
            except (OSError, ValueError):
                pass
            if u["fresh"] and u["pct"] is not None and u["pct"] >= effective:
                self.outcome = "dormant"
                self.reason = (f"usage at {u['pct']:.0f}% (threshold {effective:.0f}%"
                               + (f", {self.threshold:.0f}% less {others} concurrent runs" if others else "") + ")")
                self.resume_at = (u["resets_at"] or (_now() + dt.timedelta(hours=1))) + dt.timedelta(minutes=2)
                self._stop_child(self.reason)
                break
            self._check_nudge()
            pause_flag = self.dir / "PAUSE"
            if pause_flag.exists():
                self.outcome, self.reason = "dormant", "paused by agentctl pause"
                try:
                    self.resume_at = _parse(pause_flag.read_text().strip()) or (_now() + dt.timedelta(hours=1))
                except OSError:
                    self.resume_at = _now() + dt.timedelta(hours=1)
                pause_flag.unlink(missing_ok=True)
                self._stop_child(self.reason)
                break
            if (self.dir / "KILL").exists():
                self.outcome, self.reason = "killed", "killed by agentctl kill"
                (self.dir / "KILL").unlink(missing_ok=True)
                self._stop_child(self.reason)
                break

    # --- the launch
    def launch(self, argv: list[str], label: str) -> int:
        self.dir.mkdir(parents=True, exist_ok=True)
        with (self.dir / "stream.jsonl").open("a") as raw:
            raw.write(f"# {label} {_iso(_now())} {' '.join(argv[:3])} ...\n")
        self.log(f"\n## {label} at {_now():%Y-%m-%d %H:%M:%S}")
        stderr = (self.dir / "stderr.log").open("a")
        env = {**_clean_env(), "STEWARD_RUN_ID": self.rid, **getattr(self, "env_extra", {})}
        if self.progress_at_start is None:
            self.progress_at_start = _read(self.dir / "progress.md")
        self.launched_wall = time.time()
        self.proc = subprocess.Popen(argv, cwd=run_cwd(load_state(self.rid)), env=env, stdout=subprocess.PIPE,
                                     stderr=stderr, text=True, bufsize=1)
        # claude_pid is the session's own pid, kept after the run ends (pid is cleared), so a status
        # check can see a session that outlived its manager. `claude` is a native
        # binary, so Popen's pid is the session itself.
        save_state(self.rid, status="running", pid=self.proc.pid, claude_pid=self.proc.pid, manager_pid=os.getpid(),
                   started_this_launch=_iso(_now()))
        reader = threading.Thread(target=self._consume, args=(self.proc.stdout,), daemon=True)
        reader.start()
        watcher = threading.Thread(target=self._watch, args=(self.proc,), daemon=True)
        watcher.start()
        rc = self.proc.wait()
        reader.join(timeout=10)
        stderr.close()
        if self.outcome == "rotating" and self.argv_for:
            return self._rotate(rc)
        if self.outcome == "nudging" and self.last_result:
            self.outcome = None         # the session finished on its own before the stop landed
        if self.outcome == "nudging" and self.resume_argv_for:
            return self._nudge(rc)
        if self.outcome == "nudging":
            self.outcome = None
        return self._finish(rc)

    def _finish(self, rc: int) -> int:
        st = load_state(self.rid)
        try:
            err_tail = (self.dir / "stderr.log").read_text(errors="replace")[-2000:]
        except OSError:
            err_tail = ""
        res = str(self.last_result.get("result") or "")
        if not self.billing_text and self.last_result.get("is_error") and BILLING_RE.search(res) and not LIMIT_RE.search(res):
            self.billing_text = res
        if not self.billing_text and (rc != 0 or self.last_result.get("is_error")) and not self.last_result.get("result") \
                and BILLING_RE.search(err_tail) and not LIMIT_RE.search(err_tail):
            self.billing_text = _short(err_tail.strip().splitlines()[-1], 200) if err_tail.strip() else ""
        progress_now = _read(self.dir / "progress.md")
        if self.outcome is None:
            nxt = _next_model(st.get("model", "")) if self.credits_text and not self.billing_text else None
            if self.billing_text or (self.credits_text and not nxt):
                # Credits or billing, and no model left to switch to: waiting and relaunching cannot
                # fix it, so stop here, no retry, one notification (once read as api_error, 6 and 7
                # relaunches). The operator tops up or waits, then resumes the run by hand.
                self.outcome = "out-of-credits"
                self.reason = f"out of credits or billing: {_short(self.billing_text or self.credits_text, 120)}"
            elif REFUSED_RE.search(progress_now) and not REFUSED_RE.search(self.progress_at_start or ""):
                # A continuation found the brief out of scope and said so. Never done: the brief is
                # re-queued once as a fresh run with a note, then it waits for the operator.
                self.outcome, self.reason = "refused", "the session wrote status: refused in its checkpoint"
            elif nxt:
                # One model is out of credits while the others answer. Switching beats waiting
                # for a window that is not the problem: the state carries the new model and the
                # resume in a minute picks it up. Recorded so the report says what ran.
                self.outcome = "interrupted"
                self.reason = f"{st.get('model') or 'the default model'} is out of usage credits, retrying on {nxt}"
                self.resume_at = _now() + dt.timedelta(minutes=1)
                tried = list(st.get("models_tried") or [])
                if st.get("model"):
                    tried.append(st["model"])
                save_state(self.rid, model=nxt, models_tried=tried)
                self.log(f"{_now():%H:%M:%S}  MANAGER: model switch {st.get('model') or '(default)'} -> {nxt}")
            elif self.limit_text or (self.last_result.get("terminal_reason") in ("rate_limit", "rate_limited")):
                self.outcome = "limited"
                import stewardq as queue_add  # noqa: E402
                reset = (queue_add.parse_reset(self.limit_text or "")
                         or _usage_reset() or (_now() + dt.timedelta(hours=1)))
                self.resume_at = reset + dt.timedelta(minutes=5)
                self.reason = f"hit the usage limit, resets {reset:%H:%M}"
            elif rc == 0 and not self.last_result.get("is_error"):
                if self.progress_at_start is not None and progress_now == self.progress_at_start:
                    # Finished without touching its checkpoint: nothing a later reader can trust was
                    # done. Not green (briefs were once marked done that never ran).
                    self.outcome, self.reason = "no-op", "finished with progress.md unchanged"
                else:
                    self.outcome, self.reason = "done", "finished"
            elif (self.network_text or NETWORK_RE.search(err_tail)) \
                    and int(st.get("network_retries", 0)) < MAX_NETWORK_RETRIES:
                # The Mac slept or lost network mid-run: the session
                # is intact, so come back in five minutes. Its own counter, not the resume cap, and
                # no crash report: this is routine, not a crash.
                n = int(st.get("network_retries", 0)) + 1
                save_state(self.rid, network_retries=n)
                self.outcome, self.no_crash = "interrupted", True
                self.reason = (f"API unreachable ({_short(self.network_text or NETWORK_RE.search(err_tail).group(0), 80)}), "
                               f"retry {n} of {MAX_NETWORK_RETRIES} in {NETWORK_RETRY_MIN} min")
                self.resume_at = _now() + dt.timedelta(minutes=NETWORK_RETRY_MIN)
            elif self.last_result.get("terminal_reason") == "api_error" and int(st.get("resumes", 0)) < MAX_AUTO_RESUMES:
                self.outcome = "interrupted"
                self.reason = "api_error, resume in 10 min"
                self.resume_at = _now() + dt.timedelta(minutes=10)
            else:
                self.outcome = "failed"
                self.reason = f"exit {rc}, terminal_reason={self.last_result.get('terminal_reason', '?')}"
        self.log(f"{_now():%H:%M:%S}  MANAGER: outcome {self.outcome}: {self.reason}")
        print(f"agentctl: {self.rid} {self.outcome}: {self.reason}", flush=True)
        if AUTH_RE.search(f"{self.last_result.get('result', '')} {err_tail}") and self.outcome != "done":
            _push("Headless claude is failing to authenticate (401)",
                  f"{self.rid}: {self.reason}. Run `claude` in a terminal and /login, and check that "
                  "ANTHROPIC_API_KEY is not set in the job environment.", f"steward-claude-401-{_now():%Y-%m-%d}")
        if self.outcome != "done":
            _notify(f"{self.rid}: {self.outcome}", self.reason[:150])
        # The notify command, for the outcomes that need a person. Not dormant or interrupted: those
        # resume by themselves and would ring the phone for routine.
        if self.outcome == "out-of-credits":
            # One notice a day, whatever number of runs hit it: the subject is the notify command's de-duplication key.
            _push("Agents are out of credits",
                  f"{self.rid} stopped: {self.reason}. No retry is queued. Top up or wait, then "
                  f"resume {self.rid} by hand.", f"steward-out-of-credits-{_now():%Y-%m-%d}")
        if self.outcome in ("failed", "timed-out", "limited"):
            _push(f"Agent {self.rid} {self.outcome}: {self.reason}",
                  f"Read {self.dir}/crash.md, or run agentctl.py report {self.rid}."
                  + (" A resume is queued for the reset." if self.outcome == "limited" else ""),
                  f"agent-{self.rid}-{self.outcome}")
        save_state(self.rid, status=self.outcome, reason=self.reason, exit_code=rc, pid=None,
                   resume_at=_iso(self.resume_at), ended=_iso(_now()), **self._cost_fields(st))
        if self.outcome not in ("dormant", "limited", "interrupted"):
            # A wound-down run comes back as the same attempt, so it keeps the worktree.
            release_stage_locks(self.rid)
        if self.outcome in ("dormant", "limited", "interrupted"):
            self._queue_resume()
        if self.outcome == "refused":
            self._requeue_refused(st)
        if self.outcome in ("failed", "timed-out", "killed", "limited", "interrupted", "out-of-credits") and not self.no_crash:
            self._crash_report(rc)
        return EXIT[self.outcome]

    def _cost_fields(self, st: dict) -> dict:
        """cost_usd for the state. The result event's figure when there is one; otherwise, for a run
        that ended without a result (timed out, killed, cut off), the stream's own usage, flagged
        cost_estimated (three such runs were once recorded at 0)."""
        res = self.last_result.get("total_cost_usd")
        if isinstance(res, (int, float)):
            return {"cost_usd": self.cost_prior + res, **({"cost_estimated": True} if self.cost_prior else {})}
        est = stream_cost(self.dir / "stream.jsonl")
        if est is not None:
            return {"cost_usd": est, "cost_estimated": True}
        prior = st.get("cost_usd")
        return {"cost_usd": prior if isinstance(prior, (int, float)) else 0.0, "cost_estimated": True}

    def _requeue_refused(self, st: dict) -> None:
        """Queue the brief once more as a fresh run, with the refusal named, then never again."""
        if st.get("refusal_requeued"):
            self.log(f"{_now():%H:%M:%S}  MANAGER: refused again after one re-queue; left for the operator")
            _push(f"Agent {self.rid} refused its brief twice",
                  f"Read {self.dir}/progress.md. Nothing more is queued.", f"agent-{self.rid}-refused")
            return
        try:
            import stewardq as queue_add  # noqa: E402
            brief = self.brief_text or _read(self.dir / "brief.md")
            note = (f"This brief was queued by the operator. A continuation of run {self.rid} wrote `status: refused` "
                    f"as out of scope. Read `{self.dir / 'progress.md'}` for why, then do the brief or, if it "
                    f"truly cannot be done, say exactly what blocks it in your report.\n\n")
            p = queue_add.add_brief(f"{self.rid} (re-queued after refusal)", note + brief,
                                    _now() + dt.timedelta(minutes=5), model=st.get("model", ""),
                                    reason=f"re-queued once after {self.rid} refused")
            save_state(self.rid, refusal_requeued=p.name)
            self.log(f"{_now():%H:%M:%S}  MANAGER: refused, re-queued once as {p.name}")
        except Exception as e:  # noqa: BLE001
            self.log(f"{_now():%H:%M:%S}  MANAGER: could not re-queue the refused brief ({type(e).__name__}: {e})")

    def _queue_resume(self) -> None:
        try:
            import stewardq as queue_add  # noqa: E402
            p = queue_add.add_resume(self.rid, self.resume_at or (_now() + dt.timedelta(hours=1)), self.reason)
            self.log(f"{_now():%H:%M:%S}  MANAGER: resume queued for {self.resume_at:%a %d %b %H:%M}: {p.name if p else 'already queued'}")
        except Exception as e:  # noqa: BLE001
            self.log(f"{_now():%H:%M:%S}  MANAGER: could not queue resume ({type(e).__name__}: {e})")

    def _crash_report(self, rc: int) -> None:
        st = load_state(self.rid)
        log_tail = (self.dir / "log.md").read_text(errors="replace").splitlines()[-40:]
        err_tail = (self.dir / "stderr.log").read_text(errors="replace").splitlines()[-20:] if (self.dir / "stderr.log").exists() else []
        progress = (self.dir / "progress.md").read_text(errors="replace") if (self.dir / "progress.md").exists() else "(no checkpoint written)"
        text = "\n".join([
            f"# Crash report: {self.rid}", "",
            f"- outcome: {self.outcome}", f"- reason: {self.reason}", f"- exit code: {rc}",
            f"- terminal_reason: {self.last_result.get('terminal_reason', '?')}",
            f"- session: {st.get('session_id')}", f"- resumes so far: {st.get('resumes', 0)}",
            f"- cost so far: {st.get('cost_usd')}", f"- when: {_iso(_now())}", "",
            "## Resume", "```",
            f'python3 "{Path(__file__).resolve()}" resume {self.rid}', "```", "",
            "## Checkpoint (progress.md)", progress, "",
            "## Last 40 log lines", "```", *log_tail, "```", "",
            "## stderr tail", "```", *err_tail, "```", "",
        ])
        (self.dir / "crash.md").write_text(text)


# What one launch may spend before the CLI stops it. `--max-budget-usd` is a real flag on
# recent Claude Code versions and works under --print, which -p is. (`--max-turns` does NOT exist in this
# version; do not reach for it.) The number is a runaway guard, not a target: an ordinary
# run costs well under that, so $25 stops a loop without touching an ordinary run. A brief that genuinely needs more passes
# --max-budget-usd, and STEWARD_BUDGET_USD changes the default.
DEFAULT_BUDGET_USD = env_float("STEWARD_BUDGET_USD", 25.0)


# --- permissions. By default a headless run uses bypassPermissions, because nobody is there to
# answer a prompt. STEWARD_PERMISSION_MODE overrides the mode (dontAsk, acceptEdits, ...), and
# STEWARD_SETTINGS names a Claude Code settings file (deny rules, hooks) for every managed run.
AUTH_RE = re.compile(r"authentication_error|\b401\b.{0,40}(unauthori[sz]ed|auth)|invalid (x-)?api[- ]key|"
                     r"OAuth token (has )?expired|please run /login", re.I)
_VERSION: list[str] = []


def permission_flags() -> list[str]:
    mode = os.environ.get("STEWARD_PERMISSION_MODE") or "bypassPermissions"
    flags = ["--permission-mode", mode]
    settings = os.environ.get("STEWARD_SETTINGS")
    if settings:
        flags += ["--settings", str(Path(settings).expanduser())]
    return flags


def claude_version() -> str:
    if not _VERSION:
        try:
            out = subprocess.run([str(CLAUDE), "--version"], capture_output=True, text=True,
                                 timeout=30, env=_clean_env()).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            out = ""
        _VERSION.append(out.split()[0] if out else "")
    return _VERSION[0]


# Fixed overhead: a run with many skills installed can start at tens of thousands of tokens.
# --disable-slash-commands drops the skill entries (headless builds rarely invoke one; set
# STEWARD_KEEP_SKILLS=1 for a run that must), and --exclude-dynamic-system-prompt-sections moves cwd,
# env and git status out of the system prompt so the prefix is identical across runs. Both flags
# need a recent Claude Code; set STEWARD_LEAN_FLAGS=0 if yours rejects them.
LEAN_FLAGS = ([] if os.environ.get("STEWARD_LEAN_FLAGS") == "0" else
              (["--exclude-dynamic-system-prompt-sections"]
               + ([] if os.environ.get("STEWARD_KEEP_SKILLS") else ["--disable-slash-commands"])))


def _claude_args(prompt: str, session_id: str | None, resume: str | None, model: str,
                 extra: list[str], budget: float = 0.0, perm: list[str] | None = None) -> list[str]:
    argv = [str(CLAUDE), "-p", prompt, "--output-format", "stream-json", "--verbose"]
    argv += perm or permission_flags()
    if resume:
        argv += ["--resume", resume]
    elif session_id:
        argv += ["--session-id", session_id]
    if model:
        argv += ["--model", model]
    if budget and budget > 0:
        argv += ["--max-budget-usd", f"{budget:g}"]
    return argv + LEAN_FLAGS + extra


CAPACITY = HEALTH / "capacity.json"


def capacity_slots(default: int = 6) -> int:
    """The one capacity number: `slots` in $STEWARD_HOME/health/capacity.json, so the manager and
    the queue never disagree. STEWARD_MAX_CONCURRENT still wins. A missing or bad file gives 6."""
    env = os.environ.get("STEWARD_MAX_CONCURRENT")
    if env:
        return int(env)
    try:
        n = int(json.loads(CAPACITY.read_text()).get("slots"))
        return n if n > 0 else default
    except (OSError, ValueError, TypeError, AttributeError):
        return default


MAX_CONCURRENT = capacity_slots()
# Managed runs at once, unless --force. Five parallel builders once took the usage window from
# 53% to 100% in ten minutes; the wind-down threshold only slows runs that already exist. The
# wind-down at 90% of the five-hour window and 95% of the weekly one is the real brake, and this
# number decides how fast the window is spent.


def weekly_ceiling_hit(u: dict, model: str) -> bool:
    """True when the weekly no-new-runs ceiling should refuse a run on `model`.

    usage.json is written only by an interactive session's status line, so an old reading of a
    window that has since reset must not refuse runs indefinitely. A reading counts when
    it is fresh or at most WEEKLY_STALE_S old, and its window has not already reset. The ladder's
    own haiku name, claude-haiku-4-5-20251001, is exempt like the short ones."""
    pct = u.get("week_pct")
    if pct is None or pct < WEEKLY_NO_NEW_RUNS:
        return False
    age = u.get("age_s")
    if not (u.get("fresh") or (age is not None and age <= WEEKLY_STALE_S)):
        return False
    when = u.get("week_resets_at")
    if isinstance(when, dt.datetime) and when <= _now():
        return False
    return not (model or "").lower().startswith(("haiku", "claude-haiku"))


def cmd_run(a) -> int:
    rid = a.id
    d = run_dir(rid)
    reap()                     # a corpse must not hold a slot in the concurrency count
    if d.exists() and load_state(rid).get("status") in ("running", "starting"):
        verdict, why = run_liveness(load_state(rid), d)
        if verdict != "dead":
            print(f"agentctl: {rid} is already running or its liveness cannot be read ({verdict}: {why}); "
                  f"not starting a second session on it")
            return 1
    others = _running_others(rid)
    if others >= MAX_CONCURRENT and not a.force:
        print(f"agentctl: {others} managed runs are already running (cap {MAX_CONCURRENT}). "
              f"Queue this one (stewardq.py add --at +30m ...) or pass --force.", file=sys.stderr)
        return 79
    u = usage_now()
    if weekly_ceiling_hit(u, a.model) and not a.force:
        when = u.get("week_resets_at")
        print(f"agentctl: weekly usage is {u['week_pct']:.0f}%, above {WEEKLY_NO_NEW_RUNS:.0f}%; not starting {rid}. "
              f"Weekly reset {when:%a %d %b %H:%M}. Queue it for then, or --force." if when else
              f"agentctl: weekly usage is {u['week_pct']:.0f}%; not starting {rid}. Queue it for the weekly reset, or --force.")
        return 79
    d.mkdir(parents=True, exist_ok=True)
    brief = Path(a.brief).read_text(encoding="utf-8")
    (d / "brief.md").write_text(brief)
    # --repo wins; else a `**Repo: <path>**` line in the brief; else STEWARD_WORKDIR or the cwd.
    repo = a.repo or brief_repo(brief)
    progress = d / "progress.md"
    if not progress.exists():
        progress.write_text(f"# Checkpoint for {rid}\n\n## Done\n(nothing yet)\n\n## In progress\n\n## Next\n\n## Files touched\n\n## How to verify\n")
    perm, env_extra = permission_flags(), {}
    sid = str(uuid.uuid4())
    budget = DEFAULT_BUDGET_USD if a.max_budget_usd is None else a.max_budget_usd
    save_state(rid, id=rid, brief=str(Path(a.brief).resolve()), model=a.model or "", session_id=sid,
               threshold=a.threshold, max_minutes=a.max_minutes, started=_iso(_now()), resumes=0,
               status="starting", cwd=run_cwd({"repo": repo}), repo=repo or "", max_budget_usd=budget,
               permission_flags=perm,
               claude_version=claude_version(), manager_pid=os.getpid(), claude_pid=None)
    prompt = build_prompt(brief, progress)
    r = Runner(rid, a.threshold, a.max_minutes)
    r.env_extra = env_extra
    r.brief_text = brief
    r.argv_for = lambda p, s, m: _claude_args(p, s, None, m or a.model, [], budget, perm)
    r.resume_argv_for = lambda msg, s, m: _claude_args(msg, None, s, m or a.model, [], budget, perm)
    if repo and not Path(repo).expanduser().is_dir():
        r.log(f"{_now():%H:%M:%S}  MANAGER: repo {repo} is not a directory, the session runs in {run_cwd({})}")
    log_settings_lint(r, run_cwd({"repo": repo}))
    return _guarded(r, _claude_args(prompt, sid, None, a.model, [], budget, perm), "launch")


def _guarded(r: "Runner", argv: list[str], label: str) -> int:
    """r.launch, but a launch that raises still ends with a status, a reason and a number for cost,
    never `starting` with `cost None` until reap() finds it ."""
    try:
        return r.launch(argv, label)
    except Exception as e:  # noqa: BLE001
        st = load_state(r.rid)
        save_state(r.rid, status="failed", reason=f"the manager raised {type(e).__name__}: {_short(str(e), 120)}",
                   pid=None, ended=_iso(_now()), **fallback_cost(r.dir, st))
        print(f"agentctl: {r.rid} failed: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT["failed"]


def resume_notes(rdir: Path) -> str:
    """The operator's notes on this run, from `notes.md` in the run directory, for the resume
    prompt. "" when there are none. Every note is quoted each time, not only the new ones: they
    are a few lines, and a resumed agent half-remembering an instruction is the failure to avoid."""
    path = Path(rdir) / "notes.md"
    try:
        block = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if not block:
        return ""
    return (f"\n\nThe operator left notes for this run in `{path}`. They are quoted below. Act on them "
            "before the checkpoint's Next section.\n\n" + block[-8000:])


def cmd_resume(a) -> int:
    rid = a.id
    st = load_state(rid)
    if not st:
        print(f"agentctl: no run {rid}")
        return 1
    if st.get("status") == "running":
        verdict, why = run_liveness(st, run_dir(rid))
        if verdict != "dead":
            # A session that outlived its manager, or one whose pids cannot be read, may still be
            # editing files: resuming it would put a second session on the same work.
            print(f"agentctl: {rid} is running or its liveness cannot be read ({verdict}: {why}), nothing to resume")
            return 1
    if st.get("status") == "done" and not a.force:
        print(f"agentctl: {rid} finished already ({st.get('reason')}); use --force to continue it anyway")
        return 0
    progress = run_dir(rid) / "progress.md"
    # The resume message costs whatever it makes the agent re-read. Resumes cost nearly as much
    # as first launches, and one build's
    # resume cost MORE than its own first launch, because a resumed agent that is told to
    # "check the working tree" re-reads files its own checkpoint already summarises. Point it
    # at the checkpoint and say so.
    msg = a.message or (
        f"You were paused by the agent manager ({st.get('reason', 'unknown reason')}) at "
        f"{st.get('ended', '?')} and it is now {_now():%Y-%m-%d %H:%M}. Your conversation is "
        f"intact, so everything you had already read is still above you: do NOT re-read it. "
        f"Read `{progress}` (your own checkpoint) and run `git status --short` to see what "
        f"moved while you were stopped. Open a file again only if the checkpoint says you were "
        f"mid-edit in it, or git says another job changed it. Then continue the brief from the "
        f"checkpoint's Next section, keeping the checkpoint updated as before.")
    msg += resume_notes(run_dir(rid))
    if STRETCH.exists() and not a.message:
        msg += (" Once the brief is done and checked, keep going toward its goal under the "
                f"standing goal in `{STRETCH}`.")
    if not st.get("session_id"):
        # Checked before anything is written: this used to set status `starting` and then raise
        # KeyError, and reap() never closed a `starting` run.
        print(f"agentctl: {rid} has no session to resume (status {st.get('status', '?')}: "
              f"{st.get('reason', 'no reason recorded')}); start it again with run", file=sys.stderr)
        return 1
    perm, env_extra = list(st.get("permission_flags") or permission_flags()), {}
    save_state(rid, resumes=int(st.get("resumes", 0)) + 1, status="starting", resume_at="",
               claude_version=claude_version(), manager_pid=os.getpid(), claude_pid=None)
    r = Runner(rid, float(st.get("threshold", DEFAULT_THRESHOLD)), int(st.get("max_minutes", 90)))
    r.env_extra = env_extra
    try:
        r.brief_text = Path(st.get("brief") or "").read_text(encoding="utf-8")
    except OSError:
        r.brief_text = (run_dir(rid) / "brief.md").read_text(encoding="utf-8") if (run_dir(rid) / "brief.md").exists() else ""
    bud = float(st.get("max_budget_usd") or DEFAULT_BUDGET_USD)
    r.argv_for = (lambda p, s, m: _claude_args(p, s, None, m, [], bud, perm)) if r.brief_text else None
    r.resume_argv_for = lambda msg, s, m: _claude_args(msg, None, s, m, [], bud, perm)
    r.nudged = bool(st.get("nudged"))
    log_settings_lint(r, run_cwd(st))
    return _guarded(r, _claude_args(msg, None, st["session_id"], st.get("model", ""), [],
                                    float(st.get("max_budget_usd") or DEFAULT_BUDGET_USD), perm),
                    f"resume #{int(st.get('resumes', 0)) + 1}")


def cmd_pause(a) -> int:
    st = load_state(a.id)
    if not st or st.get("status") != "running":
        print(f"agentctl: {a.id} is not running ({st.get('status', 'no such run')})")
        return 1
    import stewardq as queue_add  # noqa: E402
    when = queue_add.parse_at(a.at) if a.at else ""
    (run_dir(a.id) / "PAUSE").write_text(_iso(when) if when else "")
    print(f"agentctl: pause requested for {a.id}; the manager winds it down within {WATCH_EVERY_S} s"
          + (f" and queues a resume for {when:%a %d %b %H:%M}" if when else ", resume queued for one hour from now"))
    return 0


def cmd_kill(a) -> int:
    st = load_state(a.id)
    if not st:
        # This used to save_state() regardless, which mkdirs, so a mistyped
        # or truncated id became a phantom run that status showed for ever.
        print(f"agentctl: no run {a.id}; nothing killed (status prints every id whole)", file=sys.stderr)
        return 1
    if st.get("status") == "running" and _alive(st.get("manager_pid")):
        (run_dir(a.id) / "KILL").write_text("")
        print(f"agentctl: kill requested for {a.id}; the manager stops it within {WATCH_EVERY_S} s")
        return 0
    for key in ("claude_pid", "pid"):
        if st.get(key) and _alive(st[key]):
            try:
                os.kill(int(st[key]), signal.SIGTERM)
            except OSError as e:
                print(f"agentctl: could not signal session pid {st[key]} ({e}); run kill outside the sandbox",
                      file=sys.stderr)
            break
    save_state(a.id, status="killed", reason="killed by agentctl kill", pid=None, **fallback_cost(run_dir(a.id), st))
    print(f"agentctl: {a.id} marked killed")
    return 0


def cmd_status(a) -> int:
    reap(quiet=False)          # a corpse is reported once, then closed
    u = usage_now()
    if u["pct"] is None:
        print(f"usage: unknown ({u['source']}); the watchdog cannot wind down on this, only react to a limit hit")
    else:
        age = f"{int(u['age_s'] // 60)} min old" if u["age_s"] is not None else "age unknown"
        print(f"usage: {u['pct']:.0f}% of the 5-hour window, resets {u['resets_at']:%H:%M} " if u["resets_at"]
              else f"usage: {u['pct']:.0f}% of the 5-hour window, reset unknown ", end="")
        print(f"({age}, {'fresh' if u['fresh'] else 'STALE, ignored'})")
    runs = sorted((dict(load_state(p.name), _dir=p.name) for p in RUNS.glob("*") if (p / "state.json").exists()),
                  key=lambda s: s.get("updated", ""), reverse=True)
    if not a.all:
        runs = [s for s in runs if s.get("status") != "done" or
                (_parse(s.get("updated", "")) or _now()) > _now() - dt.timedelta(hours=24)]
    if not runs:
        print("no agent runs recorded")
        return 0
    print(f"{'id':32} {'status':10} {'updated':16} {'resumes':7} {'cost':7} reason")
    for s in runs:
        verdict, why = run_liveness(s, RUNS / s["_dir"]) if s.get("status") == "running" else ("", "")
        alive = verdict == "alive"
        # unknown: the pids could not be read and nothing was written lately. The state is left as
        # it is; only a positive dead reading lets reap() write orphaned.
        status = s.get("status", "?") + {"": "", "alive": "", "dead": " (DEAD)", "unknown": " (unknown)"}[verdict]
        if verdict == "alive" and pid_state(s.get("manager_pid")) != "alive":
            status += " (session only)"
        upd = _parse(s.get("updated", ""))
        cost = s.get("cost_usd")
        if not isinstance(cost, (int, float)) and not alive and s.get("status") not in ("running", "starting"):
            # A run that ended before this rule existed: fill its cost in once, from its stream.
            fill = fallback_cost(RUNS / s["_dir"], s)
            if fill and (RUNS / s["_dir"] / "state.json").exists():
                save_state(s["_dir"], _touch=False, **fill)
                cost = fill["cost_usd"]
        print(f"{(s.get('id') or s.get('_dir', '?')):32} {status:10} {upd.strftime('%d %b %H:%M') if upd else '?':16} "
              f"{s.get('resumes', 0):<7} {('$' + format(cost, '.2f')) if isinstance(cost, (int, float)) else '-':7} "
              f"{_short(s.get('reason', ''), 60)}"
              + (f"  resume {_parse(s['resume_at']):%H:%M}" if s.get("resume_at") and _parse(s["resume_at"]) else ""))
    return 0


def cmd_logs(a) -> int:
    p = run_dir(a.id) / "log.md"
    if not p.exists():
        print(f"agentctl: no log for {a.id}")
        return 1
    lines = p.read_text(errors="replace").splitlines()
    print("\n".join(lines[-a.tail:] if a.tail else lines))
    if a.follow:
        os.execvp("tail", ["tail", "-n", "0", "-f", str(p)])
    return 0


def cmd_report(a) -> int:
    """Everything a person (or the next session) needs about one run, in one screen."""
    st = load_state(a.id)
    if not st:
        print(f"agentctl: no run {a.id}")
        return 1
    d = run_dir(a.id)
    print(f"# {a.id}")
    for k in ("status", "reason", "started", "ended", "resumes", "cost_usd", "session_id", "brief", "resume_at"):
        if st.get(k) not in (None, ""):
            print(f"- {k}: {st[k]}")
    print(f"- files: {d}/log.md, stream.jsonl, progress.md" + (", crash.md" if (d / "crash.md").exists() else ""))
    print(f"- resume: python3 \"{Path(__file__).resolve()}\" resume {a.id}")
    if (d / "progress.md").exists():
        print("\n## Checkpoint\n" + (d / "progress.md").read_text(errors="replace").strip())
    if (d / "log.md").exists():
        tail = (d / "log.md").read_text(errors="replace").splitlines()[-15:]
        print("\n## Last log lines\n" + "\n".join(tail))
    return 0


def cmd_usage(a) -> int:
    if a.set is not None:
        set_usage(a.set, a.resets)
    u = usage_now()
    print(json.dumps({**u, "resets_at": _iso(u["resets_at"])}, indent=1, default=str))
    return 0


def prune_streams(days: int = STREAM_KEEP_DAYS) -> tuple[int, int]:
    """Delete stream.jsonl from archived runs older than `days`. Returns (files, bytes).

    stream.jsonl is the raw event feed: every tool call, every result, the full text of every
    file the agent read. It is the largest thing a run leaves behind by an order of magnitude,
    and its only consumer is `render`, which rebuilds log.md from it. Once log.md exists and
    the run is archived, the stream has no reader left.

    log.md and progress.md are never touched. They are the readable record of what the agent
    did and what it thought it was doing, and they are what a future session reads when the
    same build has to be picked up again."""
    arch = RUNS / "archive"
    if not arch.exists():
        return (0, 0)
    cutoff = _now() - dt.timedelta(days=days)
    files = size = 0
    for stream in sorted(arch.glob("*/stream.jsonl")):
        try:
            sj = stream.parent / "state.json"
            st = json.loads(sj.read_text()) if sj.exists() else {}
        except ValueError:
            st = {}
        when = _parse(st.get("updated", "")) or _parse(st.get("started", ""))
        if when is None:
            when = dt.datetime.fromtimestamp(stream.stat().st_mtime, P)
        if when >= cutoff:
            continue
        if not (stream.parent / "log.md").exists():
            continue          # nothing readable yet; the stream is the only record
        size += stream.stat().st_size
        stream.unlink()
        files += 1
    return (files, size)


def cmd_gc(a) -> int:
    arch = RUNS / "archive"
    arch.mkdir(parents=True, exist_ok=True)
    cutoff = _now() - dt.timedelta(days=a.days)
    n = 0
    for p in RUNS.glob("*"):
        if p.name == "archive" or not (p / "state.json").exists():
            continue
        st = load_state(p.name)
        upd = _parse(st.get("updated", ""))
        if st.get("status") in ("done", "killed") and upd and upd < cutoff:
            shutil.move(str(p), str(arch / p.name))
            n += 1
    print(f"archived {n} run(s) older than {a.days} days")
    files, size = prune_streams(a.stream_days)
    print(f"dropped {files} stream.jsonl file(s) ({size // 1024} KB) from archived runs "
          f"older than {a.stream_days} days; log.md and progress.md kept")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--id", required=True); r.add_argument("--brief", required=True)
    r.add_argument("--model", default=""); r.add_argument("--max-minutes", type=int, default=90)
    r.add_argument("--repo", default="", help="the session's working directory (default: STEWARD_WORKDIR or the current directory)")
    r.add_argument("--max-budget-usd", type=float, default=None,
                   help=f"dollar ceiling for one launch (default {DEFAULT_BUDGET_USD:g}; 0 for none)")
    r.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    r.add_argument("--force", action="store_true", help=f"start even if {MAX_CONCURRENT} runs are already running")
    rs = sub.add_parser("resume"); rs.add_argument("id"); rs.add_argument("--message"); rs.add_argument("--force", action="store_true")
    pz = sub.add_parser("pause"); pz.add_argument("id"); pz.add_argument("--at")
    k = sub.add_parser("kill"); k.add_argument("id")
    s = sub.add_parser("status"); s.add_argument("--all", action="store_true")
    lg = sub.add_parser("logs"); lg.add_argument("id"); lg.add_argument("--tail", type=int, default=40)
    lg.add_argument("--follow", "-f", action="store_true")
    rp = sub.add_parser("report"); rp.add_argument("id")
    rd = sub.add_parser("render"); rd.add_argument("id")
    us = sub.add_parser("usage"); us.add_argument("--set", type=float); us.add_argument("--resets")
    g = sub.add_parser("gc"); g.add_argument("--days", type=int, default=14)
    g.add_argument("--stream-days", type=int, default=STREAM_KEEP_DAYS)
    a = ap.parse_args()
    RUNS.mkdir(parents=True, exist_ok=True)
    if a.cmd == "render":
        print(render_all(a.id))
        return 0
    return {"run": cmd_run, "resume": cmd_resume, "pause": cmd_pause, "kill": cmd_kill,
            "status": cmd_status, "logs": cmd_logs, "usage": cmd_usage, "gc": cmd_gc,
            "report": cmd_report}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
