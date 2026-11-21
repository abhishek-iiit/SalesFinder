"""Isolated ``claude -p`` calls through the operator's Claude Code subscription.

Prompts carry untrusted scraped text and replies become emails, so every call is isolated: no
tools, none of the operator's CLAUDE.md / hooks / skills / plugins / MCP / settings, no saved
transcript, the prompt on stdin (never argv), and a fresh empty temp dir as cwd, removed after.
The child runs in its own process group, killed as a whole on timeout or cancellation. Prompt,
reply and stderr text are never logged — only lengths, durations and exit codes.
"""

import asyncio
import json
import os
import shutil
import signal
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Self

import structlog

from salesfinder.brain.cli_flags import missing_flags
from salesfinder.config import BrainConfig
from salesfinder.settings import Settings

log = structlog.get_logger(__name__)

ISOLATION_ARGS: tuple[str, ...] = (
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
)
STRIPPED_ENV_VARS: tuple[str, ...] = (
    "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD",
    "CLAUDE_CODE_SIMPLE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_ENV_FILE",
    "CLAUDE_PROJECT_DIR",
    "CLAUDECODE",
    "OLDPWD",
)
HELP_TIMEOUT_SECONDS = 30.0
REAP_TIMEOUT_SECONDS = 5.0
MAX_STDOUT_BYTES = 4 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
_CHUNK = 64 * 1024

NOT_FOUND = "Claude CLI not found on PATH."
CANNOT_RUN = "Claude CLI was found but could not be started."
SIGNED_OUT = "Claude returned an error: not signed in or quota reached."
UNEXPECTED = "Claude returned an unexpected response."

Spawn = Callable[..., Awaitable[asyncio.subprocess.Process]]


class BrainError(Exception):
    """A Claude call failed; ``reason`` is a fixed, human-readable string (never model text)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    @classmethod
    def timed_out(cls, seconds: float) -> Self:
        return cls(f"Claude timed out after {seconds:g}s.")

    @classmethod
    def exited(cls, code: int) -> Self:
        return cls(f"Claude exited with an error (code {code}).")


class BrainUnavailable(BrainError):
    """The CLI can't be used at all (absent, or lacks the isolation flags) — nothing was sent."""

    @classmethod
    def missing(cls, flags: list[str]) -> Self:
        listed = ", ".join(flags)
        return cls(f"Claude CLI is missing isolation flags: {listed}. Run `claude update`.")


@dataclass(frozen=True)
class BrainResult:
    text: str
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: Decimal | None
    duration_ms: int


@dataclass(frozen=True)
class _Completed:
    returncode: int
    stdout: bytes
    stderr_len: int
    duration_ms: int


class _OutputTooLarge(Exception):
    pass


def build_argv(claude_bin: str, model: str) -> list[str]:
    argv = [claude_bin, *ISOLATION_ARGS]
    if model:
        argv += ["--model", model]
    return argv


# Every SalesFinder variable (database URL, secret key, API tokens) is dropped too: Claude
# never needs them, and a prompt can't leak what the child process never had.
STRIPPED_ENV_PREFIX = "SALESFINDER_"


def isolated_env(base: Mapping[str, str], cwd: str) -> dict[str, str]:
    """``base`` without ``STRIPPED_ENV_VARS`` and any ``SALESFINDER_*`` variable, with ``PWD``
    set to ``cwd`` and Claude Code's safe mode on."""
    env = {
        k: v
        for k, v in base.items()
        if k not in STRIPPED_ENV_VARS and not k.upper().startswith(STRIPPED_ENV_PREFIX)
    }
    env["PWD"] = cwd
    env["CLAUDE_CODE_SAFE_MODE"] = "1"
    return env


