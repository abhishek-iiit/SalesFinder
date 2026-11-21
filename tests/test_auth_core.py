import uuid
from datetime import UTC, datetime, timedelta

import pytest

from salesfinder.auth import csrf, emails, passwords, ratelimit, tokens


def test_hash_token_is_sha256_hex() -> None:
    assert len(tokens.hash_token(tokens.new_token())) == 64


def test_password_roundtrip_and_rules() -> None:
    h = passwords.hash_password("correct horse battery")
    assert passwords.verify_password(h, "correct horse battery")
    assert not passwords.verify_password(h, "wrong password!!")
    assert passwords.password_problems("short") == ["Password must be at least 12 characters."]


def test_normalize_email() -> None:
    assert emails.normalize_email("  Alice@Elchemy.COM ") == "alice@elchemy.com"
    with pytest.raises(ValueError, match="valid email"):
        emails.normalize_email("not-an-email")


def test_rate_limiter_blocks_after_five_then_expires() -> None:
    rl, now = ratelimit.LoginRateLimiter(), datetime(2026, 1, 1, tzinfo=UTC)
    for _ in range(5):
        rl.record_failure("a|ip", now)
    assert rl.is_blocked("a|ip", now)
    assert not rl.is_blocked("a|ip", now + timedelta(minutes=16))
    assert not rl.is_blocked("b|ip", now)


def test_csrf() -> None:
    sid = uuid.uuid4()
    t = csrf.csrf_token("s" * 40, sid)
    assert csrf.verify_csrf("s" * 40, sid, t)
    assert not csrf.verify_csrf("s" * 40, uuid.uuid4(), t)
    assert not csrf.verify_csrf("s" * 40, sid, None)
