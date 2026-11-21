"""launchd plists (``ops.launchd``) and ``salesfinder ops plists``. Nothing here runs launchctl,
plutil or a Claude CLI: plists are rendered to bytes or into tmp dirs, and the "Claude CLI" is
an executable stub under tmp_path that is only located, never run."""

import json
import os
import plistlib
import re
import shlex
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from conftest import TEST_SECRET_KEY, clear_salesfinder_env
from salesfinder.cli import app
from salesfinder.ops import launchd
from salesfinder.settings import get_settings

runner = CliRunner()
REPO = Path(__file__).resolve().parent.parent
EXAMPLE_REPO = Path("/Users/YOU/SalesFinder")
EXAMPLE_LOGS = Path("/Users/YOU/Library/Logs/SalesFinder")
EXAMPLE_CLAUDE = Path("/Users/YOU/.local/bin")
SYSTEM_PATH = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]


def _load(kind: launchd.Kind, *, claude_dir: Path | None = EXAMPLE_CLAUDE) -> dict[str, object]:
    data = launchd.render(kind, repo=EXAMPLE_REPO, log_dir=EXAMPLE_LOGS, claude_dir=claude_dir)
    loaded = plistlib.loads(data)
    assert isinstance(loaded, dict)
    return loaded


@pytest.mark.parametrize(("kind", "exit_timeout"), [("web", 30), ("worker", 600)])
def test_render_has_the_keys_and_values(kind: launchd.Kind, exit_timeout: int) -> None:
    plist = _load(kind)
    assert plist == {
        "Label": f"com.elchemy.salesfinder.{kind}",
        "ProgramArguments": ["/Users/YOU/SalesFinder/.venv/bin/salesfinder", kind],
        "WorkingDirectory": "/Users/YOU/SalesFinder",
        "EnvironmentVariables": {
            "PATH": ":".join(
                ["/Users/YOU/.local/bin", "/Users/YOU/SalesFinder/.venv/bin", *SYSTEM_PATH]
            ),
            "LANG": "en_US.UTF-8",
            "SALESFINDER_LOG_FILE": f"/Users/YOU/Library/Logs/SalesFinder/{kind}.log",
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "ProcessType": "Standard",
        "ExitTimeOut": exit_timeout,
        # launchd holds these open for the process's life: crash output only (Ruling P7-F).
        "StandardOutPath": f"/Users/YOU/Library/Logs/SalesFinder/{kind}.crash.log",
        "StandardErrorPath": f"/Users/YOU/Library/Logs/SalesFinder/{kind}.crash.log",
    }


def test_labels() -> None:
    assert launchd.LABELS == {
        "web": "com.elchemy.salesfinder.web",
        "worker": "com.elchemy.salesfinder.worker",
    }


def test_worker_exit_timeout_outlasts_a_stopping_pass() -> None:
    """Ruling P7-G: on SIGTERM the worker's Claude steps start nothing new, so the pass ends
    within one reply's worst case (4 Claude calls x brain.timeout_seconds); launchd must wait
    longer than that before SIGKILL."""
    from salesfinder.config import STOP_MARGIN_SECONDS, BrainConfig

    assert _load("worker")["ExitTimeOut"] == 600
    assert 600 > 4 * BrainConfig().timeout_seconds + STOP_MARGIN_SECONDS


def _path_of(plist: dict[str, object]) -> list[str]:
    env = plist["EnvironmentVariables"]
    assert isinstance(env, dict)
    path = env["PATH"]
    assert isinstance(path, str)
    return path.split(":")


def test_path_starts_with_the_claude_dir_and_has_no_duplicates() -> None:
    entries = _path_of(_load("worker"))
    assert entries[0] == str(EXAMPLE_CLAUDE)
    assert len(entries) == len(set(entries))


def test_path_dedupes_a_claude_dir_already_on_the_system_path() -> None:
    entries = _path_of(_load("worker", claude_dir=Path("/opt/homebrew/bin")))
    assert entries == ["/opt/homebrew/bin", "/Users/YOU/SalesFinder/.venv/bin", *SYSTEM_PATH[1:]]


def test_path_without_a_claude_dir() -> None:
    entries = _path_of(_load("web", claude_dir=None))
    assert entries == ["/Users/YOU/SalesFinder/.venv/bin", *SYSTEM_PATH]


def _salesfinder_vars(data: bytes) -> set[str]:
    return set(re.findall(r"SALESFINDER_[A-Z_]+", data.decode("utf-8")))


@pytest.mark.parametrize("kind", ["web", "worker"])
def test_the_only_salesfinder_variable_is_the_log_file(kind: launchd.Kind) -> None:
    """The environment is PATH, LANG and SALESFINDER_LOG_FILE (not a secret; Ruling P7-F):
    everything else, secrets above all, comes from .env in the working directory."""
    env = _load(kind)["EnvironmentVariables"]
    assert isinstance(env, dict)
    assert set(env) == {"PATH", "LANG", "SALESFINDER_LOG_FILE"}
    data = launchd.render(kind, repo=EXAMPLE_REPO, log_dir=EXAMPLE_LOGS, claude_dir=EXAMPLE_CLAUDE)
    assert _salesfinder_vars(data) == {"SALESFINDER_LOG_FILE"}


def test_paths_with_spaces_and_markup_characters_round_trip() -> None:
    """A folder named like "Sales & Co <test>" is escaped in the XML and loads back as is."""
    repo = Path("/Users/Jo Doe/Sales & Co <test>")
    logs = Path("/Users/Jo Doe/Library/Logs/Sales & Co")
    claude = Path("/Users/Jo Doe/.local/bin")
    data = launchd.render("worker", repo=repo, log_dir=logs, claude_dir=claude)
    assert b"<test>" not in data and b"&lt;test&gt;" in data and b"&amp;" in data
    plist = plistlib.loads(data)
    assert plist["ProgramArguments"] == [str(repo / ".venv" / "bin" / "salesfinder"), "worker"]
    assert plist["WorkingDirectory"] == str(repo)
    assert plist["EnvironmentVariables"]["PATH"].split(":")[:2] == [
        str(claude),
        str(repo / ".venv" / "bin"),
    ]
    assert plist["EnvironmentVariables"]["SALESFINDER_LOG_FILE"] == str(logs / "worker.log")
    assert plist["StandardErrorPath"] == str(logs / "worker.crash.log")


@pytest.mark.parametrize("kind", ["web", "worker"])
def test_tracked_examples_match_the_render_function(kind: launchd.Kind) -> None:
    """deploy/launchd/ holds what ``render`` makes for a placeholder user (drift check)."""
    example = REPO / "deploy" / "launchd" / f"{launchd.LABELS[kind]}.plist"
    assert example.read_bytes() == launchd.render(
        kind, repo=EXAMPLE_REPO, log_dir=EXAMPLE_LOGS, claude_dir=EXAMPLE_CLAUDE
    )


def test_tracked_examples_are_the_only_plists() -> None:
    found = sorted(p.name for p in (REPO / "deploy" / "launchd").glob("*.plist"))
    assert found == sorted(f"{label}.plist" for label in launchd.LABELS.values())


def _executable(path: Path, text: str = "#!/bin/sh\nexit 99\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_claude_dir_is_the_folder_of_the_found_cli(tmp_path: Path) -> None:
    stub = _executable(tmp_path / "bin" / "claude")
    assert launchd.claude_dir(str(stub)) == tmp_path / "bin"


def test_claude_dir_none_when_not_found(tmp_path: Path) -> None:
    assert launchd.claude_dir(str(tmp_path / "missing" / "claude")) is None
    assert launchd.claude_dir("salesfinder-test-no-claude") is None


# --- the CLI ------------------------------------------------------------------------------

MAILBOX_PASSWORD = "qrst uvwx yzab cdef"  # noqa: S105 - a test value


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """A stand-in SalesFinder folder as cwd (no .env), with stub pyproject.toml and
    .venv/bin/salesfinder, and SALESFINDER_CLAUDE_BIN an absolute stub under tmp_path."""
    root = tmp_path / "SalesFinder"
    root.mkdir()
    (root / "pyproject.toml").write_text('[project]\nname = "salesfinder"\n', encoding="utf-8")
    _executable(root / ".venv" / "bin" / "salesfinder")
    claude = _executable(tmp_path / "claude-home" / "bin" / "claude")
    monkeypatch.chdir(root)
    # Only the variables set here: the shell's (e.g. SALESFINDER_LOG_FILE) never leak in.
    clear_salesfinder_env(monkeypatch)
    monkeypatch.setenv(
        "SALESFINDER_DATABASE_URL", "postgresql+asyncpg://localhost/salesfinder_test"
    )
    monkeypatch.setenv("SALESFINDER_SECRET_KEY", TEST_SECRET_KEY)
    monkeypatch.setenv("SALESFINDER_CLAUDE_BIN", str(claude))
    monkeypatch.setenv("SALESFINDER_MAILBOX_PASSWORDS", json.dumps({"sales_box": MAILBOX_PASSWORD}))
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


def test_cli_prints_both_plists_without_out(repo: Path, tmp_path: Path) -> None:
    result = runner.invoke(app, ["ops", "plists"])
    assert result.exit_code == 0, result.output
    for label in launchd.LABELS.values():
        assert f"<string>{label}</string>" in result.output
    assert f"<string>{repo / '.venv' / 'bin' / 'salesfinder'}</string>" in result.output
    assert f"{tmp_path / 'claude-home' / 'bin'}:" in result.output
    assert TEST_SECRET_KEY not in result.output
    assert MAILBOX_PASSWORD not in result.output
    assert _salesfinder_vars(result.output.encode()) == {"SALESFINDER_LOG_FILE"}


def test_cli_writes_both_files_and_refuses_to_overwrite(repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "LaunchAgents"
    logs = tmp_path / "Logs" / "SalesFinder"
    args = ["ops", "plists", "--out", str(out), "--log-dir", str(logs)]

    result = runner.invoke(app, args)
    assert result.exit_code == 0, result.output
    assert logs.is_dir()
    uid = os.getuid()
    for kind, label in launchd.LABELS.items():
        written = out / f"{label}.plist"
        assert written.read_bytes() == launchd.render(
            kind,  # type: ignore[arg-type]
            repo=repo,
            log_dir=logs,
            claude_dir=tmp_path / "claude-home" / "bin",
        )
        data = written.read_bytes()
        assert TEST_SECRET_KEY.encode() not in data
        assert MAILBOX_PASSWORD.encode() not in data
        assert _salesfinder_vars(data) == {"SALESFINDER_LOG_FILE"}
        assert f"launchctl bootstrap gui/{uid} {shlex.quote(str(written))}" in result.output

    again = runner.invoke(app, args)
    assert again.exit_code == 1
    first = out / f"{launchd.LABELS['web']}.plist"
    assert f"{first} exists; re-run with --force." in again.output

    forced = runner.invoke(app, [*args, "--force"])
    assert forced.exit_code == 0, forced.output


def test_cli_quotes_the_printed_bootstrap_paths(repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "Launch Agents & co"
    result = runner.invoke(app, ["ops", "plists", "--out", str(out), "--log-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    line = next(
        x for x in result.output.splitlines() if "salesfinder.web.plist" in x and "boot" in x
    )
    assert shlex.split(line)[-1] == str(out / "com.elchemy.salesfinder.web.plist")


def test_cli_refuses_outside_a_synced_salesfinder_folder(repo: Path) -> None:
    (repo / ".venv" / "bin" / "salesfinder").unlink()
    result = runner.invoke(app, ["ops", "plists"])
    assert result.exit_code == 1
    assert "Run this from the SalesFinder folder after `uv sync`." in result.output


def test_cli_refuses_without_pyproject(repo: Path) -> None:
    (repo / "pyproject.toml").unlink()
    result = runner.invoke(app, ["ops", "plists"])
    assert result.exit_code == 1
    assert "Run this from the SalesFinder folder after `uv sync`." in result.output


def test_cli_refuses_when_the_claude_cli_is_not_found(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SALESFINDER_CLAUDE_BIN", str(tmp_path / "nowhere" / "claude"))
    get_settings.cache_clear()
    result = runner.invoke(app, ["ops", "plists"])
    assert result.exit_code == 1
    assert "Claude CLI not found: set SALESFINDER_CLAUDE_BIN to its absolute path." in result.output


# --- the Makefile targets (parsed only; never run) -------------------------------------------


def _recipe(target: str) -> list[str]:
    lines = (REPO / "Makefile").read_text(encoding="utf-8").splitlines()
    start = lines.index(f"{target}:")
    recipe: list[str] = []
    for line in lines[start + 1 :]:
        if not line.startswith("\t"):
            break
        recipe.append(line.strip())
    return recipe


def test_make_targets_drive_both_agents_in_order() -> None:
    assert _recipe("plists") == ["uv run salesfinder ops plists --out $(LAUNCH_AGENTS)"]
    assert _recipe("start") == [
        "launchctl bootstrap $(GUI) $(LAUNCH_AGENTS)/$(WEB_LABEL).plist",
        "launchctl bootstrap $(GUI) $(LAUNCH_AGENTS)/$(WORKER_LABEL).plist",
    ]
    # The worker stops first (it finishes its pass), then both are waited for.
    assert _recipe("stop") == [
        "-launchctl bootout $(GUI)/$(WORKER_LABEL)",
        "-launchctl bootout $(GUI)/$(WEB_LABEL)",
        "@$(WAIT_UNLOADED)",
        # Final wave T7: only once both are unloaded (nothing holds the crash logs).
        "@$(CLEAR_CRASH_LOGS)",
    ]
    # Ruling P7-H: restart is stop then start, always both (bootstrap re-reads the plists).
    assert _recipe("restart") == _recipe("stop") + _recipe("start")
    assert _recipe("logs") == [
        "tail -F $(LOG_DIR)/web.log $(LOG_DIR)/worker.log"
        " $(LOG_DIR)/web.crash.log $(LOG_DIR)/worker.crash.log"
    ]
    assert _recipe("e2e") == ["uv run pytest tests/test_e2e.py -q"]


def test_stop_keeps_only_the_previous_crash_logs(tmp_path: Path) -> None:
    """Final wave T7: launchd appends to the crash logs for as long as an agent is loaded, so
    ``make stop`` (after both are unloaded) moves each non-empty one to ``*.crash.log.prev``,
    replacing the older copy: at most two of each, and the last crash output is kept. The
    Makefile's own shell snippet runs here against a temp folder (never launchctl)."""
    import subprocess

    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    line = next(x for x in makefile.splitlines() if x.startswith("CLEAR_CRASH_LOGS := "))
    script = line.removeprefix("CLEAR_CRASH_LOGS := ").replace("$(LOG_DIR)", str(tmp_path))
    script = script.replace("$$", "$")
    (tmp_path / "web.crash.log").write_text("new web traceback\n")
    (tmp_path / "web.crash.log.prev").write_text("older\n")
    (tmp_path / "worker.crash.log").write_text("")  # empty: left alone

    subprocess.run(["/bin/sh", "-c", script], check=True)  # noqa: S603

    assert not (tmp_path / "web.crash.log").exists()
    assert (tmp_path / "web.crash.log.prev").read_text() == "new web traceback\n"
    assert (tmp_path / "worker.crash.log").exists()
    assert not (tmp_path / "worker.crash.log.prev").exists()
    subprocess.run(["/bin/sh", "-c", script], check=True)  # noqa: S603 - nothing to move: fine
    assert (tmp_path / "web.crash.log.prev").read_text() == "new web traceback\n"


def test_makefile_labels_and_log_dir_match_the_render_function() -> None:
    makefile = (REPO / "Makefile").read_text(encoding="utf-8")
    assert f"WEB_LABEL := {launchd.LABELS['web']}\n" in makefile
    assert f"WORKER_LABEL := {launchd.LABELS['worker']}\n" in makefile
    assert "LOG_DIR := $(HOME)/Library/Logs/SalesFinder\n" in makefile
    # The wait outlasts the worker's ExitTimeOut and checks both labels.
    wait = next(x for x in makefile.splitlines() if x.startswith("WAIT_UNLOADED := "))
    assert "seq 660" in wait and 660 > _load("worker")["ExitTimeOut"]  # type: ignore[operator]
    assert "$(WORKER_LABEL)" in wait and "$(WEB_LABEL)" in wait
    assert "kickstart" not in makefile
    assert launchd.default_log_dir() == Path.home() / "Library" / "Logs" / "SalesFinder"