def _resolve(claude_bin: str) -> str:
    """Absolute path to the CLI, resolved against the operator's PATH (the child's cwd is a temp
    dir, so a relative path would otherwise break)."""
    found = shutil.which(claude_bin)
    if found is None:
        raise BrainUnavailable(NOT_FOUND)
    return os.path.abspath(found)


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    # start_new_session=True makes the child a session/group leader: pgid == pid.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def _feed(stream: asyncio.StreamWriter | None, data: bytes) -> None:
    if stream is None:
        return
    try:
        if data:
            stream.write(data)
            await stream.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass  # the child exited without reading its input; its exit status tells the story
    finally:
        stream.close()
        try:
            await stream.wait_closed()
        except (BrokenPipeError, ConnectionResetError):
            pass


async def _read_capped(stream: asyncio.StreamReader | None, cap: int, *, strict: bool) -> bytes:
    """Read to EOF keeping at most ``cap`` bytes; past the cap either fail (``strict``) or keep
    draining so the child never blocks on a full pipe."""
    if stream is None:
        return b""
    buf = bytearray()
    while chunk := await stream.read(_CHUNK):
        if len(buf) + len(chunk) > cap:
            if strict:
                raise _OutputTooLarge
            buf += chunk[: max(0, cap - len(buf))]
        else:
            buf += chunk
    return bytes(buf)


async def _drain(stream: asyncio.StreamReader | None) -> None:
    if stream is not None:
        while await stream.read(_CHUNK):
            pass


async def _kill_and_reap(proc: asyncio.subprocess.Process) -> None:
    """SIGKILL the whole group, then drain both pipes so asyncio can see them close (``wait()``
    only resolves once the pipe transports are done, and a full, unread pipe never closes)."""
    _kill_group(proc)
    if proc.stdin is not None and not proc.stdin.is_closing():
        proc.stdin.close()
    try:
        async with asyncio.timeout(REAP_TIMEOUT_SECONDS):
            await asyncio.gather(_drain(proc.stdout), _drain(proc.stderr), proc.wait())
    except TimeoutError:
        # Something outside the group still holds a pipe open: drop the pipes ourselves.
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            transport.close()
        log.warning("claude.reap_timeout", pid=proc.pid)


async def _communicate(proc: asyncio.subprocess.Process, stdin: bytes) -> tuple[int, bytes, bytes]:
    """Feed stdin while draining both pipes (no pipe-buffer deadlock), then reap. On any failure
    or cancellation the sibling tasks are cancelled and awaited — nothing is left running."""
    tasks = (
        asyncio.create_task(_feed(proc.stdin, stdin)),
        asyncio.create_task(_read_capped(proc.stdout, MAX_STDOUT_BYTES, strict=True)),
        asyncio.create_task(_read_capped(proc.stderr, MAX_STDERR_BYTES, strict=False)),
    )
    try:
        _, out, err = await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return await proc.wait(), out, err


