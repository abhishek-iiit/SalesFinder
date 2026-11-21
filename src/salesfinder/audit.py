"""Append-only audit trail. Callers commit, so the audit row shares their transaction."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.db.models import AuditLog

# Phase 1 action names.
USER_LOGIN = "user.login"
USER_LOGIN_FAILED = "user.login_failed"
USER_LOGOUT = "user.logout"
INVITE_CREATED = "invite.created"
INVITE_ACCEPTED = "invite.accepted"
USER_DEACTIVATED = "user.deactivated"
USER_REACTIVATED = "user.reactivated"
USER_ADMIN_CREATED = "user.admin_created"
USER_PASSWORD_RESET = "user.password_reset"  # noqa: S105
# Phase 7: an admin revoked a pending invite (its expiry set to now).
INVITE_REVOKED = "invite.revoked"
# Phase 7: an admin downloaded the companies CSV (details: side and row count, never data).
COMPANIES_EXPORTED = "companies.exported"

# Phase 2 action names.
DNC_ADDED = "dnc.added"
DNC_REMOVED = "dnc.removed"
HUBSPOT_SYNC_REQUESTED = "hubspot.sync_requested"
DISCOVERY_ESTIMATED = "discovery.estimated"
DISCOVERY_APPROVED = "discovery.approved"
DISCOVERY_CANCELLED = "discovery.cancelled"
SETTINGS_UPDATED = "settings.updated"

# Phase 3 action names.
SIGNAL_STATE_CHANGED = "signal.state_changed"
COHORT_CREATED = "cohort.created"
COHORT_UPDATED = "cohort.updated"
COHORT_DELETED = "cohort.deleted"
COMPANY_REPROFILE_REQUESTED = "company.reprofile_requested"

# Phase 4 action names.
EMAIL_OVERRIDE_SET = "email.override_set"
EMAIL_OVERRIDE_CLEARED = "email.override_cleared"

# Phase 5 action names.
CAMPAIGN_CREATED = "campaign.created"
CAMPAIGN_ACTIVATED = "campaign.activated"
CAMPAIGN_PAUSED = "campaign.paused"
CAMPAIGN_RESUMED = "campaign.resumed"
CAMPAIGN_ARCHIVED = "campaign.archived"
MESSAGE_APPROVED = "message.approved"
MESSAGE_EDITED = "message.edited"
MESSAGE_REJECTED = "message.rejected"
MESSAGE_HELD = "message.held"  # an approval the gate refused: the message is now gate_failed
MESSAGE_RECHECKED = "message.rechecked"
MESSAGE_BULK_APPROVED = "message.bulk_approved"  # one summary row per bulk approval


# Phase 6 action names.
SENDING_PAUSED = "sending.paused"
SENDING_RESUMED = "sending.resumed"
SENDING_AUTO_PAUSED = "sending.auto_paused"
MAILBOX_CREATED = "mailbox.created"
MAILBOX_UPDATED = "mailbox.updated"
MAILBOX_ENABLED = "mailbox.enabled"
MAILBOX_DISABLED = "mailbox.disabled"
MAILBOX_TESTED = "mailbox.tested"
# An admin cleared a folder's mail-reading error after seeing it.
MAILBOX_ERROR_DISMISSED = "mailbox.error_dismissed"
CAMPAIGN_MAILBOX_SET = "campaign.mailbox_set"
MESSAGE_SENT = "message.sent"
MESSAGE_WITHDRAWN = "message.withdrawn"
MESSAGE_MARKED_SENT = "message.marked_sent"
MESSAGE_RESEND = "message.resend"
MESSAGE_SEND_FAILED = "message.send_failed"  # the sender gave up on it (system actor)
MESSAGE_UNCONFIRMED = "message.unconfirmed"  # not found in Sent, or not checkable for a day
MESSAGE_CANCELLED = "message.cancelled"  # by the system, e.g. its sequence moved on mid-send
SUPPRESSION_ADDED = "suppression.added"
SUPPRESSION_REMOVED = "suppression.removed"
SUPPRESSION_UPGRADED = "suppression.upgraded"  # an unsubscribe replaced a bounced/manual reason
SEQUENCE_STOPPED = "sequence.stopped"  # a system stop (actor None), e.g. recipient now excluded
CONVERSATION_ASSIGNED = "conversation.assigned"
CONVERSATION_HANDLED = "conversation.handled"
CONVERSATION_TAKEN_OVER = "conversation.taken_over"
# The conversation was flagged for a teammate (interested, unsure, a rejected draft, ...).
CONVERSATION_NEEDS_YOU = "conversation.needs_you"
REPLY_WRITTEN = "reply.written"
# An inbound reply was classified (system actor): intent, confidence, and the draft if any.
REPLY_CLASSIFIED = "reply.classified"


async def record(
    db: AsyncSession,
    *,
    action: str,
    actor_id: uuid.UUID | None,
    target_type: str | None = None,
    target_id: str | None = None,
    details: dict[str, Any] | None = None,
    ip: str | None = None,
) -> AuditLog:
    row = AuditLog(
        action=action,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        details=dict(details) if details else {},
        ip=ip[:64] if ip else None,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row, ["at"])
    return row
