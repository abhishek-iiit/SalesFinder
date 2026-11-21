"""CSRF tokens bound to a server-side session id."""

import hashlib
import hmac
import uuid


def csrf_token(secret: str, session_id: uuid.UUID) -> str:
    message = f"csrf:{session_id}".encode()
    return hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_csrf(secret: str, session_id: uuid.UUID, token: str | None) -> bool:
    if not token:
        return False
    expected = csrf_token(secret, session_id)
    return hmac.compare_digest(expected.encode("ascii"), token.encode("utf-8"))
