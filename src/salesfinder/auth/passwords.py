"""Argon2id password hashing and the password policy."""

import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_PASSWORD_LENGTH = 12

_hasher = PasswordHasher()


def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(stored_hash: str, plain: str) -> bool:
    """True only if ``plain`` matches; never raises on mismatch or a malformed hash."""
    try:
        return _hasher.verify(stored_hash, plain)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(stored_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except (InvalidHashError, ValueError):
        return True


def password_problems(plain: str) -> list[str]:
    problems: list[str] = []
    if len(plain) < MIN_PASSWORD_LENGTH:
        problems.append(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    return problems


# Computed once at import (not lazily) so the first unknown-email login is not measurably slower.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(32))


def dummy_hash() -> str:
    """A real Argon2 hash of a random throwaway password, used to equalise login timing."""
    return _DUMMY_HASH
