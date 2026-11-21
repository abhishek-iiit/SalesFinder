"""People found on company pages, their email addresses, learned patterns, and MX cache."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from salesfinder.db.base import Base, Timestamps, UUIDPk

TITLE_CATEGORIES = (
    "procurement",
    "supply_chain",
    "rnd",
    "owner_exec",
    "export",
    "business_dev",
    "marketing",
    "other",
)
EMAIL_KINDS = ("personal", "role")
EMAIL_STATUSES = (
    "published",
    "pattern_confirmed",
    "pattern_guess",
    "verified",
    "risky",
    "invalid",
)
EMAIL_SOURCES = ("page", "pattern", "guess")
MX_STATES = ("has_mx", "no_mail", "unknown")


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({','.join(repr(v) for v in values)})"


class Person(UUIDPk, Timestamps, Base):
    __tablename__ = "people"
    __table_args__ = (
        CheckConstraint(_in("title_category", TITLE_CATEGORIES), name="title_category"),
        UniqueConstraint("company_id", "name_key", name="uq_people_company_id_name_key"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    full_name: Mapped[str] = mapped_column(String(200))
    # Normalized lowercase ASCII name, computed by the service layer.
    name_key: Mapped[str] = mapped_column(String(200))
    first_name: Mapped[str | None] = mapped_column(String(100), default=None)
    last_name: Mapped[str | None] = mapped_column(String(100), default=None)
    title: Mapped[str | None] = mapped_column(String(200), default=None)
    title_category: Mapped[str] = mapped_column(String(24))
    is_target: Mapped[bool] = mapped_column(default=False, server_default=false())
    rank: Mapped[int | None] = mapped_column(default=None)
    source_url: Mapped[str] = mapped_column(String(2048))
    source_snippet: Mapped[str | None] = mapped_column(String(300), default=None)


class EmailAddress(UUIDPk, Timestamps, Base):
    __tablename__ = "email_addresses"
    __table_args__ = (
        CheckConstraint(_in("kind", EMAIL_KINDS), name="kind"),
        CheckConstraint(_in("status", EMAIL_STATUSES), name="status"),
        CheckConstraint(_in("source", EMAIL_SOURCES), name="source"),
        UniqueConstraint("company_id", "address", name="uq_email_addresses_company_id_address"),
    )

    company_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )
    person_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("people.id", ondelete="SET NULL"), default=None, index=True
    )
    # Normalized lowercase.
    address: Mapped[str] = mapped_column(String(320))
    kind: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(16))
    pattern: Mapped[str | None] = mapped_column(String(32), default=None)
    evidence_url: Mapped[str | None] = mapped_column(String(2048), default=None)
    evidence_snippet: Mapped[str | None] = mapped_column(String(300), default=None)
    verifier_result: Mapped[str | None] = mapped_column(String(32), default=None)
    # When the paid verifier answered (the daily cap counts these).
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None, index=True
    )
    # Why the address is excluded (exclusion detail), if it is.
    blocked_reason: Mapped[str | None] = mapped_column(String(300), default=None)
    override_sendable: Mapped[bool | None] = mapped_column(default=None)
    override_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    override_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # A published (source "page") address: when a profile last saw it on the site, and whether a
    # later complete profile no longer did (then it is unsendable unless a teammate allows it).
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    stale: Mapped[bool] = mapped_column(default=False, server_default=false())


class EmailPattern(Base):
    __tablename__ = "email_patterns"

    domain: Mapped[str] = mapped_column(String(255), primary_key=True)
    pattern: Mapped[str] = mapped_column(String(32))
    support: Mapped[int]
    examples: Mapped[list[str]] = mapped_column(JSONB)
    learned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    bounced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class DomainMx(Base):
    __tablename__ = "domain_mx"
    __table_args__ = (CheckConstraint(_in("state", MX_STATES), name="state"),)

    domain: Mapped[str] = mapped_column(String(255), primary_key=True)
    state: Mapped[str] = mapped_column(String(12))
    mx_hosts: Mapped[list[Any]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(200), default=None)
