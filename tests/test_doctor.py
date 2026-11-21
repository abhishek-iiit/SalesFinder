from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from campaign_helpers import seed_mailbox
from fakes.dns import FakeResolver
from fakes.mail import FakeFolder, FakeReader, FakeSender, fake_mail
from salesfinder.brain.cli_flags import REQUIRED_FLAGS, missing_flags
from salesfinder.config import AppConfig
from salesfinder.db.models import AppSetting, HubSpotSync, User
from salesfinder.doctor import run_checks
from salesfinder.mail.factory import MailFactory
from salesfinder.ops import network, runs
from salesfinder.ops.version import code_version
from salesfinder.sending.mailboxes import NOT_TESTED, SETTINGS_CHANGED
from salesfinder.settings import Settings
from test_readiness import DOMAIN, GOOD

NAMES = [
    "config",
    "database",
    "migrations",
    "admin_user",
    "claude_cli",
    "https",
    "network",
    "hubspot",
    "serper",
    "mailboxes",
    "sending",
    "worker",
]


async def _seed_mailbox(db: AsyncSession, user: User) -> None:
    await seed_mailbox(
        db,
        user,
        address=f"asha@{DOMAIN}",
        enabled=True,
        secret_name="box",  # noqa: S106
    )
    await db.commit()


def _mail(check_error: str | None = None) -> MailFactory:
    return fake_mail(
        FakeSender(check_error=check_error), FakeReader({"[Gmail]/Sent Mail": FakeFolder(1)})
    )


def test_missing_flags() -> None:
    assert missing_flags(" ".join(REQUIRED_FLAGS)) == []
    assert missing_flags("--tools --output-format") == [
        f for f in REQUIRED_FLAGS if f not in ("--tools", "--output-format")
    ]


async def test_doctor_all_green(
    test_settings_with_fake_claude: Settings, admin: User, db: AsyncSession
) -> None:
    now = datetime.now(UTC)
    db.add(HubSpotSync(status="succeeded", started_at=now, finished_at=now))
    await db.commit()
    await _seed_mailbox(db, admin)
    settings = test_settings_with_fake_claude.model_copy(
        update={
            "hubspot_token": SecretStr("t"),
            "serper_api_key": SecretStr("k"),
            "mailbox_passwords": {"box": SecretStr("pw")},
        }
    )
    results = await run_checks(settings, mail=_mail(), dns_resolve=FakeResolver(GOOD))
    assert [r.name for r in results] == NAMES
    assert all(r.ok for r in results), results


