import uuid
from datetime import UTC, date, datetime, timedelta

import dns.exception
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from campaign_helpers import FACTS, seed_user
from fakes.dns import FakeResolver
from fakes.mail import FakeFolder, FakeReader, FakeSender, fake_mail
from salesfinder.config import AppConfig
from salesfinder.db.engine import make_session_factory
from salesfinder.db.models import Mailbox
from salesfinder.sending import mailboxes
from salesfinder.sending.mailboxes import MailboxForm
from salesfinder.settings import Settings
from test_readiness import DOMAIN, GOOD

NOW = datetime(2026, 11, 13, 12, tzinfo=UTC)
CONFIG = AppConfig()


def _form(**over: object) -> MailboxForm:
    data: dict[str, object] = {
        "address": f"Asha@{DOMAIN}",
        "side": "buyer",
        "username": f"asha@{DOMAIN}",
        "secret_name": "asha",
    }
    return MailboxForm.model_validate({**data, **over})


def _settings(test_settings: Settings, **passwords: str) -> Settings:
    return test_settings.model_copy(
        update={"mailbox_passwords": {k: SecretStr(v) for k, v in passwords.items()}}
    )


def test_form_validates() -> None:
    form = _form()
    assert form.address == f"asha@{DOMAIN}"
    assert (form.smtp_host, form.smtp_port, form.daily_max) == ("smtp.gmail.com", 465, 30)
    with pytest.raises(ValidationError):
        _form(daily_max=51)
    with pytest.raises(ValidationError):
        _form(secret_name="Has Spaces")  # noqa: S106
    with pytest.raises(ValidationError):
        _form(address="not-an-address")
    with pytest.raises(ValidationError):
        MailboxForm.model_validate(
            {**_form().model_dump(), "daily_max": 40}, context={"max_daily": 30}
        )


async def test_create_refuses_elchemy_and_duplicates(db: AsyncSession) -> None:
    user = await seed_user(db)
    for address in ("asha@elchemy.com", "asha@mail.elchemy.com", "ASHA@Elchemy.com"):
        with pytest.raises(ValueError) as exc:
            await mailboxes.create_mailbox(db, _form(address=address), actor=user, facts=FACTS)
        assert str(exc.value) == (
            "Cold email is never sent from elchemy.com — use the separate sending domain."
        )
    box = await mailboxes.create_mailbox(db, _form(), actor=user, facts=FACTS)
    assert (box.enabled, box.address, box.created_by_id) == (False, f"asha@{DOMAIN}", user.id)
    with pytest.raises(ValueError, match=r"^That mailbox already exists\.$"):
        await mailboxes.create_mailbox(db, _form(address=f"ASHA@{DOMAIN}"), actor=user, facts=FACTS)
    # A second enabled mailbox for the same side is allowed.
    ben = _form(address=f"ben@{DOMAIN}", secret_name="ben")  # noqa: S106
    other = await mailboxes.create_mailbox(db, ben, actor=user, facts=FACTS)
    assert other.side == box.side


async def _tested(db: AsyncSession, box: Mailbox) -> None:
    """What a passing Test connection does to the hold (Ruling P6-W)."""
    assert await mailboxes.clear_settings_hold(db, box.id, mailboxes.connection_snapshot(box))


async def test_a_new_mailbox_is_held_until_its_first_test_passes(
    db: AsyncSession, test_settings: Settings
) -> None:
    """Ruling P6-W: a new mailbox gets no login with its password (no polling, no reconcile)
    and can't be enabled until a Test connection passes."""
    user = await seed_user(db)
    box = await mailboxes.create_mailbox(db, _form(), actor=user, facts=FACTS)
    assert box.hold_reason == mailboxes.NOT_TESTED and mailboxes.untested(box)
    settings = _settings(test_settings, asha="pw")
    with pytest.raises(ValueError, match=r"Test the connection first"):
        await mailboxes.set_enabled(db, box.id, True, actor=user, settings=settings, facts=FACTS)
    assert not box.enabled
    # Unrelated edits keep the hold; a test of settings that changed since clears nothing.
    await mailboxes.update_mailbox(db, box.id, _form(daily_max=10), actor=user, facts=FACTS)
    assert box.hold_reason == mailboxes.NOT_TESTED
    stale = mailboxes.connection_snapshot(box)
    await mailboxes.update_mailbox(
        db, box.id, _form(imap_host="imap.other.example"), actor=user, facts=FACTS
    )
    assert not await mailboxes.clear_settings_hold(db, box.id, stale)
    assert mailboxes.untested(box)
    await _tested(db, box)
    assert box.hold_reason is None and not mailboxes.untested(box)
    await mailboxes.set_enabled(db, box.id, True, actor=user, settings=settings, facts=FACTS)
    assert box.enabled


