# Steward

Steward runs unattended Claude Code sessions so a usage limit or a sleeping laptop pauses the work instead of losing it.

It is the managed agent runner from my Life Tracker system, taken out on its own. Every headless `claude -p` run goes through one manager, `agentctl.py`, which keeps a written checkpoint and the session id, winds the run down before the account's usage window runs out, and resumes it after the reset with the whole conversation intact.

## Why it exists

An unattended Claude Code run can hit the account's usage limit halfway through a task, be cut off when the Mac sleeps, or run so long that every turn re-sends a huge conversation and the cost grows with the square of its length. Restarting from scratch wastes the work, and restarting blind repeats it. Steward keeps the two things a clean restart needs. One is the conversation itself, through the session id. The other is a five-part checkpoint the agent writes for a future self with no memory.

## What it does

- **Wind-down.** At 90% of the five-hour usage window the run is stopped and a resume is queued for two minutes after the reset. The threshold drops four points for each other run in flight, and never goes below 60%, because parallel runs spend the window fast. At 95% of the seven-day window running runs wind down too, and above 92% no new Opus or Sonnet run starts (Haiku runs still may).
- **Resume with the conversation.** A resume reuses the session id, so the agent gets its whole conversation back. The resume message points it at its checkpoint rather than a re-read of the tree.
- **Context rotation.** Past 130,000 tokens of context the session is ended at the next tool result, so no call is cut off, and a fresh session starts from the brief and the checkpoint alone. This happens at most four times per launch.
- **Liveness.** A run counts as alive if the manager's process, the session's process or a recent write to its log or checkpoint says so. Only a positive dead reading closes a run as orphaned, so a sandboxed status check never kills live work.
- **Nudge.** A run that writes no checkpoint for 20 minutes is stopped at its next tool result and resumed in place once with a message asking for the checkpoint.
- **Caps.** At most six managed runs at once, a 90-minute default time cap, and a $25 budget ceiling per launch (passed to `claude --max-budget-usd`). A flag file lifts the time caps while keeping the wind-down.
- **Honest outcomes.** Each ending has its own exit code: done, failed, dormant (wound down), limited, timed out, killed, interrupted (network), out of credits, no-op (finished without touching its checkpoint) and refused (the agent judged the brief out of scope). A network drop retries every five minutes for an hour. A model that is out of credits falls back down a model ladder.
- **Crash forensics.** Any abnormal end writes `crash.md` with the exit code, the reason, the last forty log lines, stderr, the checkpoint and the exact resume command.
- **A timed queue.** `stewardq.py` holds briefs and resumes with a time each. A tick, run every minute, starts what is due (resumes first, so a paused build finishes before new work starts) and defers anything the cap or the weekly ceiling would refuse.

## Architecture

```
brief.md ──> stewardq.py add ──> queue/pending/<entry>.md
                                       │  stewardq.py tick (every 60 s, launchd or cron)
                                       ▼
                        agentctl.py run / resume ──> claude -p --output-format stream-json
                                       │                     │
            usage.json <── status line │ watchdog (20 s)     │ every event
     (statusline/usage_statusline.py)  ▼                     ▼
                        runs/<id>/state.json, stream.jsonl, log.md, progress.md, crash.md
                                       │
                 wound down or limited └──> queue/pending/<resume entry>.md
```

All state lives under `STEWARD_HOME` (default `~/.steward`):

| Path | What it holds |
| --- | --- |
| `runs/<id>/brief.md` | The brief as launched |
| `runs/<id>/state.json` | Status, session id, pids, resumes, cost, resume time (written under a file lock) |
| `runs/<id>/stream.jsonl` | Every event the session emitted |
| `runs/<id>/log.md` | The readable log, written live |
| `runs/<id>/progress.md` | The agent's own checkpoint: Done, In progress, Next, Files touched, How to verify |
| `runs/<id>/notes.md` | Your notes for the run, quoted to the agent on its next resume or rotation |
| `runs/<id>/crash.md` | Written on any abnormal end |
| `health/usage.json` | The latest usage snapshot from the status line |
| `queue/pending/`, `queue/done/` | Queue entries before and after they start |

