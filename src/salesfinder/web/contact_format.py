"""Plain-language copy for the contacts section: status badges, why an address is held, and the
learned email pattern. Pure. The sendable rule itself lives in ``contacts.service``; this only
words its answers."""

from dataclasses import dataclass

from salesfinder.contacts import service

NO_PATTERN = "No email pattern learned yet."


@dataclass(frozen=True)
class Badge:
    label: str
    css: str  # one of the existing badge classes


_BADGES = {
    "published": Badge("Published on their site", "badge-ok"),
    "pattern_confirmed": Badge("Matches their email pattern", "badge-ok"),
    "pattern_guess": Badge("Our best guess — held", "badge-warn"),
    "verified": Badge("Verified", "badge-ok"),
    "risky": Badge("Risky (catch-all) — held", "badge-warn"),
    "invalid": Badge("Won't deliver", "badge-off"),
}

# Fixed copy for the service's reason strings.
_FIXED = {
    service.WONT_DELIVER: "Won't deliver",
    service.NO_MAIL_SERVER: "No mail server",
    service.MX_NOT_CHECKED: "Mail server not checked yet",
    service.HELD["pattern_guess"]: "Held: guess",
    service.HELD["risky"]: "Held: risky (catch-all)",
    service.NOT_A_MAILBOX: "Not a person's mailbox",
    service.NO_LONGER_ON_SITE: "No longer on their site",
}
_HUBSPOT_PREFIX = "HubSpot "
_PLACEHOLDERS = {"{first}": "first", "{last}": "last", "{f}": "f", "{l}": "l"}


def status_badge(status: str) -> Badge:
    return _BADGES.get(status, Badge(status, "badge-off"))


def reason_text(reason: str | None, *, overridden_by: str | None) -> str:
    """The "why" next to a Yes/No: ``reason`` is ``unsendable_reason``'s answer (None = sendable),
    ``overridden_by`` the name of the teammate whose override applies, if any."""
    if reason is None:
        return f"Overridden by {overridden_by}" if overridden_by else ""
    if reason == service.OVERRIDDEN_OFF:
        return (
            f"Overridden by {overridden_by}: don't send"
            if overridden_by
            else ("Overridden: don't send")
        )
    if reason in _FIXED:
        return _FIXED[reason]
    if reason.startswith(_HUBSPOT_PREFIX):
        return f"Existing relationship: {reason}"
    return reason  # e.g. "Do-not-contact: …" (already worded); escaped by the template


def pattern_line(pattern: str | None, support: int | None, domain: str) -> str:
    """ "Pattern: first.last@acme.com (from 2 addresses)", or ``NO_PATTERN``."""
    if not pattern:
        return NO_PATTERN
    local = pattern
    for placeholder, word in _PLACEHOLDERS.items():
        local = local.replace(placeholder, word)
    line = f"Pattern: {local}@{domain}"
    if support:
        line += f" (from {support} address{'' if support == 1 else 'es'})"
    return line