async def test_doctor_flags_missing(
    test_settings_with_fake_claude: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_DROP", "--safe-mode")
    results = {r.name: r for r in await run_checks(test_settings_with_fake_claude)}
    assert not results["claude_cli"].ok
    assert "--safe-mode" in results["claude_cli"].detail


async def test_doctor_claude_absent(test_settings: Settings) -> None:
    settings = test_settings.model_copy(update={"claude_bin": "definitely-not-claude"})
    results = {r.name: r for r in await run_checks(settings)}
    assert not results["claude_cli"].ok
    assert results["claude_cli"].detail == "Claude CLI not found on PATH."


async def test_doctor_no_admin(test_settings_with_fake_claude: Settings, db: object) -> None:
    results = {r.name: r for r in await run_checks(test_settings_with_fake_claude)}
    assert not results["admin_user"].ok


async def test_doctor_lan_missing_cert_file(
    test_settings_with_fake_claude: Settings, tmp_path: Path
) -> None:
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("x")
    key.write_text("x")
    settings = test_settings_with_fake_claude.model_copy(
        update={"bind_host": "0.0.0.0", "tls_certfile": cert, "tls_keyfile": key}  # noqa: S104
    )
    assert {r.name: r for r in await run_checks(settings)}["https"].ok
    cert.unlink()
    result = {r.name: r for r in await run_checks(settings)}["https"]
    assert not result.ok
    assert "cert" in result.detail


async def test_doctor_unexecutable_claude_isolated(
    test_settings_with_fake_claude: Settings, tmp_path: Path
) -> None:
    bad = tmp_path / "noshebang"
    bad.write_text("not a script\n")
    bad.chmod(0o755)
    settings = test_settings_with_fake_claude.model_copy(update={"claude_bin": str(bad)})
    results = await run_checks(settings)
    assert [r.name for r in results] == NAMES
    assert not {r.name: r for r in results}["claude_cli"].ok


async def test_doctor_unexpected_exception_isolated(
    test_settings_with_fake_claude: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_: object) -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setattr("salesfinder.doctor.load_config", boom)
    results = await run_checks(test_settings_with_fake_claude)
    assert [r.name for r in results] == NAMES
    config = results[0]
    assert not config.ok
    assert "RuntimeError" in config.detail


async def test_doctor_https_checks_files_on_loopback(
    test_settings_with_fake_claude: Settings, tmp_path: Path
) -> None:
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("x")
    settings = test_settings_with_fake_claude.model_copy(
        update={"tls_certfile": cert, "tls_keyfile": key}
    )
    result = {r.name: r for r in await run_checks(settings)}["https"]
    assert not result.ok and "key" in result.detail
    key.write_text("x")
    assert {r.name: r for r in await run_checks(settings)}["https"].ok


async def test_doctor_https_half_configured(test_settings_with_fake_claude: Settings) -> None:
    settings = test_settings_with_fake_claude.model_copy(update={"tls_certfile": Path("/x.pem")})
    result = {r.name: r for r in await run_checks(settings)}["https"]
    assert not result.ok
    assert "both" in result.detail


async def test_doctor_hubspot_and_serper_missing(test_settings_with_fake_claude: Settings) -> None:
    results = {r.name: r for r in await run_checks(test_settings_with_fake_claude)}
    assert not results["hubspot"].ok
    assert results["hubspot"].detail == (
        "SALESFINDER_HUBSPOT_TOKEN is not set — cold sending will stay paused."
    )
    assert not results["serper"].ok
    assert results["serper"].detail == (
        "SALESFINDER_SERPER_API_KEY is not set — discovery jobs can't run."
    )


async def test_doctor_hubspot_token_without_sync_is_not_ok(
    test_settings_with_fake_claude: Settings, db: AsyncSession
) -> None:
    settings = test_settings_with_fake_claude.model_copy(update={"hubspot_token": SecretStr("t")})
    result = {r.name: r for r in await run_checks(settings)}["hubspot"]
    assert not result.ok
    assert "never been synced" in result.detail


async def test_doctor_hubspot_fresh_and_serper_configured(
    test_settings_with_fake_claude: Settings, db: AsyncSession
) -> None:
    now = datetime.now(UTC)
    db.add(HubSpotSync(status="succeeded", started_at=now, finished_at=now))
    await db.commit()
    settings = test_settings_with_fake_claude.model_copy(
        update={"hubspot_token": SecretStr("t"), "serper_api_key": SecretStr("k")}
    )
    results = {r.name: r for r in await run_checks(settings)}
    assert results["hubspot"].ok and "last synced" in results["hubspot"].detail
    assert results["serper"].ok and results["serper"].detail == "key configured"


async def test_doctor_config_requires_catalog_and_facts(
    test_settings_with_fake_claude: Settings,
) -> None:
    (test_settings_with_fake_claude.config_dir / "facts.yaml").unlink()
    result = {r.name: r for r in await run_checks(test_settings_with_fake_claude)}["config"]
    assert not result.ok
    assert "facts.yaml" in result.detail


async def test_doctor_no_mailboxes(test_settings_with_fake_claude: Settings) -> None:
    results = {r.name: r for r in await run_checks(test_settings_with_fake_claude)}
    assert not results["mailboxes"].ok
    assert results["mailboxes"].detail == "No mailboxes configured — nothing can send."
    assert results["sending"].ok
    assert results["sending"].detail == "Sending is paused: Sending hasn't been turned on yet."


async def test_doctor_sending_state(
    test_settings_with_fake_claude: Settings, db: AsyncSession
) -> None:
    db.add(AppSetting(key="sending", value={"paused": False}))
    await db.commit()
    assert {r.name: r for r in await run_checks(test_settings_with_fake_claude)}[
        "sending"
    ].detail == ("Sending is on.")
    row = await db.get(AppSetting, "sending")
    assert row is not None
    row.value = {"paused": True, "reason": "Paused automatically: 3 of the last 50 bounced."}
    await db.commit()
    assert {r.name: r for r in await run_checks(test_settings_with_fake_claude)}[
        "sending"
    ].detail == ("Sending is paused: Paused automatically: 3 of the last 50 bounced.")


async def test_doctor_mailbox_connection_failure_uses_fixed_copy(
    test_settings_with_fake_claude: Settings, admin: User, db: AsyncSession
) -> None:
    await _seed_mailbox(db, admin)
    settings = test_settings_with_fake_claude.model_copy(
        update={"mailbox_passwords": {"box": SecretStr("pw")}}
    )
    mail = _mail(check_error="couldn't sign in.")
    results = {
        r.name: r for r in await run_checks(settings, mail=mail, dns_resolve=FakeResolver(GOOD))
    }
    assert not results["mailboxes"].ok
    assert results["mailboxes"].detail == f"asha@{DOMAIN}: SMTP: couldn't sign in."
    assert "pw" not in results["mailboxes"].detail.split()


async def test_doctor_mailbox_without_password_or_dns(
    test_settings_with_fake_claude: Settings, admin: User, db: AsyncSession
) -> None:
    await _seed_mailbox(db, admin)
    bad_dns = FakeResolver({**GOOD, (f"_dmarc.{DOMAIN}", "TXT"): []})
    results = {
        r.name: r
        for r in await run_checks(test_settings_with_fake_claude, mail=_mail(), dns_resolve=bad_dns)
    }
    assert results["mailboxes"].detail == (
        f'asha@{DOMAIN}: Set SALESFINDER_MAILBOX_PASSWORDS for "box" in .env and restart.'
    )
    settings = test_settings_with_fake_claude.model_copy(
        update={"mailbox_passwords": {"box": SecretStr("pw")}}
    )
    results = {r.name: r for r in await run_checks(settings, mail=_mail(), dns_resolve=bad_dns)}
    assert not results["mailboxes"].ok
    assert results["mailboxes"].detail == f"asha@{DOMAIN}: No DMARC record."


async def test_doctor_worker_never_ran_is_ok(test_settings_with_fake_claude: Settings) -> None:
    result = {r.name: r for r in await run_checks(test_settings_with_fake_claude)}["worker"]
    assert result.ok and result.detail == "hasn't run yet"


async def test_doctor_worker_running_stopped_and_mismatched(
    test_settings_with_fake_claude: Settings, db: AsyncSession
) -> None:
    now = datetime.now(UTC)
    run = await runs.start_run(db, now=now, host="mac", pid=1, code_version=code_version())
    await db.commit()
    result = {r.name: r for r in await run_checks(test_settings_with_fake_claude)}["worker"]
    assert result.ok and result.detail == "running, first pass under way"

    await runs.finish_run(db, run.id, now=now - timedelta(hours=3), reason="shutdown")
    await db.commit()
    result = {r.name: r for r in await run_checks(test_settings_with_fake_claude)}["worker"]
    assert not result.ok
    assert result.detail.startswith("The worker isn't running (stopped 3 h ago).")

    # The same started_at as the finished row: the open row is the latest (final wave T3).
    await runs.start_run(db, now=now, host="mac", pid=1, code_version="old000000000")
    await db.commit()
    result = {r.name: r for r in await run_checks(test_settings_with_fake_claude)}["worker"]
    assert not result.ok and "restart both together" in result.detail


async def test_doctor_warns_about_a_no_send_worker(
    test_settings_with_fake_claude: Settings, db: AsyncSession
) -> None:
    """Final review M1 / Ruling P7-J: a leftover --no-send worker is a doctor warning."""
    now = datetime.now(UTC)
    run = await runs.start_run(db, now=now, host="mac", pid=1, code_version=code_version())
    await runs.record_pass(
        db, run.id, runs.PassResult([runs.NO_SEND_LINE], [], False), now=now, config=AppConfig()
    )
    await db.commit()
    result = {r.name: r for r in await run_checks(test_settings_with_fake_claude)}["worker"]
    assert not result.ok
    assert result.detail.startswith(
        "Running with --no-send (nothing will be sent), last pass less than a minute ago."
    )


async def test_doctor_lists_mailboxes_not_read(
    test_settings_with_fake_claude: Settings, admin: User, db: AsyncSession
) -> None:
    await _seed_mailbox(db, admin)  # enabled, tested: checked as before
    held = await seed_mailbox(db, admin, address=f"held@{DOMAIN}", secret_name="held")  # noqa: S106
    held.hold_reason = NOT_TESTED
    changed = await seed_mailbox(db, admin, address=f"moved@{DOMAIN}", secret_name="moved")  # noqa: S106
    changed.hold_reason = SETTINGS_CHANGED
    nopw = await seed_mailbox(db, admin, address=f"nopw@{DOMAIN}", secret_name="nopw")  # noqa: S106
    nopw.hold_reason = NOT_TESTED
    await db.commit()
    settings = test_settings_with_fake_claude.model_copy(
        update={
            "mailbox_passwords": {
                "box": SecretStr("pw"),
                "held": SecretStr("pw"),
                "moved": SecretStr("pw"),
            }
        }
    )
    sender = FakeSender()
    mail = fake_mail(sender, FakeReader({"[Gmail]/Sent Mail": FakeFolder(1)}))
    results = {
        r.name: r for r in await run_checks(settings, mail=mail, dns_resolve=FakeResolver(GOOD))
    }
    assert not results["mailboxes"].ok
    assert results["mailboxes"].detail == (
        f"held@{DOMAIN}: not read until Test connection passes — opt-outs may be waiting. "
        f"moved@{DOMAIN}: not read until Test connection passes — opt-outs may be waiting."
    )
    # Only the tested, enabled mailbox was logged in to.
    assert sender.checked == [f"asha@{DOMAIN}"]


async def test_doctor_not_read_without_enabled_mailboxes(
    test_settings_with_fake_claude: Settings, admin: User, db: AsyncSession
) -> None:
    held = await seed_mailbox(db, admin, address=f"held@{DOMAIN}", secret_name="held")  # noqa: S106
    held.hold_reason = NOT_TESTED
    await db.commit()
    settings = test_settings_with_fake_claude.model_copy(
        update={"mailbox_passwords": {"held": SecretStr("pw")}}
    )
    result = {r.name: r for r in await run_checks(settings)}["mailboxes"]
    assert not result.ok
    assert result.detail == (
        "No mailboxes configured — nothing can send. "
        f"held@{DOMAIN}: not read until Test connection passes — opt-outs may be waiting."
    )


async def test_doctor_network_fails_for_lan_with_loopback_only_hosts(
    test_settings_with_fake_claude: Settings, tmp_path: Path
) -> None:
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("x")
    key.write_text("x")
    settings = test_settings_with_fake_claude.model_copy(
        update={
            "bind_host": "0.0.0.0",  # noqa: S104
            "tls_certfile": cert,
            "tls_keyfile": key,
            "public_url": "https://127.0.0.1:5555",
        }
    )
    result = {r.name: r for r in await run_checks(settings)}["network"]
    assert not result.ok
    assert result.detail == network.LOOPBACK_ONLY


async def test_doctor_network_lists_every_warning(
    test_settings_with_fake_claude: Settings, tmp_path: Path
) -> None:
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("x")
    key.write_text("x")
    settings = test_settings_with_fake_claude.model_copy(
        update={
            "bind_host": "0.0.0.0",  # noqa: S104
            "tls_certfile": cert,
            "tls_keyfile": key,
            "allowed_hosts": ["*"],
        }
    )
    result = {r.name: r for r in await run_checks(settings)}["network"]
    assert not result.ok
    assert result.detail == " ".join([network.WILDCARD, network.NO_PUBLIC_URL])


async def test_doctor_network_ok_shows_mode(
    test_settings_with_fake_claude: Settings, tmp_path: Path
) -> None:
    result = {r.name: r for r in await run_checks(test_settings_with_fake_claude)}["network"]
    assert result.ok and result.detail == "Loopback only (127.0.0.1)"
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    cert.write_text("x")
    key.write_text("x")
    settings = test_settings_with_fake_claude.model_copy(
        update={
            "bind_host": "0.0.0.0",  # noqa: S104
            "port": 5555,
            "tls_certfile": cert,
            "tls_keyfile": key,
            "allowed_hosts": ["salesfinder.local", "127.0.0.1"],
            "public_url": "https://salesfinder.local:5555",
        }
    )
    result = {r.name: r for r in await run_checks(settings)}["network"]
    assert result.ok and result.detail == "LAN over HTTPS on 0.0.0.0:5555"
