"""launchd LaunchAgents that keep the web app and the worker running on the office Mac.

LaunchAgents, not LaunchDaemons: the Claude CLI's subscription login lives in the operator's
user session. Each agent runs ``<repo>/.venv/bin/salesfinder <kind>`` directly (``uv run`` may
re-sync or reach the network at start) from the repo, so ``.env`` is read from the working
directory as always. A plist carries no secret. Its environment is only ``PATH`` (launchd's
default lacks ``~/.local/bin`` and Homebrew, where the Claude CLI lives), ``LANG`` and
``SALESFINDER_LOG_FILE`` (the one ``SALESFINDER_*`` variable, not a secret; Ruling P7-F): the
app writes its log there and reopens it after newsyslog rotates it. launchd's own
StandardOut/ErrorPath, a file launchd holds open for the process's life, get only what bypasses
logging (a crash's traceback): ``<kind>.crash.log``.

Rendering never runs anything: ``claude_dir`` only locates the CLI, and loading the plists
(``launchctl bootstrap``) is the operator's step (``make start``)."""

import os
import plistlib
import shutil
from pathlib import Path
from typing import Literal

from salesfinder.config import WORKER_EXIT_TIMEOUT_SECONDS

__all__ = [
    "LABELS",
    "LOG_FILE_VAR",
    "Kind",
    "claude_dir",
    "crash_log",
    "default_log_dir",
    "log_file",
    "path_for",
    "render",
]

Kind = Literal["web", "worker"]

LABELS: dict[Kind, str] = {
    "web": "com.elchemy.salesfinder.web",
    "worker": "com.elchemy.salesfinder.worker",
}

# The rest of PATH after the Claude CLI's folder and the venv (Homebrew on Apple silicon, then
# Intel, then the system's).
_SYSTEM_PATH = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")

# Seconds launchd waits after SIGTERM before SIGKILL. On SIGTERM the worker finishes its
# current pass, but its Claude steps and Discovery start no new item (Ruling P7-G), so the pass
# ends within about one Claude call's worst case (a reply: up to 4 x brain.timeout_seconds,
# 480 s by default). A kill mid-pass is still crash-safe (the run is closed as "crashed").
# brain.timeout_seconds is bounded so that worst case fits (config.BrainConfig).
_EXIT_TIMEOUT: dict[Kind, int] = {"web": 30, "worker": WORKER_EXIT_TIMEOUT_SECONDS}

LOG_FILE_VAR = "SALESFINDER_LOG_FILE"


def default_log_dir() -> Path:
    return Path.home() / "Library" / "Logs" / "SalesFinder"


def log_file(log_dir: Path, kind: Kind) -> Path:
    """Where the app writes its log (``SALESFINDER_LOG_FILE``)."""
    return log_dir / f"{kind}.log"


def crash_log(log_dir: Path, kind: Kind) -> Path:
    """launchd's StandardOut/ErrorPath: only output that bypasses logging (a traceback)."""
    return log_dir / f"{kind}.crash.log"


def path_for(*, repo: Path, claude_dir: Path | None) -> str:
    """The agent's PATH: the Claude CLI's folder, the venv's bin, then the usual system folders
    (duplicates dropped, first one kept)."""
    entries = [
        *([str(claude_dir)] if claude_dir is not None else []),
        str(repo / ".venv" / "bin"),
        *_SYSTEM_PATH,
    ]
    return ":".join(dict.fromkeys(entries))


def render(kind: Kind, *, repo: Path, log_dir: Path, claude_dir: Path | None) -> bytes:
    """The LaunchAgent plist (XML) for ``salesfinder web`` or ``salesfinder worker``."""
    crash = str(crash_log(log_dir, kind))
    plist: dict[str, object] = {
        "Label": LABELS[kind],
        "ProgramArguments": [str(repo / ".venv" / "bin" / "salesfinder"), kind],
        "WorkingDirectory": str(repo),
        "EnvironmentVariables": {
            "PATH": path_for(repo=repo, claude_dir=claude_dir),
            "LANG": "en_US.UTF-8",
            LOG_FILE_VAR: str(log_file(log_dir, kind)),
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "ProcessType": "Standard",
        "ExitTimeOut": _EXIT_TIMEOUT[kind],
        "StandardOutPath": crash,
        "StandardErrorPath": crash,
    }
    return plistlib.dumps(plist, fmt=plistlib.FMT_XML, sort_keys=False)


def claude_dir(claude_bin: str) -> Path | None:
    """The folder holding the Claude CLI that ``claude_bin`` names (a name on this shell's PATH
    or a path), or None if it isn't found. Never runs it. The folder is kept as found, not
    resolved through symlinks: ``~/.local/bin/claude`` points into a versioned folder that
    changes with every ``claude update``."""
    found = shutil.which(claude_bin)
    if found is None:
        return None
    return Path(os.path.abspath(found)).parent
