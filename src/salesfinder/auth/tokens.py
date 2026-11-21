"""Opaque random tokens; only their SHA-256 hashes are stored."""

import hashlib
import secrets


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
