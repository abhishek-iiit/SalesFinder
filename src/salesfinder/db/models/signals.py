"""Signal settings, per-company observations, and saved cohorts."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from salesfinder.db.base import Base, Timestamps, UUIDPk
from salesfinder.db.models.companies import SIDE_CHECK


class SignalSetting(Base):
    __tablename__ = "signal_settings"
    __table_args__ = (
        CheckConstraint("state IN ('proposed','confirmed','skipped','rejected')", name="state"),
    )

    code: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(16))
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Observation(UUIDPk, Base):
    __tablename__ = "observations"
    __table_args__ = (
        Index("ix_observations_company_id_code_observed_at", "company_id", "code", "observed_at"),
        Index(
            "ix_observations_latest",
            "company_id",
            "code",
            postgresql_where=text("is_latest"),
        ),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"))
    code: Mapped[str] = mapped_column(String(64))
    value: Mapped[Any] = mapped_column(JSONB)
    evidence_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    evidence_snippet: Mapped[str | None] = mapped_column(String(300), default=None)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    source: Mapped[str] = mapped_column(String(16), default="profiler", server_default="profiler")
    # The newest observation per (company, code); maintained where observations are written.
    is_latest: Mapped[bool] = mapped_column(default=False, server_default=text("false"))


class Cohort(UUIDPk, Timestamps, Base):
    __tablename__ = "cohorts"
    __table_args__ = (CheckConstraint(SIDE_CHECK, name="side"),)

    name: Mapped[str] = mapped_column(String(120), unique=True)
    side: Mapped[str] = mapped_column(String(16))
    definition: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
