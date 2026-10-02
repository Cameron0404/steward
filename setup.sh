#!/bin/bash
# setup.sh - install Steward for the current user.
#
#   ./setup.sh                 check requirements, create $STEWARD_HOME, copy .env.example, run the tests
#   ./setup.sh --statusline    also print the settings.json snippet for the usage sensor
#   ./setup.sh --launchd       also install a launchd job that runs the queue tick every minute (macOS)
#   ./setup.sh --skill         also copy the /agents skill into ~/.claude/skills/
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="${STEWARD_HOME:-$HOME/.steward}"
PY="$(command -v python3 || true)"

say() { printf '%s\n' "$*"; }

[ -n "$PY" ] || { say "python3 not found. Install Python 3.9 or later."; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || { say "Python 3.9 or later is needed, found $("$PY" --version)."; exit 1; }
if command -v claude >/dev/null 2>&1; then
  say "claude CLI: $(claude --version 2>/dev/null | head -1)"
else
  say "claude CLI not on PATH. Install Claude Code and log in before launching runs (set STEWARD_CLAUDE if it lives elsewhere)."
fi

mkdir -p "$HOME_DIR/runs" "$HOME_DIR/health" "$HOME_DIR/queue/pending" "$HOME_DIR/queue/done"
if [ ! -f "$HOME_DIR/.env" ]; then
  sed "s#^STEWARD_HOME=.*#STEWARD_HOME=$HOME_DIR#" "$DIR/.env.example" > "$HOME_DIR/.env"
  say "wrote $HOME_DIR/.env from .env.example (edit it to change thresholds)"
fi
say "state directory: $HOME_DIR"

"$DIR/run_tests.sh"

for arg in "$@"; do
  case "$arg" in
    --statusline)
      say ""
      say "Add this to ~/.claude/settings.json so interactive sessions feed the usage sensor:"
      say "  \"statusLine\": {\"type\": \"command\", \"command\": \"STEWARD_HOME=$HOME_DIR $PY $DIR/statusline/usage_statusline.py\"}"
      ;;
    --launchd)
      [ "$(uname)" = "Darwin" ] || { say "--launchd is macOS only. Use cron: * * * * * STEWARD_HOME=$HOME_DIR $PY $DIR/stewardq.py tick"; continue; }
      dest="$HOME/Library/LaunchAgents/com.example.steward.tick.plist"
      sed -e "s#@@PYTHON@@#$PY#" -e "s#@@STEWARD_DIR@@#$DIR#" -e "s#@@STEWARD_HOME@@#$HOME_DIR#g" \
        "$DIR/launchd/com.example.steward.tick.plist" > "$dest"
      launchctl unload "$dest" 2>/dev/null || true
      launchctl load "$dest"
      say "installed and loaded $dest (queue tick every 60 s)"
      ;;
    --skill)
      mkdir -p "$HOME/.claude/skills/agents"
      cp "$DIR/skills/agents/SKILL.md" "$HOME/.claude/skills/agents/SKILL.md"
      say "copied the /agents skill to ~/.claude/skills/agents/ (set STEWARD_DIR=$DIR in your shell)"
      ;;
  esac
done
