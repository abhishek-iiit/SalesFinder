"""A fake MillionVerifier API behind ``httpx.MockTransport``. No network."""

from typing import Any

import httpx

FAKE_KEY = "mv-fake-key-0123456789abcdef"


class FakeMillionVerifier:
    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        *,
        status: int = 200,
        body: bytes | None = None,
        raises: Exception | None = None,
    ) -> None:
        self.payload = payload if payload is not None else {"result": "ok"}
        self.status = status
        self.body = body
        self.raises = raises
        self.requests: list[httpx.Request] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        if self.body is not None:
            return httpx.Response(self.status, content=self.body)
        return httpx.Response(self.status, json=self.payload)