async def test_enable_needs_a_password(db: AsyncSession, test_settings: Settings) -> None:
    user = await seed_user(db)
    box = await mailboxes.create_mailbox(db, _form(), actor=user, facts=FACTS)
    await _tested(db, box)
    with pytest.raises(ValueError) as exc:
        await mailboxes.set_enabled(
            db, box.id, True, actor=user, settings=test_settings, facts=FACTS
        )
    assert str(exc.value) == 'Set SALESFINDER_MAILBOX_PASSWORDS for "asha" in .env and restart.'
    assert box.enabled is False
    settings = _settings(test_settings, asha="app-password-1")
    assert (
        await mailboxes.set_enabled(db, box.id, True, actor=user, settings=settings, facts=FACTS)
    ).enabled
    assert not (
        await mailboxes.set_enabled(db, box.id, False, actor=user, settings=settings, facts=FACTS)
    ).enabled
    # Disabling never needs the password.
    assert not (
        await mailboxes.set_enabled(
            db, box.id, False, actor=user, settings=test_settings, facts=FACTS
        )
    ).enabled


async def test_update_keeps_the_address(db: AsyncSession) -> None:
    user = await seed_user(db)
    box = await mailboxes.create_mailbox(db, _form(), actor=user, facts=FACTS)
    updated = await mailboxes.update_mailbox(
        db,
        box.id,
        _form(daily_max=10, warmup_started_on=date(2026, 11, 1)),
        actor=user,
        facts=FACTS,
    )
    assert (updated.daily_max, updated.warmup_started_on) == (10, date(2026, 11, 1))
    with pytest.raises(ValueError, match="address can't be changed"):
        await mailboxes.update_mailbox(
            db, box.id, _form(address=f"zed@{DOMAIN}"), actor=user, facts=FACTS
        )
    with pytest.raises(ValueError, match="no longer exists"):
        await mailboxes.update_mailbox(db, uuid.uuid4(), _form(), actor=user, facts=FACTS)


async def test_credentials_use_the_settings_password(
    db: AsyncSession, test_settings: Settings
) -> None:
    user = await seed_user(db)
    box = await mailboxes.create_mailbox(db, _form(), actor=user, facts=FACTS)
    assert mailboxes.credentials(box, test_settings, CONFIG) is None
    creds = mailboxes.credentials(box, _settings(test_settings, asha="pw"), CONFIG)
    assert creds is not None
    assert creds.password.get_secret_value() == "pw"
    assert (creds.address, creds.smtp_host, creds.smtp_security) == (
        f"asha@{DOMAIN}",
        "smtp.gmail.com",
        "ssl",
    )
    assert creds.timeout == CONFIG.sending.smtp_timeout_seconds


async def test_refresh_readiness_stores_the_report(db: AsyncSession, engine: AsyncEngine) -> None:
    engine_factory = make_session_factory(engine)
    user = await seed_user(db)
    box = await mailboxes.create_mailbox(db, _form(), actor=user, facts=FACTS)
    await db.commit()
    resolver = FakeResolver(GOOD)
    report = await mailboxes.refresh_readiness(engine_factory, box.id, resolve=resolver, now=NOW)
    assert report.ok
    await db.refresh(box)
    assert (box.dns_ok, box.dns_checked_at) == (True, NOW)
    assert [c["name"] for c in box.dns_report["checks"]] == ["spf", "dkim", "dmarc", "mx"]
    assert mailboxes.readiness_is_fresh(box, now=NOW + timedelta(hours=23), config=CONFIG)
    assert not mailboxes.readiness_is_fresh(box, now=NOW + timedelta(hours=25), config=CONFIG)

    bad = FakeResolver({**GOOD, (DOMAIN, "TXT"): dns.exception.Timeout()})
    report = await mailboxes.refresh_readiness(engine_factory, box.id, resolve=bad, now=NOW)
    assert not report.ok
    await db.refresh(box)
    assert box.dns_ok is False
    assert not mailboxes.readiness_is_fresh(box, now=NOW, config=CONFIG)


