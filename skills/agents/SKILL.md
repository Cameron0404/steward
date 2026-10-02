---
name: agents
description: Launch, watch, pause, resume and report on managed headless Claude Code runs (Steward's agentctl.py), including what to do when the usage limit is near.
---

# Managed agents

You run the shift for unattended agents: each starts on time, stays inside the usage window and
ends with a report. You know where every run is, why it is there, and when it lands.

Set `STEWARD_DIR` to where you cloned Steward. Every unattended `claude -p` goes through
`$STEWARD_DIR/agentctl.py`. Use this skill when asked to "run this as an agent", "what are the
agents doing", "pause the agents", "why did the build stop", or "resume it".

- **Queue** a brief to start at a set time: `python3 $STEWARD_DIR/stewardq.py add --at now --title "..." --file brief.md`
  (or `--at tonight`, `--at 14:30`, `--at +2h`). The queue tick starts it within a minute.
- **Run now** in the foreground of a terminal: `python3 $STEWARD_DIR/agentctl.py run --id <id> --brief brief.md [--model sonnet] [--max-minutes 90] [--repo DIR]`.
- **Watch**: `agentctl.py status` (every run and the usage now), `agentctl.py logs <id> --follow`.
- **Pause** (wind down, keep the session): `agentctl.py pause <id> [--at 14:35]`. The watchdog
  does this by itself at the threshold when `$STEWARD_HOME/health/usage.json` is fresh.
- **Resume**: `agentctl.py resume <id>`. Normally the queue does it at the reset time.
- **Crash**: read `$STEWARD_HOME/runs/<id>/crash.md`, then `agentctl.py report <id>`.
- **Notes for a run**: write them in `$STEWARD_HOME/runs/<id>/notes.md`. The next resume or
  rotation quotes them to the agent.
- **Usage sensor**: the status line of an open interactive session writes usage.json. With no
  interactive session open the sensor goes stale and the manager can only react to a limit hit
  (the run is re-queued for the reset time either way).

Stop a managed run with `agentctl.py kill <id>`, which records the outcome, rather than killing
the process by hand.
