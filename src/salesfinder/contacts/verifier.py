"""Optional paid address verifier (MillionVerifier) and the daily cap helper.

The API key only ever appears in the request query string. A call that gets no answer about
the address (an HTTP error, a non-200 status, an ``error`` payload such as a bad key or no
credits left, or a payload without a ``result``) is a failure: ``verify`` returns None and the
client counts it. httpx exceptions are caught without logging their text (which would contain the
URL and key).
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

import httpx
import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.db.models import EmailAddress

VerifyResult = Literal["verified", "risky", "invalid", "unknown"]

API_URL = "https://api.millionverifier.com/api/v3/"
RESULT_MAP: dict[str, VerifyResult] = {
    "ok": "verified",
    "catch_all": "risky",
    "invalid": "invalid",
    "disposable": "invalid",
}

log = structlog.get_logger()


class _DropVerifierRequests(logging.Filter):
    """httpx logs every request URL at INFO, and ours carries the API key in its query string."""

    def filter(self, record: logging.LogRecord) -> bool:
        return "millionverifier.com" not in record.getMessage()


logging.getLogger("httpx").addFilter(_DropVerifierRequests())


class Verifier(Protocol):
    @property
    def consecutive_failures(self) -> int:
        """Failed calls since the last answered one."""
        ...

    async def verify(self, address: str) -> VerifyResult | None:
        """The verifier's answer, or None when the call failed (nothing learned, nothing to
        record, no daily-cap use)."""
        ...


class NoVerifier:
    failures = 0
    consecutive_failures = 0

    async def verify(self, address: str) -> VerifyResult | None:
        return "unknown"


class MillionVerifier:
    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20,
    ) -> None:
        self._key = api_key
        self._http = httpx.AsyncClient(transport=transport, timeout=timeout, follow_redirects=False)
        self.failures = 0  # calls that failed, over this client's lifetime
        self.consecutive_failures = 0

    def __repr__(self) -> str:
        return "MillionVerifier()"

    async def aclose(self) -> None:
        await self._http.aclose()

    def _failed(self) -> None:
        self.failures += 1
        self.consecutive_failures += 1

    async def verify(self, address: str) -> VerifyResult | None:
        try:
            response = await self._http.get(
                API_URL, params={"api": self._key, "email": address, "timeout": "10"}
            )
        except httpx.HTTPError as exc:
            log.warning("verifier_request_failed", error_type=type(exc).__name__)
            self._failed()
            return None
        if response.status_code != 200:
            log.warning("verifier_bad_status", status=response.status_code)
            self._failed()
            return None
        try:
            payload: Any = response.json()
        except ValueError:
            payload = None
        result = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(result, str) or payload.get("error") or result == "error":
            # A bad key or no credits left come back as 200 with an "error" field. Never log
            # the payload: it is the provider's text.
            log.warning("verifier_error_payload")
            self._failed()
            return None
        self.consecutive_failures = 0
        return RESULT_MAP.get(result, "unknown")


async def daily_count(db: AsyncSession, *, now: datetime) -> int:
    """Addresses sent to the verifier during the current UTC day."""
    start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    count = await db.scalar(
        select(func.count())
        .select_from(EmailAddress)
        .where(
            EmailAddress.verified_at >= start, EmailAddress.verified_at < start + timedelta(days=1)
        )
    )
    return int(count or 0)


async def under_cap(db: AsyncSession, *, now: datetime, max_per_day: int) -> bool:
    return await daily_count(db, now=now) < max_per_day
