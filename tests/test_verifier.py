"""MillionVerifier client and daily cap. No network: MockTransport."""

import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from fakes.verifier import FAKE_KEY, FakeMillionVerifier
from salesfinder.contacts.verifier import MillionVerifier, NoVerifier, daily_count
from salesfinder.db.models import Company, EmailAddress

NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"result": "ok"}, "verified"),
        ({"result": "catch_all"}, "risky"),
        ({"result": "invalid"}, "invalid"),
        ({"result": "disposable"}, "invalid"),
        ({"result": "unknown"}, "unknown"),
        ({"result": "ok", "error": ""}, "verified"),
        ({"result": "weird"}, "unknown"),
    ],
)
async def test_mapping(payload: Any, expected: str) -> None:
    fake = FakeMillionVerifier(payload)
    mv = MillionVerifier(FAKE_KEY, transport=fake.transport())
    assert await mv.verify("a@b.com") == expected
    assert (mv.failures, mv.consecutive_failures) == (0, 0)
    await mv.aclose()


@pytest.mark.parametrize(
    "payload",
    [
        {"error": "Invalid API key", "result": ""},
        {"error": "Insufficient credits"},
        {"result": "error"},
        {"nope": 1},
        ["not", "a", "dict"],
    ],
)
async def test_error_payloads_are_failures(payload: Any) -> None:
    fake = FakeMillionVerifier(payload)
    mv = MillionVerifier(FAKE_KEY, transport=fake.transport())
    assert await mv.verify("a@b.com") is None
    assert await mv.verify("c@b.com") is None
    assert (mv.failures, mv.consecutive_failures) == (2, 2)
    fake.payload = {"result": "ok"}
    assert await mv.verify("d@b.com") == "verified"
    assert (mv.failures, mv.consecutive_failures) == (2, 0)
    await mv.aclose()


async def test_request_shape() -> None:
    fake = FakeMillionVerifier()
    mv = MillionVerifier(FAKE_KEY, transport=fake.transport())
    await mv.verify("a+b@acme.com")
    (request,) = fake.requests
    assert request.method == "GET"
    assert request.url.host == "api.millionverifier.com"
    assert request.url.path == "/api/v3/"
    qs = parse_qs(request.url.query.decode())
    assert qs == {"api": [FAKE_KEY], "email": ["a+b@acme.com"], "timeout": ["10"]}
    assert "a+b" not in request.url.query.decode()  # '+' is encoded
    await mv.aclose()


async def test_http_errors_are_failures_and_key_hidden(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    cases = [
        FakeMillionVerifier(status=500),
        FakeMillionVerifier(status=401),
        FakeMillionVerifier(body=b"<html>not json"),
        FakeMillionVerifier(raises=httpx.ReadTimeout("timed out")),
        FakeMillionVerifier(raises=httpx.ConnectError("refused")),
    ]
    for fake in cases:
        mv = MillionVerifier(FAKE_KEY, transport=fake.transport())
        assert await mv.verify("a@b.com") is None
        assert mv.failures == 1
        assert FAKE_KEY not in repr(mv)
        await mv.aclose()
    assert FAKE_KEY not in caplog.text
    assert all(FAKE_KEY not in r.getMessage() for r in caplog.records)


async def test_no_verifier_is_unknown() -> None:
    assert await NoVerifier().verify("a@b.com") == "unknown"
    assert NoVerifier().consecutive_failures == 0


async def test_daily_count(db: AsyncSession) -> None:
    company = Company(
        side="buyer", status="new", name="Acme", domain="acme.com", website_url="https://acme.com/"
    )
    db.add(company)
    await db.flush()

    def addr(a: str, at: datetime | None) -> EmailAddress:
        return EmailAddress(
            company_id=company.id,
            address=a,
            kind="personal",
            status="verified",
            source="page",
            verified_at=at,
        )

    db.add_all(
        [
            addr("a@acme.com", datetime(2026, 11, 8, 0, 0, tzinfo=UTC)),
            addr("b@acme.com", datetime(2026, 11, 8, 23, 59, tzinfo=UTC)),
            addr("c@acme.com", datetime(2026, 11, 7, 23, 59, tzinfo=UTC)),
            addr("d@acme.com", datetime(2026, 11, 9, 0, 0, tzinfo=UTC)),
            addr("e@acme.com", None),
        ]
    )
    await db.flush()
    assert await daily_count(db, now=NOW) == 2