def _mailbox() -> Mailbox:
    return Mailbox(address=f"a@{DOMAIN}", side="buyer", sent_folder=None)


async def _creds(db: AsyncSession, test_settings: Settings) -> tuple[Mailbox, object]:
    user = await seed_user(db)
    box = await mailboxes.create_mailbox(db, _form(), actor=user, facts=FACTS)
    creds = mailboxes.credentials(box, _settings(test_settings, asha="pw"), CONFIG)
    assert creds is not None
    return box, creds


async def test_connection_ok(db: AsyncSession, test_settings: Settings) -> None:
    box, creds = await _creds(db, test_settings)
    sender, reader = FakeSender(), FakeReader({"[Gmail]/Sent Mail": FakeFolder(1)})
    problems = await mailboxes.test_connection(box, creds, fake_mail(sender, reader))  # type: ignore[arg-type]
    assert problems == []
    assert sender.checked == [f"asha@{DOMAIN}"]
    assert reader.closed


async def test_connection_problems_use_fixed_copy(
    db: AsyncSession, test_settings: Settings
) -> None:
    box, creds = await _creds(db, test_settings)
    sender = FakeSender(check_error="couldn't sign in.")
    reader = FakeReader({}, special={})
    problems = await mailboxes.test_connection(box, creds, fake_mail(sender, reader))  # type: ignore[arg-type]
    assert problems == [
        "SMTP: couldn't sign in.",
        "IMAP: couldn't find the Sent folder. Set its name on the mailbox.",
    ]
    assert reader.closed
    # A reader that can't open, or that blows up with server text, never leaks that text.
    secret = "535 5.7.8 Username and Password not accepted secret-detail"  # noqa: S105
    failing = FakeReader({}, fail=RuntimeError(secret))
    problems = await mailboxes.test_connection(box, creds, fake_mail(FakeSender(), failing))  # type: ignore[arg-type]
    assert problems == [mailboxes.IMAP_FAILED]
    assert all(secret not in p for p in problems)
    problems = await mailboxes.test_connection(box, creds, fake_mail(FakeSender(), None))  # type: ignore[arg-type]
    assert problems == [mailboxes.IMAP_FAILED]


def test_ramp_cap_today() -> None:
    box = _mailbox()
    box.daily_max = 12
    assert mailboxes.ramp_cap_today(box, today=date(2026, 11, 13), config=CONFIG) == 0
    box.warmup_started_on = date(2026, 11, 1)
    assert mailboxes.ramp_cap_today(box, today=date(2026, 11, 14), config=CONFIG) == 0
    assert mailboxes.ramp_cap_today(box, today=date(2026, 11, 15), config=CONFIG) == 5
    assert mailboxes.ramp_cap_today(box, today=date(2026, 11, 17), config=CONFIG) == 11
    assert mailboxes.ramp_cap_today(box, today=date(2026, 12, 25), config=CONFIG) == 12


async def test_blank_website_and_fullwidth_lookalike_are_refused(db: AsyncSession) -> None:
    user = await seed_user(db)
    blank = FACTS.model_copy(update={"website": ""})
    with pytest.raises(ValueError, match=r"^Set website in facts\.yaml first\.$"):
        await mailboxes.create_mailbox(db, _form(), actor=user, facts=blank)
    with pytest.raises(ValueError, match=r"never sent from elchemy\.com"):
        await mailboxes.create_mailbox(
            db, _form(address="a@\uff45lchemy.com"), actor=user, facts=FACTS
        )


async def test_enable_rechecks_the_elchemy_rule(db: AsyncSession, test_settings: Settings) -> None:
    user = await seed_user(db)
    box = await mailboxes.create_mailbox(db, _form(), actor=user, facts=FACTS)
    box.address = "late@elchemy.com"
    await db.flush()
    settings = _settings(test_settings, asha="pw")
    with pytest.raises(ValueError, match=r"never sent from elchemy\.com"):
        await mailboxes.set_enabled(db, box.id, True, actor=user, settings=settings, facts=FACTS)


