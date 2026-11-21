"""structlog configuration: JSON lines or a console renderer, to stderr or to a log file.

On stderr the console renderer colours only a terminal. With a log file (``SALESFINDER_LOG_FILE``,
which the launchd plists set) lines go through ``logging.handlers.WatchedFileHandler``, never
coloured: when newsyslog renames the file to rotate it, the next line reopens the path, so
rotation needs no restart and nothing is written to a renamed or deleted file (Ruling P7-F); the
file is (re-)created owner-only.

Untrusted text never goes in logs: third-party loggers that log URLs or server text (httpx logs
every request URL and the server's reason phrase at INFO) are held at WARNING or above (Ruling
P7-I), and a logged exception's traceback never renders local variables."""

import logging
import logging.handlers
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog

__all__ = ["QUIET_LOGGERS", "configure_logging"]

# Third-party loggers that can carry a URL (ours, with a key in it, or a scraped site's), a
# server's reply text or form data at INFO/DEBUG: held at WARNING or above. SQLAlchemy holds its
# own loggers at WARN; uvicorn's access log is off (cli.py).
QUIET_LOGGERS = (
    "httpx",
    "httpcore",
    "urllib3",
    "requests",
    "tldextract",
    "filelock",
    "asyncpg",
    "aiosmtplib",
    "aiosmtpd",
    "mail.log",  # aiosmtpd's server log (tests only)
    "dns",
    "python_multipart",
    "multipart",
    "charset_normalizer",
    "websockets",
    "watchfiles",
)


class _OwnerOnlyFileHandler(logging.handlers.WatchedFileHandler):
    """A WatchedFileHandler that creates the file mode 600, also when it re-creates it after
    newsyslog renamed it (lines name mailbox addresses)."""

    def _open(self) -> Any:
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        return open(fd, "a", encoding=self.encoding, errors=self.errors)


def configure_logging(level: str, json: bool, file: Path | None = None) -> None:
    """Configure structlog and the stdlib root logger (uvicorn logs through it). Without
    ``file`` both print to stderr; with it both write to ``file`` (created with its folder)."""
    numeric = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
    colors = file is None and sys.stderr.isatty()
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if json
        # Plain tracebacks: the default (rich, when installed) renders local variables.
        else structlog.dev.ConsoleRenderer(
            colors=colors, exception_formatter=structlog.dev.plain_traceback
        )
    )
    processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        renderer,
    ]
    if file is None:
        logging.basicConfig(level=numeric, format="%(message)s", stream=sys.stderr, force=True)
        logger_factory: Callable[..., Any] = structlog.PrintLoggerFactory(file=sys.stderr)
    else:
        file.parent.mkdir(parents=True, exist_ok=True)
        # Owner-only whenever the handler creates it (also after a rotation renamed it).
        handler = _OwnerOnlyFileHandler(file, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logging.basicConfig(level=numeric, handlers=[handler], force=True)
        # structlog renders the line; the stdlib handler writes it (and reopens after rotation).
        logger_factory = structlog.stdlib.LoggerFactory()
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(max(numeric, logging.WARNING))
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=logger_factory,
        cache_logger_on_first_use=False,
    )
