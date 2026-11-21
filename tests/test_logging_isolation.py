"""Logging set up by one test never outlives it (final-wave T2): the CLI configures the root
logger on pytest's captured stderr, which is closed after that test, and aiosmtpd's server
thread (test_smtp) later logged into it ("--- Logging error --- I/O operation on closed
file"). conftest restores the root logger after every test. Order matters: these run in file
order."""

import io
import logging
import logging.handlers
import stat
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog

from salesfinder.logging import configure_logging


def test_a_cli_style_setup_binds_the_root_logger_to_captured_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("INFO", json=False)
    assert any(isinstance(h, logging.StreamHandler) for h in logging.getLogger().handlers)


def test_b_no_root_handler_is_left_on_a_closed_stream() -> None:
    root = logging.getLogger()
    for handler in root.handlers:
        stream = getattr(handler, "stream", None)
        assert stream is None or not stream.closed
    root.info("logged after the setup test")  # would print "--- Logging error ---"


def test_c_no_colour_codes_when_stderr_is_not_a_tty(capsys: pytest.CaptureFixture[str]) -> None:
    """launchd sends stderr to a log file: lines there must be plain text (final wave T7)."""
    assert not sys.stderr.isatty()
    configure_logging("INFO", json=False)
    structlog.get_logger().warning("plain_line_check", count=3)
    err = capsys.readouterr().err
    assert "plain_line_check" in err
    assert "\x1b[" not in err


def test_d_colours_stay_on_in_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    terminal = Terminal()
    monkeypatch.setattr(sys, "stderr", terminal)
    root = logging.getLogger()
    handlers = list(root.handlers)
    try:
        configure_logging("INFO", json=False)
        structlog.get_logger().warning("coloured_line_check")
        assert "\x1b[" in terminal.getvalue()
    finally:
        # Never leave structlog or the root logger bound to this fake terminal.
        structlog.reset_defaults()
        root.handlers[:] = handlers


@pytest.fixture
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in root.handlers:
        if handler not in handlers:
            handler.close()
    structlog.reset_defaults()
    root.handlers[:] = handlers
    root.setLevel(level)


def _file_handlers() -> list[logging.handlers.WatchedFileHandler]:
    return [
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.WatchedFileHandler)
    ]


def test_e_log_file_goes_through_a_watched_file_handler(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], restore_logging: None
) -> None:
    """Ruling P7-F: with SALESFINDER_LOG_FILE, structlog and stdlib (uvicorn) lines go to the
    file through a WatchedFileHandler, plain text, owner-only; nothing goes to stderr."""
    log_file = tmp_path / "Logs" / "worker.log"
    configure_logging("INFO", json=False, file=log_file)
    [handler] = _file_handlers()
    assert Path(handler.baseFilename) == log_file
    structlog.get_logger().info("worker", message="Sent 2 email(s).")
    logging.getLogger("uvicorn.error").warning("uvicorn_line")
    text = log_file.read_text(encoding="utf-8")
    assert "Sent 2 email(s)." in text and "uvicorn_line" in text
    assert "\x1b[" not in text
    assert stat.S_IMODE(log_file.stat().st_mode) == 0o600
    assert capsys.readouterr().err == ""


def test_f_rotation_by_rename_starts_a_new_file(tmp_path: Path, restore_logging: None) -> None:
    """newsyslog renames the file (and may compress and delete the old one): the next line
    reopens the path, so new lines land in a new file, never in the renamed one."""
    log_file = tmp_path / "web.log"
    configure_logging("INFO", json=True, file=log_file)
    log = structlog.get_logger()
    log.info("before_rotation")
    rotated = tmp_path / "web.log.0"
    log_file.rename(rotated)
    log.info("after_rotation")
    assert "before_rotation" in rotated.read_text(encoding="utf-8")
    assert "after_rotation" not in rotated.read_text(encoding="utf-8")
    assert "after_rotation" in log_file.read_text(encoding="utf-8")

    rotated.unlink()  # the old file deleted (as after compression): logging still lands
    log.info("after_delete")
    assert "after_delete" in log_file.read_text(encoding="utf-8")


def test_g_no_log_file_means_stderr(
    capsys: pytest.CaptureFixture[str], restore_logging: None
) -> None:
    configure_logging("INFO", json=False, file=None)
    assert _file_handlers() == []
    structlog.get_logger().info("to_stderr")
    assert "to_stderr" in capsys.readouterr().err


def test_h_http_client_logs_never_reach_the_log_file(tmp_path: Path, restore_logging: None) -> None:
    """Final review I1 / Ruling P7-I: httpx logs every request (its URL, a scraped site's, and
    the server's reason phrase) at INFO; none of it may reach the persistent log, even at
    DEBUG."""
    import asyncio

    import httpx

    marker = "SCRAPEDMARKER"
    log_file = tmp_path / "worker.log"
    configure_logging("DEBUG", json=False, file=log_file)

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, extensions={"reason_phrase": f"OK {marker}".encode()})

    async def fetch() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as client:
            response = await client.get(f"https://{marker.lower()}.example/{marker}?q={marker}")
            assert response.reason_phrase == f"OK {marker}"

    asyncio.run(fetch())
    structlog.get_logger().info("worker", message="Sent 2 email(s).")
    text = log_file.read_text(encoding="utf-8")
    assert "Sent 2 email(s)." in text  # the file works
    assert marker not in text and marker.lower() not in text
    for name in ("httpx", "httpcore", "urllib3", "asyncpg", "aiosmtplib", "tldextract"):
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING, name


def test_i_quiet_loggers_follow_a_stricter_level(restore_logging: None) -> None:
    configure_logging("ERROR", json=True)
    assert logging.getLogger("httpx").level == logging.ERROR


def test_j_a_rotated_log_file_is_recreated_owner_only(
    tmp_path: Path, restore_logging: None
) -> None:
    """Final wave T7: after newsyslog renames the file, the handler re-creates it mode 600 (it
    names mailbox addresses), whatever the umask."""
    import os

    log_file = tmp_path / "worker.log"
    old_umask = os.umask(0o022)
    try:
        configure_logging("INFO", json=True, file=log_file)
        log = structlog.get_logger()
        log.info("before_rotation")
        assert stat.S_IMODE(log_file.stat().st_mode) == 0o600
        log_file.rename(tmp_path / "worker.log.0")
        log.info("after_rotation")
    finally:
        os.umask(old_umask)
    assert "after_rotation" in log_file.read_text(encoding="utf-8")
    assert stat.S_IMODE(log_file.stat().st_mode) == 0o600


def test_k_tracebacks_never_show_local_variables(tmp_path: Path, restore_logging: None) -> None:
    """Final wave T7: a logged exception's traceback never renders local variables (they can
    hold scraped text or a reply body)."""
    log_file = tmp_path / "worker.log"
    configure_logging("INFO", json=False, file=log_file)

    def fail() -> None:
        # Built at run time, so only a rendering of the local (not the source line) shows it.
        scraped = "LOCAL" + "MARKER"  # noqa: F841 - a local the traceback must not show
        raise ValueError("boom")

    try:
        fail()
    except ValueError:
        structlog.get_logger().exception("step_crashed")
    text = log_file.read_text(encoding="utf-8")
    assert "step_crashed" in text and "ValueError" in text
    assert "LOCALMARKER" not in text
