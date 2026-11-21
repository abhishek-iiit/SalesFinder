"""Ops: the worker's heartbeat log. One ``runs`` row covers one heartbeat window
(``usage.heartbeat_interval_minutes``) of worker passes: what they did (log-safe lines only),
counts and failures, plus the liveness beat (``last_beat_at``, the current step).

``lines`` and ``failures`` hold only the worker's own log-safe lines (counts, our own copy,
mailbox addresses, exception type names), never untrusted text. Items are
``{"at": <ISO 8601 UTC>, "text": <line>}``."""

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Index, String, false, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from salesfinder.db.base import Base, UUIDPk

# window: closed at the end of its heartbeat window (the worker carried on in a new row);
# shutdown: the worker stopped cleanly; crashed: closed by the next start, because the worker
# stopped without shutting down; once: a single ``salesfinder worker --once`` pass.
RUN_END_REASONS = ("window", "shutdown", "crashed", "once")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({','.join(repr(v) for v in values)})"


class Run(UUIDPk, Base):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(_in("end_reason", RUN_END_REASONS), name="end_reason"),
        CheckConstraint("(finished_at IS NULL) = (end_reason IS NULL)", name="finished_has_reason"),
        Index("ix_runs_started_at", "started_at"),
        Index("ix_runs_open", "started_at", postgresql_where=text("finished_at IS NULL")),
        # At most one open run: one worker at a time, and it closes its row before opening the
        # next.
        Index(
            "uq_runs_one_open",
            text("(true)"),
            unique=True,
            postgresql_where=text("finished_at IS NULL"),
        ),
    )

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # The liveness timestamp: the background beat and every recorded pass move it.
    last_beat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_pass_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    end_reason: Mapped[str | None] = mapped_column(String(12), default=None)
    passes: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    # Every step failure in the window (``failures`` keeps only the newest 50).
    failed_steps: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    lines: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    # Lines past ``ops.max_run_lines``: counted, not kept.
    lines_dropped: Mapped[int] = mapped_column(default=0, server_default=text("0"))
    failures: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    # Some pass in the window fell in quiet hours.
    quiet: Mapped[bool] = mapped_column(default=False, server_default=false())
    # The step running at the last beat (None between passes).
    step: Mapped[str | None] = mapped_column(String(32), default=None)
    step_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    host: Mapped[str] = mapped_column(String(255))
    pid: Mapped[int]
    # ``ops.version.code_version()`` of the worker that wrote the row.
    code_version: Mapped[str] = mapped_column(String(16))
