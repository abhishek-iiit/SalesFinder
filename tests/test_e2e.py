"""End to end against fakes (Phase 7 Task 6): the real worker passes (``run_pass`` +
``record``) and the real web routes drive one buyer through discovery -> profiling ->
contacts -> a Claude draft -> a teammate's approval in the Outbox -> sending -> their reply ->
a drafted answer -> its approval -> sending, on a controlled clock.

Fakes only: Serper, HubSpot, the web (pinned fetches), DNS, ``FakeSender``/``FakeReader`` and
``tests/fakes/fake_claude``; the conftest guards (real Claude, network, DNS, mail) stay on.
The web's clock (``web.deps.utcnow`` as each route module imported it) is the same ``Clock``
the worker uses, so office hours, the warm-up ramp, spacing, follow-up due dates, quiet hours
and the gate's freshness checks all read one controlled time. Not covered: columns the
database fills itself (``created_at``/``updated_at`` server defaults) stay on real time.

The config and catalog are the shipped ones, loaded by conftest's fixtures (never at import,
never merged with a developer's ``salesfinder.local.yaml``)."""

import email
import email.message
import html
import json
import random
import re
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email import policy as email_policy
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from campaign_helpers import CLEAN, FACTS, MARKER
from conftest import FakeClaude
from fakes.dns import FakeResolver
from fakes.hubspot import FAKE_TOKEN, FakeHubSpot, company
from fakes.mail import FakeFolder, FakeReader, FakeSender, fake_mail
from fakes.serper import FAKE_KEY, FakeSerper, organic
from fakes.web import FakeWeb
from mail_helpers import reply_to
from salesfinder.auth.ratelimit import LoginRateLimiter
from salesfinder.campaigns import service as campaigns
from salesfinder.catalog import Catalog
from salesfinder.cohorts.evaluate import create_cohort
from salesfinder.cohorts.model import CohortDefinition, Condition
from salesfinder.config import AppConfig
from salesfinder.contacts.dns import MxChecker
from salesfinder.db.engine import make_session_factory
from salesfinder.db.models import (
    Company,
    Conversation,
    EmailAddress,
    Message,
    Person,
    Run,
    Sequence,
    Suppression,
    User,
)
from salesfinder.discovery import jobs
from salesfinder.ops.health import worker_health
from salesfinder.ops.runs import PassResult
from salesfinder.sending import state as sending_state
from salesfinder.sending.mailboxes import refresh_readiness
from salesfinder.settings import Settings
from salesfinder.signals import settings as signal_settings
from salesfinder.signals.latest import latest_values
from salesfinder.web import deps as web_deps
from salesfinder.web.app import create_app
from salesfinder.worker import NO_SEND_LINE, WorkerDeps, record, run_pass, start
from sending_helpers import BOX, SECRET, SENT_FOLDER, ready_mailbox
from web_helpers import csrf_from, login, text_of

# Tuesday 09:00 in New York: the first minute of a New Jersey buyer's office hours, and
# outside the default quiet hours (22:00-07:00 New York).
START = datetime(2026, 11, 10, 14, 0, tzinfo=UTC)

ACME, BETA, KNOWN = "acme-soap.com", "beta-clean.com", "known-co.com"
ACME_IP, BETA_IP = "93.184.216.34", "93.184.216.35"
# Not the helpers' "Jane Doe": the contact extractor drops "Doe" as a template placeholder
# ("John Doe"), so a team page naming Jane Doe yields no person.
JANE = "Jane Dobson"
RECIPIENT = "jane.dobson@acme-soap.com"

# fake_claude markers, first match wins: the reply classification, the reply answer, then
# the first-email writer (whose heading every writer prompt has).
CLASSIFY_MARKER = "REPLY (untrusted"
DRAFT_MARKER = "Write the reply"
INTERESTED: dict[str, Any] = {
    "intent": "interested",
    "confidence": 0.9,
    "summary": "They asked for the CoA for SLES.",
    "products": ["SLES"],
}
THEIR_ID = "<reply-0001@mail.acme-soap.com>"  # mail_helpers.reply_to's Message-ID
ANSWER = {
    "body": "Hi Jane,\n\nThanks for asking. We send the CoA and SDS with every order, so I can "
    "share the CoA for our SLES today.\n\nWould a sample help as well?\n\nThanks,"
}

