#!/usr/bin/env python3
"""agentctl.py's own tests: the model ladder with the names launchers actually pass, and the
manager's commands against a temporary runs directory.

    python3 agentctl_selftest.py

RUNS and USAGE point at a temporary directory and Runner is replaced, so nothing launches and
no `claude` binary is needed. The cases cover the failures the manager was built around:

  - an out-of-credits model escalating to the wrong rung of the ladder
  - `kill` on an unknown id inventing a phantom run, and `status` truncating ids
  - `resume` on a run with no session id leaving it at `starting` for ever
  - the weekly ceiling trusting a stale usage snapshot
  - context rotation, the checkpoint nudge, honest outcomes, and liveness under a sandbox

Stdlib only. Compiles under 3.9.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agentctl as A  # noqa: E402

RUNNER = A.Runner   # temp_runs() swaps A.Runner for a stub; the rotation tests need the real one

CASES = [
    ("opus", "claude-sonnet-5"),
    ("sonnet", "claude-haiku-4-5-20251001"),
    ("haiku", ""),
    ("claude-opus-5", "claude-sonnet-5"),
    ("claude-sonnet-5", "claude-haiku-4-5-20251001"),
    ("claude-haiku-4-5", ""),
    ("claude-haiku-4-5-20251001", ""),
    ("fable", "claude-opus-5-5"),
    ("claude-fable-5-1", "claude-opus-5-5"),
    ("claude-opus-5-5", "claude-sonnet-5"),
    ("", "claude-opus-5-5"),
    ("Opus", "claude-sonnet-5"),
]


@contextlib.contextmanager
def temp_runs():
    saved = (A.RUNS, A.USAGE, A.Runner, A.BUILD_LOCKS)
    with tempfile.TemporaryDirectory(prefix="zz-test-runs-") as td:
        A.RUNS = Path(td)
        A.USAGE = Path(td) / "zz-test-usage.json"
        A.BUILD_LOCKS = Path(td) / "zz-test-builds"

        def _no_launch(*_a, **_k):
            raise RuntimeError("zz-test: the selftest would have launched a session")
        A.Runner = _no_launch
        try:
            yield Path(td)
        finally:
            A.RUNS, A.USAGE, A.Runner, A.BUILD_LOCKS = saved


def quietly(fn, *args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            return fn(*args), buf.getvalue(), ""
        except Exception as e:  # noqa: BLE001
            return None, buf.getvalue(), f"{type(e).__name__}: {e}"


def _state(d: Path, **st) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(json.dumps(st))


def t_kill_unknown() -> list:
    with temp_runs() as b:
        rc, out, err = quietly(A.cmd_kill, types.SimpleNamespace(id="2026-10-01-0000-build-an-exampl"))
        return [("kill on an id with no run exits non-zero", rc not in (0, None) and not err, f"rc={rc} {err} {out!r}"),
                ("kill on an id with no run creates no directory", not any(b.iterdir()),
                 str([p.name for p in b.iterdir()]))]


def t_status_full_id() -> list:
    rid = "2026-10-01-0000-build-an-example-feature-with-a-long-descriptive-id"
    with temp_runs() as b:
        _state(b / rid, id=rid, status="killed", reason="zz-test", updated=A._iso(A._now()))
        _state(b / "zz-test-no-id", status="killed", reason="zz-test", updated=A._iso(A._now()))
        rc, out, err = quietly(A.cmd_status, types.SimpleNamespace(all=True))
        return [("status prints an id whole, so it can be pasted into kill or resume", rid in out, out[-400:] + err),
                ("a state with no id is shown by its directory name, not '?'", "zz-test-no-id" in out, out[-400:])]


def t_resume_without_session() -> list:
    with temp_runs() as b:
        _state(b / "zz-test-x", status="killed", reason="killed by agentctl kill", pid=None)
        rc, out, err = quietly(A.cmd_resume, types.SimpleNamespace(id="zz-test-x", message=None, force=False))
        st = json.loads((b / "zz-test-x" / "state.json").read_text())
        return [("resume of a run with no session says so and exits non-zero", rc not in (0, None) and not err,
                 f"rc={rc} {err} {out!r}"),
                ("resume that cannot run leaves the state it found", st.get("status") == "killed", json.dumps(st))]


def t_reap_starting() -> list:
    with temp_runs() as b:
        old = A._iso(A._now() - dt.timedelta(hours=1))
        _state(b / "zz-test-stuck", id="zz-test-stuck", status="starting", updated=old)
        _state(b / "zz-test-launching", id="zz-test-launching", status="starting", updated=A._iso(A._now()))
        _, out, err = quietly(A.reap)
        stuck = json.loads((b / "zz-test-stuck" / "state.json").read_text())
        fresh = json.loads((b / "zz-test-launching" / "state.json").read_text())
        return [("reap closes a run left at 'starting' an hour ago with no manager", stuck.get("status") == "orphaned",
                 json.dumps(stuck) + err),
                ("reap leaves a run that started a moment ago alone", fresh.get("status") == "starting", json.dumps(fresh))]


def t_weekly_ceiling() -> list:
    now = A._now()
    hit = getattr(A, "weekly_ceiling_hit", None)
    if hit is None:
        return [("agentctl.weekly_ceiling_hit exists", False, "missing")]
    fresh = {"fresh": True, "week_pct": 93.0, "week_resets_at": now + dt.timedelta(days=2)}
    out = [("a fresh 93% week refuses an Opus run", hit(fresh, "opus") is True, ""),
           ("a stale 93% reading refuses nothing", hit(dict(fresh, fresh=False), "opus") is False, ""),
           ("a 93% reading of a window that has already reset refuses nothing",
            hit(dict(fresh, week_resets_at=now - dt.timedelta(hours=70)), "opus") is False, ""),
           ("the ladder's own haiku name is exempt", hit(fresh, "claude-haiku-4-5-20251001") is False, ""),
           ("the short haiku name is exempt", hit(fresh, "haiku") is False, "")]
    with temp_runs() as b:
        old = now - dt.timedelta(days=3)
        A.USAGE.write_text(json.dumps({"_written": A._iso(old), "data": {"rate_limits": {
            "five_hour": {"used_percentage": 20, "resets_at": (old + dt.timedelta(hours=1)).timestamp()},
            "seven_day": {"used_percentage": 93, "resets_at": (old + dt.timedelta(hours=2)).timestamp()}}}}))
        out.append(("usage.json three days old does not trip the weekly gate", hit(A.usage_now(), "opus") is False, ""))
    return out


def _asst(ctx, tool=True, parent=None, mid="m1", model="claude-opus-5-5", out=100):
    content = [{"type": "tool_use", "name": "Bash", "input": {}}] if tool else [{"type": "text", "text": "done"}]
    return {"type": "assistant", "parent_tool_use_id": parent,
            "message": {"id": mid, "model": model, "content": content,
                        "usage": {"input_tokens": 2, "cache_read_input_tokens": ctx - 2, "cache_creation_input_tokens": 0,
                                  "output_tokens": out}}}


def t_rotation_trigger() -> list:
    with temp_runs():
        r = RUNNER("zz-test-rot", 90, 60)
        stops = []
        r._stop_child = lambda why: stops.append(why)
        r._watch_context(_asst(100_000))
        r._watch_context(_asst(200_000, parent="toolu_x"))           # a subagent's turn does not count
        below = not r.rotate_pending
        r._watch_context(_asst(131_000, tool=False))                 # the final message: nothing to cut off
        final_ok = not r.rotate_pending
        r._watch_context(_asst(131_000))
        armed = r.rotate_pending and r.outcome is None
        r._watch_context({"type": "user", "parent_tool_use_id": None})
        import time as _t; _t.sleep(0.1)
        r2 = RUNNER("zz-test-cap", 90, 60)
        r2.rotations = A.MAX_ROTATIONS
        r2._watch_context(_asst(300_000))
        return [("under 130k, or a subagent turn, arms nothing", below, ""),
                ("a final text-only message past 130k arms nothing", final_ok, ""),
                ("a tool-calling turn past 130k arms, and the next tool result rotates", armed and r.outcome == "rotating" and stops == ["rotating the session"], f"{r.outcome} {stops}"),
                ("no rotation past the cap", not r2.rotate_pending, "")]


def t_rotation_relaunch() -> list:
    with temp_runs() as b:
        rid = "zz-test-relaunch"
        d = b / rid
        _state(d, id=rid, model="claude-opus-5-5", session_id="old", brief="x", resumes=0)
        (d / "progress.md").write_text("# Checkpoint\n## Done\nDECISION-MARKER-42\n")
        (d / "stream.jsonl").write_text("\n".join(json.dumps(e) for e in (_asst(130_000, mid="a", out=1000), _asst(140_000, mid="b", out=1000))) + "\n")
        r = RUNNER(rid, 90, 60)
        r.brief_text, r.last_ctx, r.outcome = "BRIEF-MARKER", 140_000, "rotating"
        seen = {}
        r.argv_for = lambda p, s, m: ["claude", p, s, m]
        r.launch = lambda argv, label: seen.update(argv=argv, label=label) or 0
        r._rotate(0)
        p = seen["argv"][1]
        st = json.loads((d / "state.json").read_text())
        log = (d / "log.md").read_text()
        return [("rotation prompt carries the checkpoint and the brief, brief last",
                 "DECISION-MARKER-42" in p and p.rstrip().endswith("BRIEF-MARKER") and p.index("DECISION-MARKER-42") < p.index("BRIEF-MARKER"), p[-200:]),
                ("rotation starts a NEW session id and records it, the count and a cost",
                 seen["argv"][2] != "old" and st["session_id"] == seen["argv"][2] and st["rotations"] == 1 and st["cost_usd"] > 0, json.dumps(st)),
                ("rotation writes one log line naming the tokens", "rotation 1 of" in log and "140,000 tokens" in log, log),
                ("the new launch is labelled", seen["label"] == "rotation #1", seen["label"])]


def t_prompt_order_and_flags() -> list:
    p = A.build_prompt("THE-BRIEF", Path("/tmp/zz-progress.md"))
    argv = A._claude_args("p", "sid", None, "claude-opus-5-5", [], 5.0, None)
    return [("prompt puts the constant blocks first and the brief last",
             p.rstrip().endswith("THE-BRIEF") and p.index("How you are being run") < p.index("THE-BRIEF"), p[:80]),
            ("launch carries both lean flags", "--disable-slash-commands" in argv and "--exclude-dynamic-system-prompt-sections" in argv, str(argv))]


def t_style_block() -> list:
    """An optional house style file rides in every managed prompt, before the brief, and log
    lines are cut at a word boundary."""
    saved = A.STYLE
    with tempfile.TemporaryDirectory(prefix="zz-test-style-") as td:
        A.STYLE = Path(td) / "style.md"
        A.STYLE.write_text("Open with the bottom line in one sentence.\n")
        try:
            p = A.build_prompt("THE-BRIEF", Path("/tmp/zz-progress.md"))
            A.STYLE = Path(td) / "missing.md"
            bare = A.build_prompt("THE-BRIEF", Path("/tmp/zz-progress.md"))
        finally:
            A.STYLE = saved
    s = A._short("alpha " * 60, 40)
    return [("the prompt carries the house style, before the brief",
             "Open with the bottom line" in p and p.index("Open with the bottom line") < p.index("THE-BRIEF"), p[-400:]),
            ("the brief is still last", p.rstrip().endswith("THE-BRIEF"), p[-80:]),
            ("no style file, no style block", "# House style" not in bare, bare[-200:]),
            ("_short cuts at a word boundary", s.endswith("alpha…") and len(s) <= 40, s),
            ("_short leaves a short line alone", A._short("a b  c") == "a b c", A._short("a b  c"))]


def t_stream_cost() -> list:
    with temp_runs() as b:
        f = b / "stream.jsonl"
        # message a appears twice (one line per content block): counted once, the later usage wins
        evs = [_asst(10_000, mid="a", out=10, model="claude-opus-5"), _asst(10_000, mid="a", out=1000, model="claude-opus-5")]
        f.write_text("# header\n" + "\n".join(json.dumps(e) for e in evs) + "\n{not json\n")
        got = A.stream_cost(f)
        want = round((2 * 5 + 9_998 * 0.5 + 1000 * 25) / 1e6, 3)
        rid = "zz-test-orphan"
        _state(b / rid, id=rid, status="running", manager_pid=99999999, pid=None, cost_usd=0)
        (b / rid / "stream.jsonl").write_text(f.read_text())
        quietly(A.reap)
        st = json.loads((b / rid / "state.json").read_text())
        return [("stream_cost counts a message once and prices its cache reads and output", got == want, f"{got} != {want}"),
                ("an orphaned run gets an estimated cost, flagged", st.get("status") == "orphaned" and st.get("cost_usd") == want
                 and st.get("cost_estimated") is True, json.dumps(st)),
                ("no stream, no estimate", A.stream_cost(b / "nope.jsonl") is None, "")]


def _finish_case(b: Path, rid: str, rc: int, progress_before: str, progress_after: str, **runner) -> tuple:
    """Run Runner._finish on a fake ended session. Pushes, notices and the queue are captured."""
    d = b / rid
    _state(d, id=rid, model="claude-haiku-4-5-20251001", session_id="s", resumes=0)
    (d / "progress.md").write_text(progress_after)
    r = RUNNER(rid, 90, 60)
    r.progress_at_start = progress_before
    r.brief_text = "ZZ-BRIEF"
    for k, v in runner.items():
        setattr(r, k, v)
    pushes, queued, resumes = [], [], []
    saved = (A._push, A._notify)
    A._push = lambda text, note, subject: pushes.append(subject)
    A._notify = lambda *_a: None
    r._queue_resume = lambda: resumes.append(r.resume_at)
    fake_q = types.SimpleNamespace(add_brief=lambda title, body, at, **k: queued.append((title, body)) or Path("zz-q.md"))
    saved_q = sys.modules.get("stewardq")
    sys.modules["stewardq"] = fake_q
    try:
        code = r._finish(rc)
    finally:
        A._push, A._notify = saved
        if saved_q is not None:
            sys.modules["stewardq"] = saved_q
        else:
            sys.modules.pop("stewardq", None)
    st = json.loads((d / "state.json").read_text())
    return code, st, pushes, queued, resumes, (d / "crash.md").exists()


def t_honest_outcomes() -> list:
    """No green verdict over a failed step, and every ended run has a cost."""
    out = []
    with temp_runs() as b:
        code, st, pushes, _, resumes, crash = _finish_case(
            b, "zz-test-credit", 1, "a", "b",
            last_result={"is_error": True, "result": "Your credit balance is too low to access the Anthropic API"})
        out.append(("a credit-balance error is out-of-credits, exit 82, no resume, one push",
                    code == 82 and st["status"] == "out-of-credits" and not resumes and len(pushes) == 1,
                    f"{code} {st.get('status')} {resumes} {pushes}"))
        out.append(("an out-of-credits run still records a number for cost", isinstance(st.get("cost_usd"), (int, float)),
                    json.dumps(st)))
        code, st, _, _, _, _ = _finish_case(b, "zz-test-ladder-spent", 1, "a", "b",
                                            credits_text="You're out of usage credits. Switch to another model")
        out.append(("out of usage credits with the ladder spent is out-of-credits, not limited",
                    st["status"] == "out-of-credits", st.get("status")))
        code, st, _, _, resumes, crash = _finish_case(b, "zz-test-net", 1, "a", "b",
                                                      network_text="getaddrinfo ENOTFOUND api.anthropic.com")
        wait = (A._parse(st.get("resume_at", "")) - A._now()).total_seconds() if st.get("resume_at") else -1
        out.append(("ENOTFOUND is interrupted, retried in five minutes, with no crash report",
                    st["status"] == "interrupted" and 200 < wait <= 305 and not crash and st.get("network_retries") == 1
                    and len(resumes) == 1, f"{st.get('status')} wait={wait:.0f}s crash={crash} {st.get('network_retries')}"))
        code, st, _, _, _, _ = _finish_case(b, "zz-test-noop", 0, "same", "same")
        out.append(("a clean exit with progress.md unchanged is no-op, exit 83", code == 83 and st["status"] == "no-op",
                    f"{code} {st.get('status')}"))
        out.append(("a no-op run with no stream records cost 0.0, flagged, never None",
                    st.get("cost_usd") == 0.0 and st.get("cost_estimated") is True, json.dumps(st)))
        code, st, _, _, _, _ = _finish_case(b, "zz-test-done", 0, "before", "after")
        out.append(("a clean exit that moved its checkpoint is done", code == 0 and st["status"] == "done", st.get("status")))
        code, st, _, queued, _, _ = _finish_case(b, "zz-test-refused", 0, "## Done\n", "## Done\nstatus: refused\nout of scope\n")
        out.append(("a continuation that writes status: refused is refused (84), never done, and re-queued once",
                    code == 84 and st["status"] == "refused" and len(queued) == 1 and "ZZ-BRIEF" in queued[0][1]
                    and st.get("refusal_requeued"), f"{code} {st.get('status')} {queued}"))
        d = b / "zz-test-refused"
        (d / "progress.md").write_text("## Done\nstatus: refused again\n")
        r = RUNNER("zz-test-refused", 90, 60)
        r.progress_at_start, r.brief_text = "## Done\n", "ZZ-BRIEF"
        again = []
        saved = (A._push, A._notify)
        A._push, A._notify = (lambda *a: again.append("push")), (lambda *a: None)
        sys.modules["stewardq"] = types.SimpleNamespace(add_brief=lambda *a, **k: again.append("queued") or Path("x"))
        try:
            r._finish(0)
        finally:
            A._push, A._notify = saved
            sys.modules.pop("stewardq", None)
        out.append(("a second refusal is not re-queued again", "queued" not in again and "push" in again, str(again)))
        # kill of an ended run and the status backfill both leave a number
        _state(b / "zz-test-old", id="zz-test-old", status="failed", reason="zz", updated=A._iso(A._now()))
        quietly(A.cmd_status, types.SimpleNamespace(all=True))
        st = json.loads((b / "zz-test-old" / "state.json").read_text())
        out.append(("status fills in a missing cost once, without bumping updated",
                    st.get("cost_usd") == 0.0 and st.get("cost_estimated") is True, json.dumps(st)))
        _state(b / "zz-test-k", id="zz-test-k", status="failed", updated=A._iso(A._now()))
        quietly(A.cmd_kill, types.SimpleNamespace(id="zz-test-k"))
        st = json.loads((b / "zz-test-k" / "state.json").read_text())
        out.append(("kill records a cost", isinstance(st.get("cost_usd"), (int, float)), json.dumps(st)))
    out.append(("a run's repo field sets the session cwd", A.run_cwd({"repo": str(Path.home())}) == str(Path.home()), ""))
    out.append(("a missing repo falls back to the default workdir", A.run_cwd({"repo": "/nonexistent/zz-test"}) == str(A.DEFAULT_WORKDIR)
                and A.run_cwd({}) == str(A.DEFAULT_WORKDIR), ""))
    import os
    saved_cap, saved_env = A.CAPACITY, os.environ.pop("STEWARD_MAX_CONCURRENT", None)
    try:
        with tempfile.TemporaryDirectory(prefix="zz-test-cap-") as td:
            A.CAPACITY = Path(td) / "capacity.json"
            A.CAPACITY.write_text('{"slots": 3}')
            three = A.capacity_slots()
            A.CAPACITY.write_text("not json")
            bad = A.capacity_slots()
            os.environ["STEWARD_MAX_CONCURRENT"] = "9"
            env = A.capacity_slots()
    finally:
        A.CAPACITY = saved_cap
        os.environ.pop("STEWARD_MAX_CONCURRENT", None)
        if saved_env is not None:
            os.environ["STEWARD_MAX_CONCURRENT"] = saved_env
    out.append(("capacity.json slots is the cap, a bad file gives 6, the env var still wins",
                (three, bad, env) == (3, 6, 9), str((three, bad, env))))
    p = A.rotation_preamble(Path("/nonexistent/zz"), 140_000, 1, "")
    out.append(("a rotated session is told how to refuse without being marked done", "status: refused" in p, p[:200]))
    return out


def t_repo_lint_nudge() -> list:
    """A brief names its repo, the lint warns at launch, one nudge."""
    import os
    import time
    out = []
    hdr = "Repo: `~/example-repo` (run from its root unless the brief says otherwise).\n"
    out.append(("a **Repo: <path>** line in a brief names the repo, ~ expanded",
                A.brief_repo(hdr + "**Repo: `~/zz-test`**\n") == str(Path.home() / "zz-test")
                and A.brief_repo("**Repo: /tmp**") == "/tmp", A.brief_repo("**Repo: /tmp**")))
    out.append(("the common header's plain Repo line does not count", A.brief_repo(hdr) == "", A.brief_repo(hdr)))
    out.append(("settings_findings returns a list of strings, never raises",
                isinstance(A.settings_findings(), list), ""))
    with temp_runs() as b, tempfile.TemporaryDirectory(prefix="zz-test-repo-") as repo:
        brief = b / "zz-brief.md"
        brief.write_text(hdr + "\n**Repo: " + repo + "**\n\nDo the thing.\n")
        seen = {}

        class FakeRunner(RUNNER):
            def launch(self, argv, label):
                seen.update(argv=argv, label=label)
                return 0
        saved = (A.Runner, A.settings_findings, A.claude_version)
        A.Runner, A.settings_findings, A.claude_version = FakeRunner, (lambda cwd="": ["zz-finding: bad rule"]), (lambda: "zz")
        try:
            ns = types.SimpleNamespace(id="zz-test-repo", force=True, model="", brief=str(brief), role="", mode="",
                                       max_budget_usd=None, threshold=90.0, max_minutes=60, repo=None)
            rc, o, err = quietly(A.cmd_run, ns)
        finally:
            A.Runner, A.settings_findings, A.claude_version = saved
        st = json.loads((b / "zz-test-repo" / "state.json").read_text())
        log = (b / "zz-test-repo" / "log.md").read_text() if (b / "zz-test-repo" / "log.md").exists() else ""
        out.append(("a brief's Repo line sets the run's repo and cwd", st.get("repo") == repo and st.get("cwd") == repo,
                    f"{rc} {err} {json.dumps(st)[:300]}"))
        out.append(("a settings lint finding is logged as a warning and the run still launches",
                    "settings lint WARNING: zz-finding" in log and seen.get("label") == "launch" and st.get("settings_lint") == 1,
                    log[-300:] + err))
    with temp_runs() as b:
        rid = "zz-test-nudge"
        d = b / rid
        _state(d, id=rid, model="claude-opus-5-5", session_id="sess-1")
        (d / "progress.md").write_text("# Checkpoint\n")
        old = time.time() - 25 * 60
        os.utime(d / "progress.md", (old, old))
        r = RUNNER(rid, 90, 60)
        r.launched_wall = old
        stops, seen = [], {}
        r._stop_child = lambda why: stops.append(why)
        r._check_nudge()
        idle = not r.nudge_pending
        r.resume_argv_for = lambda msg, s, m: ["claude", msg, s]
        r._check_nudge()
        armed = r.nudge_pending
        r._watch_context(_asst(50_000, parent="toolu_sub"))
        r._watch_context({"type": "user", "parent_tool_use_id": "toolu_sub"})
        sub_ok = r.outcome is None
        r._watch_context({"type": "user", "parent_tool_use_id": None})
        time.sleep(0.1)
        fired = r.outcome == "nudging" and stops == ["nudging the session"]
        r.launch = lambda argv, label: seen.update(argv=argv, label=label) or 0
        r._nudge(0)
        st = json.loads((d / "state.json").read_text())
        r._check_nudge()
        out += [("no nudge without a way to resume the session", idle, ""),
                ("25 minutes with no checkpoint write arms one nudge", armed, ""),
                ("a subagent's tool result does not fire it", sub_ok, str(r.outcome)),
                ("the next main-thread tool result stops the session to nudge it", fired, f"{r.outcome} {stops}"),
                ("the nudge resumes the SAME session with a checkpoint message",
                 seen.get("label") == "nudge" and seen["argv"][2] == "sess-1" and "has not been written for 25 minutes" in seen["argv"][1],
                 str(seen)[:300]),
                ("the nudge is recorded and never sent twice", bool(st.get("nudged")) and not r.nudge_pending and r.nudged,
                 json.dumps(st))]
        (d / "progress.md").write_text("# Checkpoint\nfresh\n")
        r2 = RUNNER(rid, 90, 60)
        r2.launched_wall, r2.resume_argv_for = old, (lambda *a: [])
        r2._check_nudge()
        out.append(("a checkpoint written a moment ago arms nothing", not r2.nudge_pending, ""))
    return out


@contextlib.contextmanager
def kill_says(exc_for):
    """Replace os.kill inside agentctl: exc_for(pid, sig) returns an exception to raise, or None."""
    real = A.os.kill

    def fake(pid, sig):
        e = exc_for(pid, sig)
        if e is not None:
            raise e
        return real(pid, sig)
    A.os.kill = fake
    try:
        yield
    finally:
        A.os.kill = real


def _dead_pid() -> int:
    """The pid of a process that has exited and been reaped: os.kill on it is ProcessLookupError."""
    pr = subprocess.Popen([sys.executable, "-c", "pass"])
    pr.wait()
    return pr.pid


def _age(path: Path, minutes: float) -> None:
    t = time.time() - minutes * 60
    os.utime(path, (t, t))


def t_liveness_without_ps() -> list:
    """Under a sandboxed shell os.kill on another process raises PermissionError, and
    _alive() read it as dead, so a sandboxed status marked four live runs orphaned. A session can
    also outlive its manager. Only a positive dead reading may write orphaned."""
    out = []
    dead = _dead_pid()
    out.append(("pid_state: a reaped process is dead (ProcessLookupError)", A.pid_state(dead) == "dead", A.pid_state(dead)))
    out.append(("pid_state: this process is alive", A.pid_state(os.getpid()) == "alive", ""))
    out.append(("pid_state: no pid is unknown, never dead", A.pid_state(None) == "unknown" and A.pid_state("") == "unknown", ""))
    with kill_says(lambda pid, sig: PermissionError(1, "Operation not permitted")):
        out.append(("pid_state: PermissionError (the sandbox) is alive", A.pid_state(424242) == "alive", ""))
    with kill_says(lambda pid, sig: OSError(22, "Invalid argument")):
        out.append(("pid_state: any other OSError is unknown", A.pid_state(424242) == "unknown", ""))

    with temp_runs() as b:
        # a) a live manager the sandbox will not let us signal
        rid = "zz-test-sandboxed"
        _state(b / rid, id=rid, status="running", manager_pid=424242, pid=424243, claude_pid=424243)
        _age(b / rid / "state.json", 60)
        with kill_says(lambda pid, sig: PermissionError(1, "Operation not permitted")):
            _, text, err = quietly(A.cmd_status, types.SimpleNamespace(all=True))
        st = json.loads((b / rid / "state.json").read_text())
        out.append(("sandboxed status leaves a live run running, not orphaned", st.get("status") == "running",
                    json.dumps(st) + err))
        out.append(("sandboxed status does not show the run as DEAD", "DEAD" not in text, text[-300:]))

        # b) the manager is gone but the Claude session is still alive: alive, never killed
        rid = "zz-test-outlived"
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            _state(b / rid, id=rid, status="running", manager_pid=dead, pid=None, claude_pid=child.pid)
            verdict = A.run_liveness(json.loads((b / rid / "state.json").read_text()), b / rid)[0]
            quietly(A.reap)
            st = json.loads((b / rid / "state.json").read_text())
            out.append(("a session that outlived its manager reads alive", verdict == "alive", verdict))
            out.append(("reap leaves it running and does not kill it", st.get("status") == "running" and child.poll() is None,
                        json.dumps(st)))
            _, text, _ = quietly(A.cmd_status, types.SimpleNamespace(all=True))
            out.append(("status marks it session only", "session only" in text, text[-300:]))
            rc, text, err = quietly(A.cmd_resume, types.SimpleNamespace(id=rid, message=None, force=False))
            out.append(("resume refuses a run whose session is still alive", rc == 1 and "nothing to resume" in text,
                        f"rc={rc} {text} {err}"))
        finally:
            child.kill()
            child.wait()

        # c) both pids dead, but log.md written three minutes ago: alive by the second signal
        rid = "zz-test-fresh-log"
        _state(b / rid, id=rid, status="running", manager_pid=dead, claude_pid=dead)
        (b / rid / "log.md").write_text("14:40:00  tool: Edit\n")
        _age(b / rid / "log.md", 3)
        quietly(A.reap)
        st = json.loads((b / rid / "state.json").read_text())
        out.append(("a run whose log.md moved in the last minutes is not orphaned", st.get("status") == "running",
                    json.dumps(st)))

        # d) a positive dead reading: both pids dead, nothing written for an hour
        rid = "zz-test-really-dead"
        _state(b / rid, id=rid, status="running", manager_pid=dead, claude_pid=dead)
        (b / rid / "log.md").write_text("x\n")
        (b / rid / "progress.md").write_text("x\n")
        _age(b / rid / "log.md", 60)
        _age(b / rid / "progress.md", 60)
        lock = A.BUILD_LOCKS / "zz-test-pipe" / "worktree.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"entry": rid}))
        quietly(A.reap)
        st = json.loads((b / rid / "state.json").read_text())
        out.append(("both pids dead and nothing written: orphaned", st.get("status") == "orphaned"
                    and "dead" in st.get("reason", ""), json.dumps(st)))
        out.append(("an orphaned run releases the worktree lock it held", not lock.exists(), ""))

        # e) unknown: the pids cannot be read either way and nothing moved
        rid = "zz-test-unknown"
        _state(b / rid, id=rid, status="running", manager_pid=424242, claude_pid=424243)
        with kill_says(lambda pid, sig: OSError(22, "Invalid argument")):
            _, text, _ = quietly(A.cmd_status, types.SimpleNamespace(all=True))
        st = json.loads((b / rid / "state.json").read_text())
        out.append(("liveness unknown: the state is left unchanged", st.get("status") == "running", json.dumps(st)))
        out.append(("liveness unknown: status reports it as unknown", "(unknown)" in text, text[-300:]))

    # f) the launch records the Claude session's own pid
    with temp_runs() as b:
        rid = "zz-test-claude-pid"
        _state(b / rid, id=rid, status="starting", cwd=str(b))
        pidfile = b / "child.pid"
        r = RUNNER(rid, 90, 60)
        r._finish = lambda rc: rc
        rc, _, err = quietly(r.launch, [sys.executable, "-c", f"import os; open({str(pidfile)!r}, 'w').write(str(os.getpid()))"],
                             "launch")
        st = json.loads((b / rid / "state.json").read_text())
        got = pidfile.read_text().strip() if pidfile.exists() else ""
        out.append(("launch records the session's own pid as claude_pid, beside the manager's",
                    got and str(st.get("claude_pid")) == got and st.get("manager_pid") == os.getpid(),
                    f"child {got!r} state {json.dumps(st)} {err}"))
    return out


def main() -> int:
    failed = total = 0
    for asked, want in CASES:
        got = A._next_model(asked)
        ok = got == want
        failed += not ok
        total += 1
        print("%s  %r out of credits -> %r%s" % ("PASS" if ok else "FAIL", asked, got,
                                                  "" if ok else "   (expected %r)" % want))
    for t in (t_kill_unknown, t_status_full_id, t_resume_without_session, t_reap_starting, t_weekly_ceiling,
              t_rotation_trigger, t_rotation_relaunch, t_prompt_order_and_flags, t_style_block, t_stream_cost, t_honest_outcomes, t_repo_lint_nudge,
              t_liveness_without_ps):
        try:
            rows = t()
        except Exception as e:  # noqa: BLE001
            rows = [(f"{t.__name__} ran to the end", False, f"{type(e).__name__}: {e}")]
        for name, ok, detail in rows:
            failed += not ok
            total += 1
            print("%s  %s%s" % ("PASS" if ok else "FAIL", name, "" if ok else "\n      " + str(detail)[:300]))
    print("\nagentctl selftest: %d/%d passed" % (total - failed, total))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
