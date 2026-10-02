"""steward_config.py - where Steward keeps its state, and which clock and binary it uses.

Everything is an environment variable, so nothing personal is ever written into the code:

  STEWARD_HOME      state directory (runs/, health/, queue/). Default ~/.steward
  STEWARD_TZ        IANA zone for timestamps, e.g. Europe/London. Default: the machine's zone
  STEWARD_CLAUDE    path to the `claude` binary. Default: `claude` on PATH, else ~/.local/bin/claude
  STEWARD_WORKDIR   the session's working directory when a run names no repo. Default: the
                    directory agentctl.py was started from

A `.env` file in STEWARD_HOME (KEY=value lines) is read too, without overriding the environment.
"""
from __future__ import annotations

import datetime as dt
import os
import shutil
from pathlib import Path


def _load_env_file(path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


ROOT = Path(os.environ.get("STEWARD_HOME") or (Path.home() / ".steward")).expanduser()
_load_env_file(ROOT / ".env")


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name) or default)
    except ValueError:
        return float(default)


def _zone():
    name = os.environ.get("STEWARD_TZ")
    if name:
        try:
            import zoneinfo
            return zoneinfo.ZoneInfo(name)
        except Exception:  # noqa: BLE001
            pass
    try:
        import zoneinfo
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            return zoneinfo.ZoneInfo(link.split("zoneinfo/", 1)[1])
    except Exception:  # noqa: BLE001
        pass
    return dt.datetime.now().astimezone().tzinfo


TZ = _zone()


def _claude() -> Path:
    env = os.environ.get("STEWARD_CLAUDE")
    if env:
        return Path(env).expanduser()
    found = shutil.which("claude")
    return Path(found) if found else Path.home() / ".local" / "bin" / "claude"


CLAUDE = _claude()
DEFAULT_WORKDIR = Path(os.environ.get("STEWARD_WORKDIR") or os.getcwd()).expanduser()