async def test_secret_name_is_unique(db: AsyncSession) -> None:
    user = await seed_user(db)
    await mailboxes.create_mailbox(db, _form(), actor=user, facts=FACTS)
    other = _form(address=f"ben@{DOMAIN}")
    with pytest.raises(ValueError, match=r"^That password name is already used"):
        await mailboxes.create_mailbox(db, other, actor=user, facts=FACTS)
    ben_form = _form(address=f"ben@{DOMAIN}", secret_name="ben")  # noqa: S106
    ben = await mailboxes.create_mailbox(db, ben_form, actor=user, facts=FACTS)
    with pytest.raises(ValueError, match=r"^That password name is already used"):
        await mailboxes.update_mailbox(db, ben.id, other, actor=user, facts=FACTS)


async def test_changing_connection_settings_disables(
    db: AsyncSession, test_settings: Settings
) -> None:
    user = await seed_user(db)
    box = await mailboxes.create_mailbox(db, _form(), actor=user, facts=FACTS)
    await _tested(db, box)
    settings = _settings(test_settings, asha="pw")
    await mailboxes.set_enabled(db, box.id, True, actor=user, settings=settings, facts=FACTS)
    same = _form(daily_max=10)
    assert mailboxes.connection_changes(box, same) == []
    await mailboxes.update_mailbox(db, box.id, same, actor=user, facts=FACTS)
    assert box.enabled
    moved = _form(smtp_host="smtp.evil.example", imap_port=994)
    assert mailboxes.connection_changes(box, moved) == ["smtp_host", "imap_port"]
    await mailboxes.update_mailbox(db, box.id, moved, actor=user, facts=FACTS)
    assert not box.enabled
    assert box.hold_reason == (
        "Connection settings changed — test it, then enable it again. "
        "Replies and opt-outs aren't read until it passes."
    )
    assert mailboxes.untested(box)
    # Ruling P6-V: not enabled again until a successful Test connection clears the hold.
    with pytest.raises(ValueError, match=r"Test the connection first"):
        await mailboxes.set_enabled(db, box.id, True, actor=user, settings=settings, facts=FACTS)
    tested = mailboxes.connection_snapshot(box)
    assert await mailboxes.clear_settings_hold(db, box.id, tested)
    assert box.hold_reason is None and not mailboxes.untested(box)
    assert not await mailboxes.clear_settings_hold(db, box.id, tested)  # nothing to clear
    await mailboxes.set_enabled(db, box.id, True, actor=user, settings=settings, facts=FACTS)
    assert box.enabled and box.hold_reason is None


async def test_a_disabled_mailbox_is_held_too_and_a_stale_test_doesnt_clear(
    db: AsyncSession,
) -> None:
    """Every mailbox with a password is read (P6-J), so a disabled one's changed settings are
    held as well; a test of settings that changed again meanwhile clears nothing."""
    user = await seed_user(db)
    box = await mailboxes.create_mailbox(db, _form(), actor=user, facts=FACTS)
    await _tested(db, box)
    assert not box.enabled and not mailboxes.untested(box)
    await mailboxes.update_mailbox(
        db, box.id, _form(imap_host="imap.new.example"), actor=user, facts=FACTS
    )
    assert mailboxes.untested(box)
    tested = mailboxes.connection_snapshot(box)
    await mailboxes.update_mailbox(
        db, box.id, _form(imap_host="imap.newer.example"), actor=user, facts=FACTS
    )
    assert not await mailboxes.clear_settings_hold(db, box.id, tested)
    assert mailboxes.untested(box)


def test_google_host_matches_on_a_dot_boundary() -> None:
    def host(h: str) -> bool:
        return mailboxes.is_google_host(Mailbox(smtp_host=h))

    assert host("smtp.gmail.com") and host("gmail.com")
    assert not host("evilgmail.com") and not host("gmail.com.evil.org")


async def test_connection_passes_the_senders_reason_through_unchanged(
    db: AsyncSession, test_settings: Settings
) -> None:
    """Only the sender adapter's fixed wording ever reaches the page; this layer adds none."""
    box, creds = await _creds(db, test_settings)
    reader = FakeReader({"[Gmail]/Sent Mail": FakeFolder(1)})
    mail = fake_mail(FakeSender(check_error="fixed wording."), reader)
    problems = await mailboxes.test_connection(box, creds, mail)  # type: ignore[arg-type]
    assert problems == ["SMTP: fixed wording."]
