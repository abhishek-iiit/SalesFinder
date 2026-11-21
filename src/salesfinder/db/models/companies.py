"""Discovered companies, discovery jobs, and dropped search results."""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    false,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from salesfinder.db.base import Base, Timestamps, UUIDPk

SIDE_CHECK = "side IN ('buyer','supplier')"
PROFILE_STATUSES = "'pending','ok','partial','failed','blocked','no_site'"
JOB_STATUSES = "'estimated','approved','running','succeeded','failed','stopped_budget','cancelled'"


class DiscoveryJob(UUIDPk, Timestamps, Base):
    __tablename__ = "discovery_jobs"
    __table_args__ = (
        CheckConstraint(SIDE_CHECK, name="side"),
        CheckConstraint(f"status IN ({JOB_STATUSES})", name="status"),
        Index("ix_discovery_jobs_status_approved_at", "status", "approved_at"),
    )

    side: Mapped[str] = mapped_column(String(16))
    params: Mapped[dict[str, Any]] = mapped_column(JSONB)
    queries: Mapped[list[dict[str, str]]] = mapped_column(JSONB)
    pages_per_query: Mapped[int]
    estimated_searches: Mapped[int]
    estimated_cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 4))
    status: Mapped[str] = mapped_column(String(16))
    created_by_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    approved_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    searches_done: Mapped[int] = mapped_column(default=0, server_default="0")
    actual_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), default=Decimal(0), server_default=text("0")
    )
    results_seen: Mapped[int] = mapped_column(default=0, server_default="0")
    companies_new: Mapped[int] = mapped_column(default=0, server_default="0")
    companies_existing: Mapped[int] = mapped_column(default=0, server_default="0")
    companies_excluded: Mapped[int] = mapped_column(default=0, server_default="0")
    results_dropped: Mapped[int] = mapped_column(default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(String(500), default=None)


class Company(UUIDPk, Timestamps, Base):
    __tablename__ = "companies"
    __table_args__ = (
        CheckConstraint(SIDE_CHECK, name="side"),
        CheckConstraint("status IN ('new','excluded')", name="status"),
        CheckConstraint(f"profile_status IN ({PROFILE_STATUSES})", name="profile_status"),
        UniqueConstraint("side", "domain", name="uq_companies_side_domain"),
        Index("ix_companies_side_status", "side", "status"),
        # The exclusion sweep joins companies to the exclusion lists by domain alone.
        Index("ix_companies_domain", "domain"),
        Index("ix_companies_side_score", "side", "score"),
    )

    side: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(300))
    domain: Mapped[str] = mapped_column(String(255))
    website_url: Mapped[str] = mapped_column(String(2048))
    country: Mapped[str | None] = mapped_column(String(2), default=None)
    region: Mapped[str | None] = mapped_column(String(100), default=None)
    source: Mapped[str] = mapped_column(String(16), default="serper", server_default="serper")
    discovered_by_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("discovery_jobs.id", ondelete="SET NULL"), default=None
    )
    discovery_query: Mapped[str | None] = mapped_column(String(500), default=None)
    snippet: Mapped[str | None] = mapped_column(String(1000), default=None)
    status: Mapped[str] = mapped_column(String(16))
    exclusion_reason: Mapped[str | None] = mapped_column(String(32), default=None)
    exclusion_detail: Mapped[str | None] = mapped_column(String(300), default=None)
    profile_status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending"
    )
    profile_error: Mapped[str | None] = mapped_column(String(300), default=None)
    profiled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    pages_fetched: Mapped[int] = mapped_column(default=0, server_default="0")
    # Consecutive failed / no_site profiles (0 after a successful one): sets the retry backoff.
    profile_failures: Mapped[int] = mapped_column(default=0, server_default="0")
    # Not claimed for profiling before this (set when a batch was held back as a network outage).
    profile_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Consecutive circuit-breaker holds (0 after any recorded outcome): at the cap a dead site
    # is recorded instead of held again.
    profile_holds: Mapped[int] = mapped_column(default=0, server_default="0")
    contact_form_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    contacts_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    contacts_found: Mapped[int] = mapped_column(default=0, server_default="0")
    sendable_contacts: Mapped[int] = mapped_column(default=0, server_default="0")
    # The last profile's people extraction: how many about/contact/team/leadership pages it
    # read, and whether extracting from them crashed.
    people_pages_read: Mapped[int] = mapped_column(default=0, server_default="0")
    contacts_extract_failed: Mapped[bool] = mapped_column(default=False, server_default=false())
    score: Mapped[int | None] = mapped_column(default=None)
    disqualified: Mapped[bool] = mapped_column(default=False, server_default=false())
    scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class DiscoveryDrop(UUIDPk, Base):
    __tablename__ = "discovery_drops"

    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("discovery_jobs.id", ondelete="CASCADE"), index=True
    )
    url: Mapped[str] = mapped_column(String(2048))
    domain: Mapped[str | None] = mapped_column(String(255), default=None)
    reason: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
