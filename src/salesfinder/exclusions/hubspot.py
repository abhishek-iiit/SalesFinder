"""Read-only HubSpot CRM client: GET companies, contacts and owners, with paging and retries.

The token only ever appears in the ``Authorization`` header. Errors are ``HubSpotError`` with a
fixed, human-readable ``reason``; underlying httpx exceptions are never chained or echoed.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from types import TracebackType
from typing import Any, Self

import httpx

DEFAULT_BASE_URL = "https://api.hubapi.com"
TIMEOUT_SECONDS = 20.0
PAGE_LIMIT = "100"
MAX_RETRY_AFTER_SECONDS = 60.0

COMPANY_PROPERTIES = "name,domain,website,hs_additional_domains,lifecyclestage,hubspot_owner_id"
CONTACT_PROPERTIES = "email,firstname,lastname,company,lifecyclestage,hubspot_owner_id"

TOKEN_REJECTED = "HubSpot rejected the token (401). Check SALESFINDER_HUBSPOT_TOKEN."  # noqa: S105
MISSING_SCOPE = (
    "HubSpot token lacks a required scope (403): "
    "crm.objects.companies.read / crm.objects.contacts.read."
)
UNEXPECTED_RESPONSE = "HubSpot returned an unexpected response."

Sleep = Callable[[float], Awaitable[None]]


class HubSpotError(Exception):
    """A HubSpot failure with a human-readable reason that never contains the token."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    raw = response.headers.get("Retry-After")
    if raw is not None:
        try:
            seconds = float(raw)
        except ValueError:
            seconds = -1.0
        if seconds >= 0:
            return min(seconds, MAX_RETRY_AFTER_SECONDS)
    return float(2**attempt)


class HubSpotClient:
    def __init__(
        self,
        token: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = DEFAULT_BASE_URL,
        max_retries: int = 3,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._max_retries = max_retries
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            base_url=base_url,
            transport=transport,
            timeout=TIMEOUT_SECONDS,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            follow_redirects=False,
        )

    def __repr__(self) -> str:
        return f"HubSpotClient(base_url={str(self._http.base_url)!r})"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _get(
        self, path: str, params: dict[str, str], *, forbidden: str | None
    ) -> dict[str, Any] | None:
        """GET one page. Returns None on 403 when ``forbidden`` is None (optional endpoint)."""
        attempt = 0
        while True:
            try:
                response = await self._http.get(path, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                # A blip (timeout, dropped connection) gets the same retry budget as a 429/5xx.
                if attempt >= self._max_retries:
                    raise HubSpotError(f"Could not reach HubSpot: {type(exc).__name__}.") from None
                await self._sleep(float(2**attempt))
                attempt += 1
                continue
            status = response.status_code
            if status == 429 or status >= 500:
                if attempt >= self._max_retries:
                    raise HubSpotError(
                        f"HubSpot unavailable after {self._max_retries} retries (HTTP {status})."
                    )
                await self._sleep(_retry_delay(response, attempt))
                attempt += 1
                continue
            if status == 401:
                raise HubSpotError(TOKEN_REJECTED)
            if status == 403:
                if forbidden is None:
                    return None
                raise HubSpotError(forbidden)
            if status != 200:
                raise HubSpotError(f"HubSpot request failed (HTTP {status}).")
            try:
                body = response.json()
            except ValueError:
                raise HubSpotError(UNEXPECTED_RESPONSE) from None
            if not isinstance(body, dict) or not isinstance(body.get("results"), list):
                raise HubSpotError(UNEXPECTED_RESPONSE)
            return body

    async def _pages(
        self, path: str, params: dict[str, str], *, forbidden: str | None
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Yield each page's results, following ``paging.next.after``."""
        after: str | None = None
        seen: set[str] = set()
        while True:
            query = dict(params)
            if after is not None:
                query["after"] = after
            body = await self._get(path, query, forbidden=forbidden)
            if body is None:
                return
            yield [item for item in body["results"] if isinstance(item, dict)]
            paging = body.get("paging")
            nxt = paging.get("next") if isinstance(paging, dict) else None
            cursor = nxt.get("after") if isinstance(nxt, dict) else None
            if cursor is None or cursor == "":
                return
            after = str(cursor)
            if after in seen:  # a cursor that loops would page forever
                raise HubSpotError(UNEXPECTED_RESPONSE)
            seen.add(after)

    async def _objects(self, kind: str, properties: str) -> AsyncIterator[dict[str, Any]]:
        params = {"limit": PAGE_LIMIT, "properties": properties, "archived": "false"}
        async for page in self._pages(f"/crm/v3/objects/{kind}", params, forbidden=MISSING_SCOPE):
            for item in page:
                yield item

    def iter_companies(self) -> AsyncIterator[dict[str, Any]]:
        return self._objects("companies", COMPANY_PROPERTIES)

    def iter_contacts(self) -> AsyncIterator[dict[str, Any]]:
        return self._objects("contacts", CONTACT_PROPERTIES)

    async def owners(self) -> dict[str, str]:
        """``{owner id: "First Last" or email}``; ``{}`` when the token lacks the owners scope."""
        names: dict[str, str] = {}
        async for page in self._pages("/crm/v3/owners", {"limit": PAGE_LIMIT}, forbidden=None):
            for item in page:
                oid = item.get("id")
                if oid is None:
                    continue
                first = str(item.get("firstName") or "").strip()
                last = str(item.get("lastName") or "").strip()
                name = " ".join(p for p in (first, last) if p) or str(item.get("email") or "")
                if name.strip():
                    names[str(oid)] = name.strip()
        return names
