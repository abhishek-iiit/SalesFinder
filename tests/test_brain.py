import asyncio
import logging
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from structlog.testing import capture_logs

from conftest import FAKE_CLAUDE, FakeClaude
from salesfinder.brain.budget import calls_today, record_usage, remaining_calls
from salesfinder.brain.json_task import REPAIR_SUFFIX, complete_json, extract_json_object
from salesfinder.brain.runtime import (
    STRIPPED_ENV_VARS,
    BrainError,
    BrainResult,
    BrainUnavailable,
    ClaudeRuntime,
    isolated_env,
)
from salesfinder.config import AppConfig, BrainConfig, UsageConfig
from salesfinder.db.engine import make_session_factory
from salesfinder.db.models import UsageEvent
from salesfinder.settings import Settings

REPO = Path(__file__).resolve().parent.parent
ISOLATION_ARGV = [
    "-p",
    "--output-format",
    "json",
    "--tools",
    "",
    "--strict-mcp-config",
    "--safe-mode",
    "--setting-sources",
    "",
    "--disable-slash-commands",
    "--no-session-persistence",
]
UNEXPECTED = "Claude returned an unexpected response."


def ok_reply(text: str, **extra: Any) -> dict[str, Any]:
    return {"type": "result", "subtype": "success", "is_error": False, "result": text, **extra}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat = subprocess.run(  # noqa: S603
        ["ps", "-o", "stat=", "-p", str(pid)],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    return bool(stat) and not stat.startswith("Z")


def _real(path: str) -> str:
    return os.path.realpath(path)


def _exists(path: str) -> bool:
    return Path(path).exists()


async def _wait_dead(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        await asyncio.sleep(0.05)
    return not _alive(pid)


@pytest.fixture
def runtime(test_settings_with_fake_claude: Settings) -> ClaudeRuntime:
    return ClaudeRuntime(test_settings_with_fake_claude, BrainConfig())


# --- runtime: isolation ---------------------------------------------------------------------


async def test_isolated_argv_env_cwd_stdin(
    runtime: ClaudeRuntime, fake_claude_env: FakeClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    for var in STRIPPED_ENV_VARS:
        monkeypatch.setenv(var, "leak")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("PWD", str(REPO))
    monkeypatch.setenv("CLAUDE_TEST_PASSTHROUGH", "kept")
    monkeypatch.setenv("SALESFINDER_SECRET_KEY", "leak-secret")
    monkeypatch.setenv("SALESFINDER_DATABASE_URL", "postgresql://leak")
    prompt = "Write something about ISOLATION-MARKER.\nLine two."
    fake_claude_env.reply("ISOLATION-MARKER", ok_reply("done"))

    result = await runtime.complete(prompt)

    assert result.text == "done"
    [call] = fake_claude_env.calls()
    assert call["argv"] == ISOLATION_ARGV
    assert call["prompt"] == prompt
    assert all(prompt not in arg and "ISOLATION-MARKER" not in arg for arg in call["argv"])
    cwd = Path(call["cwd"])
    assert cwd.is_absolute()
    assert _real(call["env"]["PWD"]) == _real(call["cwd"])
    assert not _real(call["cwd"]).startswith(str(REPO))
    assert call["cwd_entries"] == []
    assert not _exists(call["cwd"]), "temp dir must be removed after the call"
    env = call["env"]
    assert env["CLAUDE_CODE_SAFE_MODE"] == "1"
    assert env["CLAUDE_TEST_PASSTHROUGH"] == "kept"
    for var in STRIPPED_ENV_VARS:
        assert var not in env, var
    assert "CLAUDECODE" not in env and "OLDPWD" not in env
    assert not [k for k in env if k.upper().startswith("SALESFINDER_")]
    # The --help preflight runs isolated too.
    [help_call] = fake_claude_env.calls("help")
    assert help_call["argv"] == ["--help"]
    assert "CLAUDECODE" not in help_call["env"]
    assert not _exists(help_call["cwd"])


def test_stripped_env_list_is_exact() -> None:
    assert set(STRIPPED_ENV_VARS) == {
        "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD",
        "CLAUDE_CODE_SIMPLE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SSE_PORT",
        "CLAUDE_ENV_FILE",
        "CLAUDE_PROJECT_DIR",
        "CLAUDECODE",
        "OLDPWD",
    }


def test_isolated_env_drops_every_salesfinder_variable() -> None:
    base = {
        "PATH": "/usr/bin",
        "HOME": "/home/op",
        "SALESFINDER_SECRET_KEY": "s",
        "SALESFINDER_HUBSPOT_TOKEN": "t",
        "salesfinder_database_url": "d",
        "SALESFINDERX": "kept",  # no underscore: not ours
        "CLAUDECODE": "1",
        "PWD": "/repo",
    }
    assert isolated_env(base, "/work/x") == {
        "PATH": "/usr/bin",
        "HOME": "/home/op",
        "SALESFINDERX": "kept",
        "PWD": "/work/x",
        "CLAUDE_CODE_SAFE_MODE": "1",
    }


async def test_model_flag(
    test_settings_with_fake_claude: Settings, fake_claude_env: FakeClaude
) -> None:
    runtime = ClaudeRuntime(test_settings_with_fake_claude, BrainConfig(model="claude-sonnet-x"))
    await runtime.complete("hello")
    [call] = fake_claude_env.calls()
    assert call["argv"] == [*ISOLATION_ARGV, "--model", "claude-sonnet-x"]


async def test_help_checked_once_and_process_in_own_session(
    test_settings_with_fake_claude: Settings, fake_claude_env: FakeClaude
) -> None:
    seen: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def spy(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        seen.append((args, kwargs))
        return await asyncio.create_subprocess_exec(*args, **kwargs)

    runtime = ClaudeRuntime(test_settings_with_fake_claude, BrainConfig(), spawn=spy)
    await runtime.complete("one")
    await runtime.complete("two")
    assert [args[1] for args, _ in seen] == ["--help", "-p", "-p"]
    for _, kwargs in seen:
        assert kwargs["start_new_session"] is True
        assert kwargs["cwd"] == kwargs["env"]["PWD"]
        assert kwargs["stdin"] in (asyncio.subprocess.PIPE, asyncio.subprocess.DEVNULL)
    assert len(fake_claude_env.calls("help")) == 1


async def test_refuses_without_flags(
    runtime: ClaudeRuntime, fake_claude_env: FakeClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_DROP", "--safe-mode")
    with pytest.raises(BrainUnavailable) as exc:
        await runtime.complete("hello")
    assert exc.value.reason == (
        "Claude CLI is missing isolation flags: --safe-mode. Run `claude update`."
    )
    assert fake_claude_env.calls() == []
    # Not cached as OK: still refused on the next call.
    with pytest.raises(BrainUnavailable):
        await runtime.complete("hello")
    assert fake_claude_env.calls() == []


async def test_not_found(test_settings: Settings, tmp_path: Path) -> None:
    for claude_bin in ("definitely-not-claude-xyz", str(tmp_path / "nope" / "claude")):
        settings = test_settings.model_copy(update={"claude_bin": claude_bin})
        with pytest.raises(BrainUnavailable) as exc:
            await ClaudeRuntime(settings, BrainConfig()).complete("hello")
        assert exc.value.reason == "Claude CLI not found on PATH."


# --- runtime: failures ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (
            {"type": "result", "is_error": True, "result": "Not logged in"},
            "Claude returned an error: not signed in or quota reached.",
        ),
        ({"exit": 1}, "Claude exited with an error (code 1)."),
        ({"exit": 3}, "Claude exited with an error (code 3)."),
        ({"raw": "garbage, not json"}, UNEXPECTED),
        ({"raw": ""}, UNEXPECTED),
        ({"raw": "[1, 2]"}, UNEXPECTED),
        ({"type": "assistant", "is_error": False, "result": "x"}, UNEXPECTED),
        ({"type": "result", "is_error": False, "result": 5}, UNEXPECTED),
        ({"type": "result", "result": "no is_error"}, UNEXPECTED),
        ({"raw": "x", "repeat": 5 * 1024 * 1024}, UNEXPECTED),
        ({"raw": "[" * 20000 + "]" * 20000}, UNEXPECTED),
    ],
)
async def test_error_shapes(
    runtime: ClaudeRuntime, fake_claude_env: FakeClaude, reply: dict[str, Any], reason: str
) -> None:
    fake_claude_env.reply("MARK", reply)
    with pytest.raises(BrainError) as exc:
        await runtime.complete("MARK")
    assert not isinstance(exc.value, BrainUnavailable)
    assert exc.value.reason == reason
    assert str(exc.value) == reason


async def test_is_error_with_nonzero_exit_reports_signed_out(
    runtime: ClaudeRuntime, fake_claude_env: FakeClaude
) -> None:
    fake_claude_env.reply(
        "MARK", {"type": "result", "is_error": True, "result": "quota", "exit_after": 1}
    )
    with pytest.raises(BrainError) as exc:
        await runtime.complete("MARK")
    assert exc.value.reason == "Claude returned an error: not signed in or quota reached."


async def test_timeout_kills_process_group(
    runtime: ClaudeRuntime, fake_claude_env: FakeClaude
) -> None:
    fake_claude_env.reply("SLOW", {"sleep": 30})
    started = time.monotonic()
    with pytest.raises(BrainError) as exc:
        await runtime.complete("SLOW", timeout=1)
    assert exc.value.reason == "Claude timed out after 1s."
    assert time.monotonic() - started < 10
    [call] = fake_claude_env.calls()
    assert await _wait_dead(call["pid"])
    assert await _wait_dead(call["child_pid"]), "grandchild must die with the process group"
    assert not _exists(call["cwd"])


async def test_timeout_reason_uses_configured_seconds(runtime: ClaudeRuntime) -> None:
    assert BrainConfig().timeout_seconds == 120
    assert BrainError.timed_out(120.0).reason == "Claude timed out after 120s."
    assert BrainError.timed_out(1.5).reason == "Claude timed out after 1.5s."


async def test_cancellation_cleans_up(runtime: ClaudeRuntime, fake_claude_env: FakeClaude) -> None:
    fake_claude_env.reply("SLOW", {"sleep": 30})
    task = asyncio.create_task(runtime.complete("SLOW"))
    for _ in range(200):
        if fake_claude_env.calls():
            break
        await asyncio.sleep(0.05)
    [call] = fake_claude_env.calls()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _wait_dead(call["pid"])
    assert await _wait_dead(call["child_pid"])
    assert not _exists(call["cwd"])


async def test_usage_parsed(runtime: ClaudeRuntime, fake_claude_env: FakeClaude) -> None:
    fake_claude_env.reply(
        "FULL",
        ok_reply(
            "hi there",
            duration_ms=1234,
            total_cost_usd=0.0123,
            usage={"input_tokens": 11, "output_tokens": 7},
            session_id="abc",
        ),
    )
    result = await runtime.complete("FULL")
    assert result.text == "hi there"
    assert result.input_tokens == 11
    assert result.output_tokens == 7
    assert result.cost_usd == Decimal("0.0123")
    assert isinstance(result.duration_ms, int) and result.duration_ms >= 0

    fake_claude_env.reply("BARE", ok_reply("bare", usage={"input_tokens": "x"}))
    bare = await runtime.complete("BARE")
    assert (bare.input_tokens, bare.output_tokens, bare.cost_usd) == (None, None, None)


async def test_prompt_never_logged(
    runtime: ClaudeRuntime,
    fake_claude_env: FakeClaude,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    fake_claude_env.reply("FAILME", {"exit": 2})  # the fake echoes the prompt on stderr
    fake_claude_env.reply("PROMPT-SECRET", ok_reply("RESULT-SECRET"))
    with capture_logs() as logs:
        await runtime.complete("PROMPT-SECRET please")
        with pytest.raises(BrainError):
            await runtime.complete("FAILME PROMPT-SECRET")
    out = capsys.readouterr()
    assert logs, "the runtime should log call metadata"
    blob = repr(logs) + caplog.text + out.out + out.err
    assert "PROMPT-SECRET" not in blob
    assert "RESULT-SECRET" not in blob
    assert any("prompt_chars" in entry for entry in logs)


# --- json tasks + budget --------------------------------------------------------------------


class Pair(BaseModel):
    a: int
    b: str


def _config(cap: int = 150) -> AppConfig:
    return AppConfig(usage=UsageConfig(max_daily_claude_calls=cap))


async def _rows(db: AsyncSession) -> list[UsageEvent]:
    return list((await db.scalars(select(UsageEvent).order_by(UsageEvent.ok))).all())


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ('Sure!\n```json\n{"a": "}"}\n```\nThanks', {"a": "}"}),
        ('x {not json} then {"a": {"b": 2}}', {"a": {"b": 2}}),
        ('{"a": "\\"}"}', {"a": '"}'}),
        ('text {"a": 1} {"b": 2}', {"a": 1}),
        ("[1, 2]", None),
        ("", None),
        ('{"a": 1', None),
        ("no braces at all", None),
    ],
)
def test_extract_json_object(text: str, expected: dict[str, Any] | None) -> None:
    assert extract_json_object(text) == expected


async def test_complete_json_repairs_once(
    runtime: ClaudeRuntime, engine: AsyncEngine, db: AsyncSession, fake_claude_env: FakeClaude
) -> None:
    # Most specific marker first: the repair prompt also contains the original marker.
    fake_claude_env.reply(
        "Reply with only the JSON object.",
        ok_reply('Sure!\n```json\n{"a": 1, "b": "x}"}\n```', total_cost_usd=0.01),
    )
    fake_claude_env.reply("PAIR-TASK", ok_reply('Here:\n```json\n{"a": "nope", "b": 2}\n```'))
    now = datetime.now(UTC)
    pair = await complete_json(
        runtime,
        make_session_factory(engine),
        prompt="PAIR-TASK give me a pair",
        schema=Pair,
        task="test_pair",
        now=now,
        config=_config(),
    )
    assert pair == Pair(a=1, b="x}")
    first, second = fake_claude_env.calls()
    assert first["prompt"] == "PAIR-TASK give me a pair"
    assert second["prompt"] == "PAIR-TASK give me a pair" + REPAIR_SUFFIX
    rows = await _rows(db)
    assert [r.ok for r in rows] == [False, True]
    assert {r.task for r in rows} == {"test_pair"}
    assert rows[0].error == UNEXPECTED and rows[0].cost_usd is None
    assert rows[1].error is None and rows[1].cost_usd == Decimal("0.0100")
    assert all(r.duration_ms >= 0 for r in rows)


async def test_complete_json_gives_up(
    runtime: ClaudeRuntime, engine: AsyncEngine, db: AsyncSession, fake_claude_env: FakeClaude
) -> None:
    fake_claude_env.reply("PAIR-TASK", ok_reply("I can't do JSON today."))
    with pytest.raises(BrainError) as exc:
        await complete_json(
            runtime,
            make_session_factory(engine),
            prompt="PAIR-TASK",
            schema=Pair,
            task="test_pair",
            now=datetime.now(UTC),
            config=_config(),
        )
    assert exc.value.reason == UNEXPECTED
    assert len(fake_claude_env.calls()) == 2
    rows = await _rows(db)
    assert [(r.ok, r.error) for r in rows] == [(False, UNEXPECTED), (False, UNEXPECTED)]


async def test_complete_json_records_runtime_failure_without_retry(
    runtime: ClaudeRuntime, engine: AsyncEngine, db: AsyncSession, fake_claude_env: FakeClaude
) -> None:
    fake_claude_env.reply("PAIR-TASK", {"exit": 1})
    with pytest.raises(BrainError) as exc:
        await complete_json(
            runtime,
            make_session_factory(engine),
            prompt="PAIR-TASK",
            schema=Pair,
            task="test_pair",
            now=datetime.now(UTC),
            config=_config(),
        )
    assert exc.value.reason == "Claude exited with an error (code 1)."
    assert len(fake_claude_env.calls()) == 1
    rows = await _rows(db)
    assert [(r.ok, r.error, r.cost_usd) for r in rows] == [
        (False, "Claude exited with an error (code 1).", None)
    ]


async def test_complete_json_unavailable_is_not_a_call(
    runtime: ClaudeRuntime,
    engine: AsyncEngine,
    db: AsyncSession,
    fake_claude_env: FakeClaude,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_DROP", "--tools")
    with pytest.raises(BrainUnavailable):
        await complete_json(
            runtime,
            make_session_factory(engine),
            prompt="PAIR-TASK",
            schema=Pair,
            task="test_pair",
            now=datetime.now(UTC),
            config=_config(),
        )
    assert await _rows(db) == []


async def test_budget(
    runtime: ClaudeRuntime, engine: AsyncEngine, db: AsyncSession, fake_claude_env: FakeClaude
) -> None:
    now = datetime.now(UTC)
    db.add_all(
        [
            UsageEvent(at=now, task="x", ok=True, duration_ms=1),
            UsageEvent(at=now, task="x", ok=False, duration_ms=1),
            UsageEvent(at=now - timedelta(days=1), task="x", ok=True, duration_ms=1),
        ]
    )
    await db.commit()
    assert await calls_today(db, now=now) == 2
    assert await remaining_calls(db, now=now, config=_config(2)) == 0
    assert await remaining_calls(db, now=now, config=_config(5)) == 3
    assert await remaining_calls(db, now=now, config=_config(1)) == 0
    with pytest.raises(BrainError) as exc:
        await complete_json(
            runtime,
            make_session_factory(engine),
            prompt="PAIR-TASK",
            schema=Pair,
            task="test_pair",
            now=now,
            config=_config(2),
        )
    assert exc.value.reason == "Daily Claude budget reached (2 calls)."
    assert fake_claude_env.calls(None) == []
    assert len(await _rows(db)) == 3


async def test_budget_stops_the_repair_retry(
    runtime: ClaudeRuntime, engine: AsyncEngine, db: AsyncSession, fake_claude_env: FakeClaude
) -> None:
    fake_claude_env.reply("PAIR-TASK", ok_reply("not json"))
    with pytest.raises(BrainError) as exc:
        await complete_json(
            runtime,
            make_session_factory(engine),
            prompt="PAIR-TASK",
            schema=Pair,
            task="test_pair",
            now=datetime.now(UTC),
            config=_config(1),
        )
    assert exc.value.reason == "Daily Claude budget reached (1 calls)."
    assert len(fake_claude_env.calls()) == 1
    assert len(await _rows(db)) == 1


async def test_calls_today_is_utc_day(db: AsyncSession) -> None:
    today = datetime(2026, 11, 8, tzinfo=UTC)
    offsets = [timedelta(seconds=-1), timedelta(0), timedelta(hours=23, minutes=59), timedelta(1)]
    db.add_all([UsageEvent(at=today + d, task="x", ok=True, duration_ms=1) for d in offsets])
    await db.commit()
    assert await calls_today(db, now=today + timedelta(hours=12)) == 2
    # 03:00 in India on Nov 9 is still Nov 8 in UTC.
    ist = timezone(timedelta(hours=5, minutes=30))
    assert await calls_today(db, now=datetime(2026, 11, 9, 3, 0, tzinfo=ist)) == 2


async def test_record_usage_commits_own_transaction(engine: AsyncEngine, db: AsyncSession) -> None:
    result = BrainResult(
        text="secret text", input_tokens=3, output_tokens=4, cost_usd=Decimal("0.5"), duration_ms=9
    )
    await record_usage(
        make_session_factory(engine),
        task="t",
        ok=True,
        duration_ms=9,
        result=result,
        error=None,
        campaign_id=None,
    )
    await record_usage(
        make_session_factory(engine),
        task="t",
        ok=False,
        duration_ms=5,
        result=None,
        error="e" * 500,
        campaign_id=None,
    )
    rows = await _rows(db)
    assert [(r.ok, r.input_tokens, r.output_tokens, r.cost_usd) for r in rows] == [
        (False, None, None, None),
        (True, 3, 4, Decimal("0.5000")),
    ]
    assert rows[0].error is not None and len(rows[0].error) == 200


async def test_finished_call_never_signals_the_reaped_pid(
    runtime: ClaudeRuntime, fake_claude_env: FakeClaude, monkeypatch: pytest.MonkeyPatch
) -> None:
    # After a clean exit the pid is reaped and may be reused: never killpg it.
    killed: list[int] = []
    monkeypatch.setattr("salesfinder.brain.runtime.os.killpg", lambda pid, sig: killed.append(pid))
    await runtime.complete("hello")
    assert killed == []


DEEP = '{"a":' + "[" * 20000 + "]" * 20000 + "}"


def test_extract_json_object_survives_deep_nesting() -> None:
    assert extract_json_object(DEEP) is None
    assert extract_json_object("prose " + DEEP + ' then {"ok": 1}') == {"ok": 1}


async def test_complete_json_deeply_nested_reply_is_recorded(
    runtime: ClaudeRuntime, engine: AsyncEngine, db: AsyncSession, fake_claude_env: FakeClaude
) -> None:
    fake_claude_env.reply("PAIR-TASK", ok_reply(DEEP))
    with pytest.raises(BrainError) as exc:
        await complete_json(
            runtime,
            make_session_factory(engine),
            prompt="PAIR-TASK",
            schema=Pair,
            task="test_pair",
            now=datetime.now(UTC),
            config=_config(),
        )
    assert exc.value.reason == UNEXPECTED
    assert len(fake_claude_env.calls()) == 2
    assert [(r.ok, r.error) for r in await _rows(db)] == [(False, UNEXPECTED)] * 2


async def test_complete_json_records_even_if_validation_blows_up(
    runtime: ClaudeRuntime,
    engine: AsyncEngine,
    db: AsyncSession,
    fake_claude_env: FakeClaude,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_: object) -> None:
        raise RuntimeError("unexpected")

    monkeypatch.setattr("salesfinder.brain.json_task._validate", boom)
    with pytest.raises(RuntimeError):
        await complete_json(
            runtime,
            make_session_factory(engine),
            prompt="PAIR-TASK",
            schema=Pair,
            task="test_pair",
            now=datetime.now(UTC),
            config=_config(),
        )
    assert [r.ok for r in await _rows(db)] == [False]


async def test_preflight_timeout_is_unavailable_not_a_call(
    runtime: ClaudeRuntime,
    engine: AsyncEngine,
    db: AsyncSession,
    fake_claude_env: FakeClaude,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("salesfinder.brain.runtime.HELP_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setenv("FAKE_CLAUDE_HELP_SLEEP", "30")
    with pytest.raises(BrainUnavailable) as exc:
        await complete_json(
            runtime,
            make_session_factory(engine),
            prompt="PAIR-TASK",
            schema=Pair,
            task="test_pair",
            now=datetime.now(UTC),
            config=_config(),
        )
    assert exc.value.reason == "Claude timed out after 0.5s."
    assert fake_claude_env.calls() == []
    assert await _rows(db) == []
    [help_call] = fake_claude_env.calls("help")
    assert await _wait_dead(help_call["pid"])


async def test_uses_the_binary_that_passed_the_flag_check(
    test_settings: Settings,
    fake_claude_env: FakeClaude,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    good, evil = tmp_path / "good", tmp_path / "evil"
    good.mkdir()
    evil.mkdir()
    (good / "claude").symlink_to(FAKE_CLAUDE.resolve())
    (evil / "claude").write_text("#!/bin/sh\nexit 7\n")
    (evil / "claude").chmod(0o755)
    path = os.environ["PATH"]
    monkeypatch.setenv("PATH", f"{good}{os.pathsep}{path}")
    settings = test_settings.model_copy(update={"claude_bin": "claude"})
    runtime = ClaudeRuntime(settings, BrainConfig())
    await runtime.complete("first")
    monkeypatch.setenv("PATH", f"{evil}{os.pathsep}{path}")
    await runtime.complete("second")
    assert [c["prompt"] for c in fake_claude_env.calls()] == ["first", "second"]


async def test_lone_surrogate_in_prompt_is_replaced(
    runtime: ClaudeRuntime, fake_claude_env: FakeClaude
) -> None:
    result = await runtime.complete("bad \ud800 text")
    assert result.text == "{}"
    [call] = fake_claude_env.calls()
    assert call["prompt"] == "bad ? text"
