"""Audit log and key/value app settings."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from salesfinder.db.base import Base, UUIDPk


class AuditLog(UUIDPk, Base):
    __tablename__ = "audit_log"
    # The sender looks rows up by (action, target_id): "may have gone", MX-wait markers.
    __table_args__ = (Index("ix_audit_log_action_target_id", "action", "target_id"),)

    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id"), index=True, default=None
    )
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str | None] = mapped_column(String(64), default=None)
    target_id: Mapped[str | None] = mapped_column(String(64), default=None)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    ip: Mapped[str | None] = mapped_column(String(64), default=None)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[Any] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), default=None)
