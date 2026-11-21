"""HubSpot-synced known relationships, sync runs, and the do-not-contact list."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from salesfinder.db.base import Base, Timestamps, UUIDPk

KIND_CHECK = "kind IN ('domain','email')"


class KnownRelationship(UUIDPk, Timestamps, Base):
    __tablename__ = "known_relationships"
    __table_args__ = (
        CheckConstraint("source IN ('hubspot')", name="source"),
        CheckConstraint(KIND_CHECK, name="kind"),
        CheckConstraint("hubspot_object IN ('company','contact')", name="hubspot_object"),
        UniqueConstraint(
            "source", "kind", "value", name="uq_known_relationships_source_kind_value"
        ),
    )

    source: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(8))
    value: Mapped[str] = mapped_column(String(320), index=True)
    hubspot_object: Mapped[str] = mapped_column(String(16))
    hubspot_id: Mapped[str] = mapped_column(String(32))
    name: Mapped[str | None] = mapped_column(String(200), default=None)
    lifecycle_stage: Mapped[str | None] = mapped_column(String(64), default=None)
    owner_name: Mapped[str | None] = mapped_column(String(200), default=None)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class HubSpotSync(UUIDPk, Base):
    __tablename__ = "hubspot_syncs"
    __table_args__ = (
        CheckConstraint("status IN ('queued','running','succeeded','failed')", name="status"),
        Index("ix_hubspot_syncs_status_finished_at", "status", "finished_at"),
    )

    status: Mapped[str] = mapped_column(String(16))
    requested_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    companies_count: Mapped[int] = mapped_column(default=0, server_default="0")
    contacts_count: Mapped[int] = mapped_column(default=0, server_default="0")
    error: Mapped[str | None] = mapped_column(String(500), default=None)


class DoNotContact(UUIDPk, Base):
    __tablename__ = "do_not_contact"
    __table_args__ = (
        CheckConstraint(KIND_CHECK, name="kind"),
        UniqueConstraint("kind", "value", name="uq_do_not_contact_kind_value"),
    )

    kind: Mapped[str] = mapped_column(String(8))
    value: Mapped[str] = mapped_column(String(320))
    reason: Mapped[str] = mapped_column(String(200))
    added_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
