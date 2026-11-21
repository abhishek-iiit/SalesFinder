"""Serper (Google search API) client: one POST per search, with retries.

The API key only ever appears in the ``X-API-KEY`` header. Errors are ``SerperError`` with a
fixed, human-readable ``reason``; underlying httpx exceptions are never chained or echoed.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

import httpx

SEARCH_URL = "https://google.serper.dev/search"
TIMEOUT_SECONDS = 20.0
RESULTS_PER_PAGE = 10
MAX_RETRY_AFTER_SECONDS = 60.0

KEY_REJECTED = "Serper rejected the API key."
UNEXPECTED_RESPONSE = "Serper returned an unexpected response."

Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str
    position: int


class SerperError(Exception):
    """A Serper failure with a human-readable reason that never contains the API key.

    ``retryable`` is True when the cause was transient (HTTP 429/5xx) and the client gave up
    after its own retries: a later attempt may succeed. Callers fail the job either way.
    """

    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.retryable = retryable


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    raw = response.headers.get("Retry-After") if response is not None else None
    if raw is not None:
        try:
            seconds = float(raw)
        except ValueError:
            seconds = -1.0
        if 0 <= seconds < float("inf"):
            return min(seconds, MAX_RETRY_AFTER_SECONDS)
    return float(2**attempt)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _parse(body: Any) -> list[SearchResult]:
    if not isinstance(body, dict):
        raise SerperError(UNEXPECTED_RESPONSE)
    organic = body.get("organic", [])
    if not isinstance(organic, list):
        raise SerperError(UNEXPECTED_RESPONSE)
    results: list[SearchResult] = []
    for index, item in enumerate(organic):
        if not isinstance(item, dict):
            continue
        url = _text(item.get("link"))
        if not url:
            continue
        position = item.get("position")
        if not isinstance(position, int) or isinstance(position, bool):
            position = index + 1
        results.append(
            SearchResult(
                title=_text(item.get("title")),
                url=url,
                snippet=_text(item.get("snippet")),
                position=position,
            )
        )
    return results


class SerperClient:
    def __init__(
        self,
        api_key: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        max_retries: int = 3,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._max_retries = max_retries
        self._sleep = sleep
        self._http = httpx.AsyncClient(
            transport=transport,
            timeout=TIMEOUT_SECONDS,
            headers={"X-API-KEY": api_key, "Accept": "application/json"},
            follow_redirects=False,
        )

    def __repr__(self) -> str:
        return f"SerperClient(url={SEARCH_URL!r})"

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

    async def search(self, q: str, *, gl: str, page: int) -> list[SearchResult]:
        """One page of organic results. Retries 429/5xx and transport errors with backoff."""
        payload = {"q": q, "gl": gl, "num": RESULTS_PER_PAGE, "page": page}
        attempt = 0
        while True:
            response: httpx.Response | None
            try:
                response = await self._http.post(SEARCH_URL, json=payload)
            except httpx.TransportError as exc:  # timeouts, refused/dropped connections
                if attempt >= self._max_retries:
                    raise SerperError(f"Could not reach Serper: {type(exc).__name__}.") from None
                response = None
            else:
                status = response.status_code
                if status == 429 or status >= 500:
                    if attempt >= self._max_retries:
                        reason = f"Serper unavailable after {self._max_retries} retries"
                        raise SerperError(f"{reason} (HTTP {status}).", retryable=True)
                else:
                    return self._handle(response)
            await self._sleep(_retry_delay(response, attempt))
            attempt += 1

    @staticmethod
    def _handle(response: httpx.Response) -> list[SearchResult]:
        status = response.status_code
        if status in (401, 403):
            raise SerperError(KEY_REJECTED)
        if status != 200:
            raise SerperError(f"Serper request failed (HTTP {status}).")
        try:
            body = response.json()
        except ValueError:
            raise SerperError(UNEXPECTED_RESPONSE) from None
        return _parse(body)
