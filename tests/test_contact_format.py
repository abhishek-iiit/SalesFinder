"""Plain-language copy for contacts: status badges, sendable reasons, the learned pattern."""

import pytest

from salesfinder.contacts import service
from salesfinder.web.contact_format import (
    NO_PATTERN,
    pattern_line,
    reason_text,
    status_badge,
)


@pytest.mark.parametrize(
    ("status", "label"),
    [
        ("published", "Published on their site"),
        ("pattern_confirmed", "Matches their email pattern"),
        ("pattern_guess", "Our best guess — held"),
        ("verified", "Verified"),
        ("risky", "Risky (catch-all) — held"),
        ("invalid", "Won't deliver"),
    ],
)
def test_status_badges(status: str, label: str) -> None:
    assert status_badge(status).label == label


def test_unknown_status_is_shown_plainly() -> None:
    assert status_badge("weird").label == "weird"


def test_reasons() -> None:
    assert reason_text(None, overridden_by=None) == ""
    assert reason_text(None, overridden_by="Mira") == "Overridden by Mira"
    assert reason_text(service.NO_MAIL_SERVER, overridden_by=None) == "No mail server"
    assert reason_text(service.WONT_DELIVER, overridden_by=None) == "Won't deliver"
    assert reason_text(service.MX_NOT_CHECKED, overridden_by=None) == "Mail server not checked yet"
    assert reason_text(service.HELD["pattern_guess"], overridden_by=None) == "Held: guess"
    assert reason_text(service.HELD["risky"], overridden_by=None) == "Held: risky (catch-all)"
    assert (
        reason_text(service.OVERRIDDEN_OFF, overridden_by="Mira")
        == "Overridden by Mira: don't send"
    )
    assert reason_text(service.OVERRIDDEN_OFF, overridden_by=None) == "Overridden: don't send"
    assert (
        reason_text("HubSpot company Delta (owner: Priya)", overridden_by=None)
        == "Existing relationship: HubSpot company Delta (owner: Priya)"
    )
    assert (
        reason_text("Do-not-contact: a@b.com — asked", overridden_by=None)
        == "Do-not-contact: a@b.com — asked"
    )


def test_pattern_line() -> None:
    assert pattern_line("{first}.{last}", 2, "acme.com") == (
        "Pattern: first.last@acme.com (from 2 addresses)"
    )
    assert pattern_line("{f}{last}", 1, "acme.com") == "Pattern: flast@acme.com (from 1 address)"
    assert pattern_line("{first}{l}", 3, "x.io") == "Pattern: firstl@x.io (from 3 addresses)"
    assert pattern_line(None, None, "acme.com") == NO_PATTERN == "No email pattern learned yet."