The code is three stdlib Python files: `agentctl.py` (the manager), `stewardq.py` (the queue) and `steward_config.py` (paths, clock and binary from environment variables), plus the status line sensor in `statusline/`.

## Requirements

- macOS or Linux. It was built and is used on macOS. On Linux everything works except the desktop notification and the launchd installer (use cron for the tick).
- Python 3.9 or later. No third-party packages.
- [Claude Code](https://docs.claude.com/en/docs/claude-code) installed and logged in, so `claude -p` works in a terminal.
- A Claude subscription for the wind-down. The usage percentages come from the status line document, which carries `rate_limits` for subscription accounts. Without them Steward still runs, caps and resumes, but can only react to a limit hit rather than wind down before it.

## Setup

1. Clone the repository.

   ```bash
   git clone https://github.com/Cameron0404/steward.git
   cd steward
   ```

2. Run the setup script. It checks Python and the `claude` CLI, creates `~/.steward` with `runs/`, `health/` and `queue/`, copies `.env.example` to `~/.steward/.env`, and runs the tests.

   ```bash
   ./setup.sh
   ```

3. Connect the usage sensor. Run `./setup.sh --statusline` to print the snippet, then add it to `~/.claude/settings.json`:

   ```json
   "statusLine": {
     "type": "command",
     "command": "python3 /path/to/steward/statusline/usage_statusline.py"
   }
   ```

   From then on any open interactive Claude Code session writes `~/.steward/health/usage.json` on every update. Check it with `python3 agentctl.py usage`.

4. Start the queue tick. On macOS, `./setup.sh --launchd` installs a launchd job that runs `stewardq.py tick` every 60 seconds. Elsewhere, add a cron line:

   ```
   * * * * * STEWARD_HOME=$HOME/.steward python3 /path/to/steward/stewardq.py tick
   ```

5. Optionally, install the `/agents` skill so an interactive Claude Code session knows how to drive Steward: `./setup.sh --skill`, then set `STEWARD_DIR` to the clone's path in your shell profile.

6. Try one real run. This spends a few cents on Haiku:

   ```bash
   STEWARD_LIVE=1 ./run_tests.sh
   ```

## Configuration

Settings are environment variables, read from your shell or from `~/.steward/.env`. Every value in `.env.example` is a placeholder or the default.

| Variable | Default | What it sets |
| --- | --- | --- |
| `STEWARD_HOME` | `~/.steward` | Where runs, health files and the queue live |
| `STEWARD_TZ` | the machine's zone | Time zone for timestamps |
| `STEWARD_CLAUDE` | `claude` on PATH | The Claude Code binary |
| `STEWARD_WORKDIR` | the current directory | The session's working directory when a run names no repo |
| `STEWARD_THRESHOLD` | 90 | Wind-down point, % of the five-hour window (also `health/threshold`) |
| `STEWARD_WEEKLY_WINDDOWN` | 95 | Wind-down point, % of the seven-day window |
| `STEWARD_WEEKLY_NO_NEW_RUNS` | 92 | Above this weekly %, no new non-Haiku run starts |
| `STEWARD_MAX_CONCURRENT` | 6 | Managed runs at once (also `{"slots": N}` in `health/capacity.json`) |
| `STEWARD_BUDGET_USD` | 25 | Dollar ceiling for one launch, 0 for none |
| `STEWARD_ROTATE_TOKENS` | 130000 | Context size that triggers a rotation |
| `STEWARD_NUDGE_MINUTES` | 20 | Minutes without a checkpoint write before the one nudge, 0 for off |
| `STEWARD_MODEL_LADDER` | Opus 5.5, Sonnet 5, Haiku 4.5 | Fallback models when one runs out of credits |
| `STEWARD_PERMISSION_MODE` | `bypassPermissions` | Permission mode for headless runs |
| `STEWARD_SETTINGS` | none | A Claude Code settings file (deny rules, hooks) for every run |
| `STEWARD_NOTIFY_CMD` | none | A command called with text, note and subject when a run needs a person |
| `STEWARD_STYLE_FILE` | `~/.steward/style.md` | Optional house style quoted into every prompt |
| `STEWARD_STRETCH_FILE` | `~/.steward/stretch.md` | Optional standing goal quoted into every prompt |

Three files in `~/.steward/health/` change behaviour without a restart. `threshold-override` holds one number that replaces the per-run wind-down point for every run. `no-time-limit`, while it exists, lifts the time caps. `capacity.json` sets the run cap.

A brief can name its own working directory on a line of its own: `**Repo: ~/code/my-project**`. `--repo` on the command line wins over it.

## Usage

```bash
# Run a brief now, in the foreground, with its log streaming to runs/<id>/log.md
python3 agentctl.py run --id add-json-flag --brief examples/build-brief.md --model sonnet --max-minutes 60

# Or queue it for tonight (03:30), a time, or a delay
python3 stewardq.py add --at tonight --title "Add JSON flag" --file examples/build-brief.md
python3 stewardq.py add --at +2h --title "Add JSON flag" --file examples/build-brief.md
python3 stewardq.py list

# Every run, newest first, with the usage the watchdog sees
python3 agentctl.py status

# Follow one run's readable log
python3 agentctl.py logs add-json-flag --follow

# Everything about one run on one screen: state, checkpoint, last log lines, resume command
python3 agentctl.py report add-json-flag

# Wind a run down by hand, with its resume queued for 14:35 (or an hour from now)
python3 agentctl.py pause add-json-flag --at 14:35

# Continue a dormant, limited or timed-out run with its conversation intact
python3 agentctl.py resume add-json-flag

# Stop a run for good, recording the outcome
python3 agentctl.py kill add-json-flag

# Archive finished runs older than 14 days and drop their raw streams
python3 agentctl.py gc
```

`status` looks like this:

```
usage: 41% of the 5-hour window, resets 16:00 (3 min old, fresh)
id                               status     updated          resumes cost    reason
add-json-flag                    dormant    01 Oct 14:02     1       $3.18   usage at 90% (threshold 90%)  resume 16:02
nightly-tidy                     done       01 Oct 03:52     0       $0.41   finished
```

## Tests

```bash
./run_tests.sh
```

This runs the manager's selftest (88 checks), the queue's (20) and the status line sensor's (4) against a temporary `STEWARD_HOME`. None of them launches a session or needs the `claude` CLI. `STEWARD_LIVE=1 ./run_tests.sh` adds one real Haiku run of `examples/hello-brief.md`, and is skipped cleanly when `claude` is not installed.

## Limitations

- The wind-down depends on the status line. Claude Code writes the usage document only while an interactive session is open, so with none open the reading goes stale after 45 minutes and Steward falls back to reacting to a limit message, then re-queues the run for the reset.
- The usage fields and CLI flags it relies on (`rate_limits` in the status line document, `--max-budget-usd`, `--exclude-dynamic-system-prompt-sections`, `--disable-slash-commands`) belong to recent Claude Code versions and may change. Set `STEWARD_LEAN_FLAGS=0` if your version rejects the last two.
- Runs use `bypassPermissions` by default, because nobody is there to answer a prompt. Give each run a narrow brief and a working directory you are happy for it to change, and use `STEWARD_SETTINGS` for deny rules.
- Cost for a run that ends without a result event is an estimate from the stream's token counts at list prices, flagged `cost_estimated`.
- Rotation can cut off work in flight, such as a subagent's result or a long test run, so the checkpoint has to carry decisions, file paths and verify commands. The prompt tells the agent so.
- The desktop notification is macOS only, and `setup.sh --launchd` is macOS only.

## Licence

MIT. See [LICENSE](LICENSE).

Built by Cameron Mills. I designed the system and its checks, and Claude Code agents wrote the code to my specification.
