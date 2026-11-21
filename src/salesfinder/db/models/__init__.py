"""Import every model so Alembic autogenerate sees the full metadata."""

from salesfinder.db.models.audit import AppSetting, AuditLog
from salesfinder.db.models.auth import Invite, User, UserSession
from salesfinder.db.models.companies import Company, DiscoveryDrop, DiscoveryJob
from salesfinder.db.models.contacts import DomainMx, EmailAddress, EmailPattern, Person
from salesfinder.db.models.exclusions import DoNotContact, HubSpotSync, KnownRelationship
from salesfinder.db.models.mail import Conversation, Mailbox, MailboxFolder, MailboxMessage
from salesfinder.db.models.ops import Run
from salesfinder.db.models.outreach import (
    Campaign,
    DraftFailure,
    Message,
    OutboxFeedback,
    Sequence,
    Suppression,
    UsageEvent,
)
from salesfinder.db.models.signals import Cohort, Observation, SignalSetting

__all__ = [
    "AppSetting",
    "AuditLog",
    "Campaign",
    "Cohort",
    "Company",
    "Conversation",
    "DiscoveryDrop",
    "DiscoveryJob",
    "DoNotContact",
    "DomainMx",
    "DraftFailure",
    "EmailAddress",
    "EmailPattern",
    "HubSpotSync",
    "Invite",
    "KnownRelationship",
    "Mailbox",
    "MailboxFolder",
    "MailboxMessage",
    "Message",
    "Observation",
    "OutboxFeedback",
    "Person",
    "Run",
    "Sequence",
    "SignalSetting",
    "Suppression",
    "UsageEvent",
    "User",
    "UserSession",
]