SITES: dict[str, Any] = {
    f"https://{ACME}/": (
        "<title>Acme Soap Works</title>"
        "<h1>Acme Soap Works</h1>"
        "<p>We make shampoo and body wash with sodium laureth sulfate.</p>"
        '<a href="/team">Our team</a>'
    ),
    f"https://{ACME}/team": (
        "<title>Our Team</title>"
        f"<ul><li>{JANE} | Purchasing Manager</li></ul>"
        f"<p>Mail {RECIPIENT}</p>"
    ),
    f"https://{BETA}/": (
        "<title>Beta Clean</title>"
        "<h1>Beta Clean</h1>"
        "<p>Household cleaner and detergent made with sodium laureth sulfate and "
        "cocamidopropyl betaine.</p>"
    ),
}
# The mail servers of both buyers, and the sending domain's SPF/DKIM/DMARC/MX.
DNS_ANSWERS: dict[tuple[str, str], Any] = {
    (ACME, "MX"): [(10, f"mx.{ACME}.")],
    (BETA, "MX"): [(10, f"mx.{BETA}.")],
    ("send-domain.com", "TXT"): [(0, '"v=spf1 include:_spf.google.com ~all"')],
    ("google._domainkey.send-domain.com", "TXT"): [(0, '"v=DKIM1; k=rsa; p=MIIBIjANBgkq"')],
    ("_dmarc.send-domain.com", "TXT"): [(0, '"v=DMARC1; p=none; rua=mailto:d@send-domain.com"')],
    ("send-domain.com", "MX"): [(1, "aspmx.l.google.com.")],
}


def _ok(obj: dict[str, Any]) -> dict[str, Any]:
    """A successful fake_claude reply whose result is ``obj`` as JSON."""
    return {"type": "result", "subtype": "success", "is_error": False, "result": json.dumps(obj)}


class Clock:
    """The one controlled time of a test: the worker's ``deps.now`` and the web's ``utcnow``."""

    def __init__(self, at: datetime) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **delta: float) -> None:
        self.at += timedelta(**delta)


async def drive(
    deps: WorkerDeps, *, until: Callable[[], Awaitable[bool]], max_passes: int = 10
) -> list[PassResult]:
    """Real worker passes (``run_pass`` then ``record``, as ``run_forever`` does) until
    ``until()`` holds; fails after ``max_passes``, and if any pass had a failed step. Like
    ``run_forever``, the worker's run is opened (``start``) before its first pass."""
    if deps.run_id is None:
        await start(deps)
    results: list[PassResult] = []
    for _ in range(max_passes):
        result = await run_pass(deps)
        await record(deps, result)
        results.append(result)
        assert result.failures == [], result.failures
        if await until():
            return results
    pytest.fail(f"not reached in {max_passes} passes: {[r.lines for r in results]}")