async def _run(argv: list[str], *, stdin: bytes, seconds: float, spawn: Spawn) -> _Completed:
    """Run one isolated CLI process in a fresh temp dir; always kills its group and removes the
    dir, whether it finishes, times out, overflows or the caller is cancelled."""
    started = time.monotonic()
    with tempfile.TemporaryDirectory(
        prefix="salesfinder-claude-", ignore_cleanup_errors=True
    ) as tmp:
        try:
            proc = await spawn(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=tmp,
                env=isolated_env(os.environ, tmp),
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise BrainUnavailable(NOT_FOUND) from exc
        except OSError as exc:
            raise BrainUnavailable(CANNOT_RUN) from exc
        finished = False
        try:
            returncode, out, err = await asyncio.wait_for(
                _communicate(proc, stdin), timeout=seconds
            )
        except TimeoutError as exc:
            raise BrainError.timed_out(seconds) from exc
        except _OutputTooLarge as exc:
            raise BrainError(UNEXPECTED) from exc
        else:
            finished = True
        finally:
            if not finished:
                # Shielded: reap even if we are being cancelled.
                await asyncio.shield(_kill_and_reap(proc))
    return _Completed(
        returncode=returncode,
        stdout=out,
        stderr_len=len(err),
        duration_ms=int((time.monotonic() - started) * 1000),
    )


async def cli_missing_flags(
    claude_bin: str, *, spawn: Spawn | None = None, seconds: float = HELP_TIMEOUT_SECONDS
) -> list[str]:
    """Run ``claude --help`` (isolated like every call) and return the required isolation flags
    it doesn't list. Raises ``BrainUnavailable`` if the CLI can't be found or started and
    ``BrainError`` on timeout. The single implementation behind the runtime and ``doctor``."""
    done = await _run(
        [_resolve(claude_bin), "--help"],
        stdin=b"",
        seconds=seconds,
        spawn=spawn or asyncio.create_subprocess_exec,
    )
    return missing_flags(done.stdout.decode(errors="replace"))


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _cost(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        cost = Decimal(str(value))
    except InvalidOperation:
        return None
    return cost if cost.is_finite() else None


def _envelope(stdout: bytes) -> dict[str, Any] | None:
    try:
        data = json.loads(stdout)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or data.get("type") != "result":
        return None
    return data


def parse_result(done: _Completed) -> BrainResult:
    data = _envelope(done.stdout)
    # A signed-out / over-quota CLI still prints its JSON envelope, often with a non-zero exit.
    if data is not None and data.get("is_error") is True:
        raise BrainError(SIGNED_OUT)
    if done.returncode != 0:
        raise BrainError.exited(done.returncode)
    if data is None or data.get("is_error") is not False or not isinstance(data.get("result"), str):
        raise BrainError(UNEXPECTED)
    usage = data.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    return BrainResult(
        text=data["result"],
        input_tokens=_int_or_none(usage.get("input_tokens")),
        output_tokens=_int_or_none(usage.get("output_tokens")),
        cost_usd=_cost(data.get("total_cost_usd")),
        duration_ms=done.duration_ms,
    )


class ClaudeRuntime:
    """One isolated ``claude -p`` call per ``complete``. The CLI's ``--help`` is checked for the
    isolation flags before the first call (and re-checked until it passes); without them the
    runtime refuses rather than fall back to an unisolated call."""

    def __init__(
        self, settings: Settings, config: BrainConfig, *, spawn: Spawn | None = None
    ) -> None:
        self._claude_bin = settings.claude_bin
        self._config = config
        self._spawn: Spawn = spawn or asyncio.create_subprocess_exec
        # The resolved binary that passed the flag check; every -p call uses exactly it.
        self._checked_bin: str | None = None
        self._flags_lock = asyncio.Lock()

    async def _ensure_flags(self) -> str:
        if self._checked_bin is not None:
            return self._checked_bin
        async with self._flags_lock:
            if self._checked_bin is not None:
                return self._checked_bin
            resolved = _resolve(self._claude_bin)
            try:
                missing = await cli_missing_flags(
                    resolved, spawn=self._spawn, seconds=HELP_TIMEOUT_SECONDS
                )
            except BrainUnavailable:
                raise
            except BrainError as exc:
                # A preflight failure (e.g. --help timing out) means nothing was asked of
                # Claude: unavailable, never a recorded call.
                raise BrainUnavailable(exc.reason) from exc
            if missing:
                log.warning("claude.missing_flags", missing=missing)
                raise BrainUnavailable.missing(missing)
            self._checked_bin = resolved
            return resolved

    async def complete(
        self,
        prompt: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 -- enforced by killing the process group
    ) -> BrainResult:
        seconds = self._config.timeout_seconds if timeout is None else timeout
        argv = build_argv(await self._ensure_flags(), self._config.model)
        # errors="replace": a lone surrogate in scraped text must not crash the call.
        stdin = prompt.encode(errors="replace")
        try:
            done = await _run(argv, stdin=stdin, seconds=seconds, spawn=self._spawn)
            result = parse_result(done)
        except BrainError as exc:
            log.warning("claude.failed", reason=exc.reason, prompt_chars=len(prompt))
            raise
        log.info(
            "claude.call",
            prompt_chars=len(prompt),
            result_chars=len(result.text),
            duration_ms=result.duration_ms,
            exit_code=done.returncode,
            stderr_bytes=done.stderr_len,
        )
        return result