@dataclass
class World:
    engine: AsyncEngine
    clock: Clock
    deps: WorkerDeps
    client: httpx.AsyncClient
    claude: FakeClaude
    sender: FakeSender
    inbox: FakeFolder
    web: FakeWeb
    serper: FakeSerper
    hubspot: FakeHubSpot
    admin: User
    password: str
    config: AppConfig

    def session(self) -> AsyncSession:
        return make_session_factory(self.engine)()

    def writer_calls(self) -> list[dict[str, Any]]:
        return [
            c
            for c in self.claude.calls()
            if MARKER in c["prompt"]
            and CLASSIFY_MARKER not in c["prompt"]
            and DRAFT_MARKER not in c["prompt"]
        ]

    def classify_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.claude.calls() if CLASSIFY_MARKER in c["prompt"]]

    def answer_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.claude.calls() if DRAFT_MARKER in c["prompt"]]

    async def first_email(self) -> Message | None:
        async with self.session() as s:
            row: Message | None = (
                await s.execute(select(Message).where(Message.step == 1))
            ).scalar_one_or_none()
            return row

    async def message(self, message_id: uuid.UUID) -> Message:
        async with self.session() as s:
            row = await s.get(Message, message_id)
            assert row is not None
            return row

    async def sequence(self) -> Sequence:
        async with self.session() as s:
            row: Sequence = (await s.execute(select(Sequence))).scalar_one()
            return row

    async def company(self, domain: str) -> Company:
        async with self.session() as s:
            return (await s.execute(select(Company).where(Company.domain == domain))).scalar_one()

    async def approve(self, message_id: uuid.UUID, *, view: str = "review") -> None:
        """A teammate approves ``message_id`` in the Outbox (POST with the page's CSRF
        token)."""
        page = await self.client.get("/outbox", params={"view": view})
        assert page.status_code == 200
        assert str(message_id) in page.text
        resp = await self.client.post(
            f"/outbox/{message_id}/approve", data={"csrf_token": csrf_from(page.text)}
        )
        assert resp.status_code == 303, resp.text
        assert "msg=approved" in resp.headers["location"]
        assert (await self.message(message_id)).status == "approved"

    async def edit(self, message_id: uuid.UUID, *, body: str) -> None:
        """A teammate edits the written part of ``message_id`` on its Outbox page."""
        page = await self.client.get(f"/outbox/{message_id}")
        assert page.status_code == 200
        version = re.search(r'name="version" value="([^"]+)"', page.text)
        assert version is not None
        stored = await self.message(message_id)
        resp = await self.client.post(
            f"/outbox/{message_id}",
            data={
                "csrf_token": csrf_from(page.text),
                "subject": stored.subject,
                "body": body,
                "version": text_of_attr(version.group(1)),
            },
        )
        assert resp.status_code == 303, text_of(resp)
        assert "msg=saved" in resp.headers["location"]

    async def export(self, side: str = "buyer") -> str:
        """An admin downloads the companies CSV from Settings (POST with the CSRF token)."""
        page = await self.client.get("/admin/settings")
        assert page.status_code == 200
        resp = await self.client.post(
            "/admin/export/companies", data={"csrf_token": csrf_from(page.text), "side": side}
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("text/csv")
        return resp.content.decode("utf-8-sig")

    async def runs(self) -> list[Run]:
        async with self.session() as s:
            return list((await s.scalars(select(Run).order_by(Run.started_at, Run.id))).all())

    async def run_texts(self) -> list[str]:
        return [item["text"] for run in await self.runs() for item in run.lines]


def text_of_attr(value: str) -> str:
    """An HTML attribute value as the browser submits it."""
    return html.unescape(value)


@pytest.fixture
def clock() -> Clock:
    return Clock(START)


@pytest.fixture
def resolver() -> FakeResolver:
    return FakeResolver(dict(DNS_ANSWERS))


@pytest.fixture
def settings(test_settings_with_fake_claude: Settings) -> Settings:
    return test_settings_with_fake_claude.model_copy(
        update={
            "hubspot_token": SecretStr(FAKE_TOKEN),
            "serper_api_key": SecretStr(FAKE_KEY),
            "mailbox_passwords": {SECRET: SecretStr("app-password")},
        }
    )


@pytest.fixture
def e2e_app(
    settings: Settings,
    engine: AsyncEngine,
    clock: Clock,
    resolver: FakeResolver,
    shipped_config: AppConfig,
    shipped_catalog: Catalog,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    """The web app as ``confirmed_app`` builds it (confirmed facts), with the shipped config
    and catalog, the fake resolver for the Outbox's MX refresh, and the test's clock."""
    app = create_app(
        settings, session_factory=make_session_factory(engine), rate_limiter=LoginRateLimiter()
    )
    app.state.facts = FACTS
    app.state.config = shipped_config
    app.state.catalog = shipped_catalog
    app.state.mx_checker = MxChecker(shipped_config.contacts, resolve=resolver)
    app.state.dns_resolve = resolver
    real_utcnow = web_deps.utcnow
    patched = 0
    for name, module in list(sys.modules.items()):
        if name.startswith("salesfinder.web") and getattr(module, "utcnow", None) is real_utcnow:
            monkeypatch.setattr(module, "utcnow", clock.now)
            patched += 1
    assert patched > 5  # deps and the route modules
    return app


@pytest_asyncio.fixture
async def e2e_client(e2e_app: FastAPI, db: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=e2e_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        yield c


async def _seed(
    db: AsyncSession,
    engine: AsyncEngine,
    admin: User,
    *,
    clock: Clock,
    resolver: FakeResolver,
    config: AppConfig,
    catalog: Catalog,
    resume: bool,
) -> None:
    """Free signals confirmed; a buyer cohort requiring USES_CATALOG_CHEMICAL; a campaign
    with the ready mailbox (its DNS checked against the fake resolver), activated; sending
    resumed (unless ``resume`` is False); and a discovery job estimated and approved."""
    now = clock()
    await signal_settings.confirm_free(db, actor=admin)
    cohort = await create_cohort(
        db,
        name="Buyers who use our chemicals",
        definition=CohortDefinition(
            side="buyer", must=[Condition(code="USES_CATALOG_CHEMICAL", op="present")]
        ),
        actor=admin,
    )
    campaign = await campaigns.create_campaign(
        db, name="SLES buyers", side="buyer", cohort_id=cohort.id, daily_draft_cap=40, actor=admin
    )
    box = await ready_mailbox(db, admin.id)
    box.dns_ok, box.dns_checked_at = False, None  # checked below, against the fake DNS
    await campaigns.set_mailbox(db, campaign.id, box.id, actor=admin)
    await campaigns.activate(db, campaign.id, actor=admin, facts=FACTS, config=config, now=now)
    if resume:
        await sending_state.resume(db, actor=admin, now=now - timedelta(hours=1))
    job = await jobs.create_estimate(
        db,
        side="buyer",
        product_ids=["sles"],
        industries=["personal_care"],
        region_names=["New Jersey"],
        created_by=admin,
        catalog=catalog,
        config=config.discovery,
    )
    await jobs.approve_job(db, job.id, approved_by=admin, now=now, config=config.discovery)
    await db.commit()
    report = await refresh_readiness(
        make_session_factory(engine), box.id, resolve=resolver, now=now, contacts=config.contacts
    )
    assert report.ok, report.to_json()


@pytest_asyncio.fixture
async def make_world(
    db: AsyncSession,
    engine: AsyncEngine,
    admin: User,
    password: str,
    settings: Settings,
    clock: Clock,
    resolver: FakeResolver,
    e2e_client: httpx.AsyncClient,
    fake_claude_env: FakeClaude,
    shipped_config: AppConfig,
    shipped_catalog: Catalog,
) -> Callable[..., Awaitable[World]]:
    async def make(*, resume: bool = True, send_enabled: bool = True) -> World:
        await _seed(
            db,
            engine,
            admin,
            clock=clock,
            resolver=resolver,
            config=shipped_config,
            catalog=shipped_catalog,
            resume=resume,
        )
        fake_claude_env.reply(CLASSIFY_MARKER, _ok(INTERESTED))
        fake_claude_env.reply(DRAFT_MARKER, _ok(ANSWER))
        fake_claude_env.reply(MARKER, _ok(CLEAN))
        hubspot = FakeHubSpot([company("1", "Known Co", domain=KNOWN)], [], [])
        serper = FakeSerper(
            lambda _payload: [
                organic(f"https://{ACME}/", "Acme Soap Works", position=1),
                organic(f"https://{BETA}/", "Beta Clean", position=2),
                organic(f"https://{KNOWN}/", "Known Co", position=3),
            ]
        )
        web = FakeWeb(SITES, {ACME: [ACME_IP], BETA: [BETA_IP]})
        inbox = FakeFolder(1)
        sender = FakeSender()
        reader = FakeReader({"INBOX": inbox, SENT_FOLDER: FakeFolder(1)})
        deps = WorkerDeps(
            session_factory=make_session_factory(engine),
            settings=settings,
            config=shipped_config,
            catalog=shipped_catalog,
            facts=FACTS,
            now=clock.now,
            hubspot_transport=hubspot.transport,
            serper_transport=serper.transport,
            web_transport=web.transport,
            web_resolver=web.resolver,
            dns_resolve=resolver,
            mail=fake_mail(sender, reader),
            rng=random.Random(7),  # noqa: S311
            send_enabled=send_enabled,
        )
        resp = await login(e2e_client, admin.email, password)
        assert resp.status_code == 303, resp.text
        return World(
            engine=engine,
            clock=clock,
            deps=deps,
            client=e2e_client,
            claude=fake_claude_env,
            sender=sender,
            inbox=inbox,
            web=web,
            serper=serper,
            hubspot=hubspot,
            admin=admin,
            password=password,
            config=shipped_config,
        )

    return make


def _wire(message: email.message.EmailMessage) -> email.message.EmailMessage:
    """The message as the server receives it, parsed back."""
    parsed = email.message_from_bytes(message.as_bytes(), policy=email_policy.default)
    assert isinstance(parsed, email.message.EmailMessage)
    return parsed


def _body(message: email.message.EmailMessage) -> str:
    content = _wire(message).get_content()
    assert isinstance(content, str)
    return content.replace("\r\n", "\n")


def _ends_line(body: str) -> str:
    """The stored body as the email API sends it (it ends a body with a line break)."""
    return body if body.endswith("\n") else body + "\n"


async def _first_email_waiting(world: World) -> Callable[[], Awaitable[bool]]:
    async def until() -> bool:
        first = await world.first_email()
        return first is not None and first.status == "awaiting_approval"

    return until


def _passes(count: int) -> Callable[[], Awaitable[bool]]:
    """Holds after ``count`` passes (to show what a few more passes don't do)."""
    done = 0

    async def until() -> bool:
        nonlocal done
        done += 1
        return done >= count

    return until


def _sent(world: World, count: int) -> Callable[[], Awaitable[bool]]:
    async def until() -> bool:
        return len(world.sender.sent) >= count

    return until


async def _send_first_email(world: World, *, edit: bool = False) -> Message:
    """Drive to the first draft, (optionally edit it,) approve it in the Outbox and drive
    until it is sent. Returns the stored message as sent."""
    await drive(world.deps, until=await _first_email_waiting(world))
    first = await world.first_email()
    assert first is not None
    if edit:
        edited = CLEAN["first_body"].replace("Hi there,", "Hi Jane,")
        await world.edit(first.id, body=edited)
    await world.approve(first.id)
    await drive(world.deps, until=_sent(world, 1))
    stored = await world.message(first.id)
    assert stored.status == "sent"
    return stored


# --------------------------------------------------------------------------- the buyer loop


async def test_buyer_loop_end_to_end(make_world: Callable[..., Awaitable[World]]) -> None:
    world = await make_world()

    # 1. Discovery -> profiling -> contacts -> a first draft waiting for a teammate.
    await drive(world.deps, until=await _first_email_waiting(world))

    known = await world.company(KNOWN)
    assert known.status == "excluded"
    assert known.profiled_at is None and known.contacts_checked_at is None  # never profiled
    assert KNOWN not in world.web.lookups
    acme = await world.company(ACME)
    async with world.session() as s:
        found = await latest_values(s, ["USES_CATALOG_CHEMICAL"], [acme.id])
        jane = (await s.execute(select(Person).where(Person.company_id == acme.id))).scalar_one()
        address = (
            await s.execute(select(EmailAddress).where(EmailAddress.company_id == acme.id))
        ).scalar_one()
    assert found[acme.id]["USES_CATALOG_CHEMICAL"]  # profiled with the signal
    assert (jane.full_name, jane.is_target) == (JANE, True)
    assert (address.address, address.status, address.person_id) == (
        RECIPIENT,
        "published",
        jane.id,
    )
    assert len(world.writer_calls()) == 1  # Acme only: Beta Clean has nobody to write to
    sequence = await world.sequence()
    assert (sequence.company_id, sequence.recipient) == (acme.id, RECIPIENT)

    # 2. A teammate edits and approves it in the Outbox; the next pass sends it.
    first = await world.first_email()
    assert first is not None
    edited = CLEAN["first_body"].replace("Hi there,", "Hi Jane,")
    await world.edit(first.id, body=edited)
    await world.approve(first.id)
    await drive(world.deps, until=_sent(world, 1))

    stored = await world.message(first.id)
    assert stored.status == "sent" and stored.sent_at == START
    [(from_box, envelope_to, mime)] = world.sender.sent
    assert (from_box, envelope_to) == (BOX, RECIPIENT)
    assert "Hi Jane," in stored.body  # the teammate's edit is what went out
    assert stored.subject == CLEAN["first_subject"]  # the subject Claude wrote
    assert _body(mime) == _ends_line(stored.body)
    wire = _wire(mime)
    assert wire["Subject"] == stored.subject
    assert str(wire["From"]) == f"Asha Rao <{BOX}>"  # the confirmed buyer persona
    assert wire["Message-ID"] == stored.message_id_header
    assert wire["In-Reply-To"] is None and wire["References"] is None
    sent_id = stored.message_id_header
    assert sent_id is not None

    # 3. Jane replies; the next mail read (5 minutes on) classifies it and drafts an answer.
    world.inbox.add(
        reply_to(
            sent_id,
            body="Could you send the CoA for SLES?",
            sender=f"{JANE} <{RECIPIENT}>",
            date=world.clock(),
        )
    )
    world.clock.advance(minutes=6)

    async def answer_waiting() -> bool:
        async with world.session() as s:
            answer = (
                await s.execute(select(Message).where(Message.kind == "reply"))
            ).scalar_one_or_none()
        return answer is not None and answer.status == "awaiting_approval"

    await drive(world.deps, until=answer_waiting)

    async with world.session() as s:
        step2 = (await s.execute(select(Message).where(Message.step == 2))).scalar_one()
        answer = (await s.execute(select(Message).where(Message.kind == "reply"))).scalar_one()
        conversation = (await s.execute(select(Conversation))).scalar_one()
    assert step2.status == "cancelled"
    assert (await world.sequence()).status == "replied"
    assert answer.in_reply_to == THEIR_ID
    assert conversation.intent == "interested"
    assert len(world.classify_calls()) == 1 and len(world.answer_calls()) == 1
    assert len(world.writer_calls()) == 1  # no new first email

    # 4. A teammate approves the answer; once the mailbox's spacing has passed, it goes.
    await world.approve(answer.id)
    world.clock.advance(minutes=10)  # beyond sending.max_spacing_seconds (9 min)
    await drive(world.deps, until=_sent(world, 2))

    sent_answer = await world.message(answer.id)
    assert sent_answer.status == "sent"
    _, envelope_to, mime = world.sender.sent[1]
    assert envelope_to == RECIPIENT
    wire = _wire(mime)
    assert wire["In-Reply-To"] == THEIR_ID
    assert sent_id in str(wire["References"]) and THEIR_ID in str(wire["References"])
    assert wire["Message-ID"] == sent_answer.message_id_header
    assert sent_answer.message_id_header not in (None, sent_id)
    assert wire["Subject"] == sent_answer.subject
    assert _body(mime) == _ends_line(sent_answer.body)
    assert ANSWER["body"].rstrip(",") in sent_answer.body  # the answer Claude wrote
    assert len(world.sender.sent) == 2  # nothing else went out

    # 5. The heartbeat log: no failures, the send recorded, the worker running.
    runs = await world.runs()
    assert runs and all(run.failures == [] for run in runs)
    assert "Sent 1 email(s)." in await world.run_texts()
    async with world.session() as s:
        health = await worker_health(
            s, now=world.clock(), config=world.config, code_version=world.deps.code_version
        )
    assert health.state == "running"
    page = await world.client.get("/runs")
    assert page.status_code == 200
    assert "Sent 1 email(s)." in text_of(page)
    assert world.serper.violations == world.web.violations == world.hubspot.violations == []
    world.hubspot.assert_read_only()


# --------------------------------------------------------------------------- opt-out + export


async def test_unsubscribe_and_export_end_to_end(
    make_world: Callable[..., Awaitable[World]],
) -> None:
    world = await make_world()
    sent = await _send_first_email(world)
    assert sent.message_id_header is not None
    assert (await world.company(KNOWN)).status == "excluded"

    # Before the opt-out, the export lists Acme with Jane, and Beta Clean (not Known Co).
    before = await world.export()
    assert ACME in before and JANE in before and RECIPIENT in before and BETA in before
    assert KNOWN not in before

    world.inbox.add(
        reply_to(
            sent.message_id_header,
            body="Please remove me from your list",
            sender=f"{JANE} <{RECIPIENT}>",
            subject="Re: " + sent.subject,
            date=world.clock(),
        )
    )
    world.clock.advance(minutes=6)

    async def suppressed() -> bool:
        async with world.session() as s:
            return bool(await s.scalar(select(func.count()).select_from(Suppression)))

    await drive(world.deps, until=suppressed)

    async with world.session() as s:
        suppression = (await s.execute(select(Suppression))).scalar_one()
        conversation = (await s.execute(select(Conversation))).scalar_one()
        unsent = (
            await s.scalars(select(Message).where(Message.status.not_in(["sent", "cancelled"])))
        ).all()
    assert (suppression.address, suppression.reason) == (RECIPIENT, "unsubscribed")
    assert (await world.sequence()).status == "stopped"
    assert (conversation.status, conversation.intent) == ("handled", "unsubscribe")
    assert unsent == []  # the follow-up is cancelled; nothing else waits
    assert world.classify_calls() == []  # the opt-out needs no Claude

    # A week on, past the follow-up's due date (4 business days): the cancelled follow-up is
    # never re-queued for review (a queued one would only wait in the Outbox anyway), and
    # nothing else goes to Jane.
    queued_before = sum(t.startswith("Queued ") for t in await world.run_texts())
    world.clock.advance(days=7)
    await drive(world.deps, until=_passes(2))
    assert len(world.sender.sent) == 1
    async with world.session() as s:
        statuses = (await s.scalars(select(Message.status).order_by(Message.step))).all()
    assert list(statuses) == ["sent", "cancelled"]
    texts = await world.run_texts()
    assert sum(t.startswith("Queued ") for t in texts) == queued_before == 0

    # The admin's CSV export leaves out the suppressed and the excluded company. (A week on,
    # the admin's session has expired: they sign in again.)
    resp = await login(world.client, world.admin.email, world.password)
    assert resp.status_code == 303
    body = await world.export()
    assert BETA in body
    assert ACME not in body and RECIPIENT not in body and JANE not in body
    assert KNOWN not in body


# --------------------------------------------------------------------------- the dry run


async def test_dry_run_mode_drafts_but_never_sends(
    make_world: Callable[..., Awaitable[World]],
) -> None:
    # Sending never resumed (paused by default) and the worker runs with --no-send.
    world = await make_world(resume=False, send_enabled=False)

    await drive(world.deps, until=await _first_email_waiting(world))
    first = await world.first_email()
    assert first is not None
    await world.approve(first.id)

    world.clock.advance(minutes=6)  # mail is read again, follow-ups and writing run again
    await drive(world.deps, until=_passes(3))

    assert world.sender.sent == []
    assert (await world.message(first.id)).status == "approved"
    page = await world.client.get("/outbox", params={"view": "approved"})
    assert page.status_code == 200
    shown = text_of(page)
    assert str(first.id) in page.text
    assert f"Sending is paused: {sending_state.NOT_TURNED_ON}" in shown
    texts = await world.run_texts()
    assert texts.count(NO_SEND_LINE) == 1
    assert not any(text.startswith("Sent ") for text in texts)
    assert len(await world.runs()) == 1


async def test_paused_sending_holds_approved_mail(
    make_world: Callable[..., Awaitable[World]],
) -> None:
    # Sending never resumed (paused by default); the worker may send (no --no-send).
    world = await make_world(resume=False)

    await drive(world.deps, until=await _first_email_waiting(world))
    first = await world.first_email()
    assert first is not None
    await world.approve(first.id)
    world.clock.advance(minutes=6)
    await drive(world.deps, until=_passes(3))

    assert world.sender.sent == []
    assert (await world.message(first.id)).status == "approved"
    texts = await world.run_texts()
    assert texts.count(sending_state.NOT_TURNED_ON) == 1
    assert NO_SEND_LINE not in texts
    assert not any(text.startswith("Sent ") for text in texts)
    assert len(await world.runs()) == 1
