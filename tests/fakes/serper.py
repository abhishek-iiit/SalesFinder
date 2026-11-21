"""A fake Serper search API behind ``httpx.MockTransport``: strict, with a call log."""

import json
from collections.abc import Callable
from typing import Any

import httpx

FAKE_KEY = "serper-fake-key-0123456789abcdef"

Payload = dict[str, Any]
Responder = Callable[[Payload], list[dict[str, Any]] | httpx.Response]


class FakeSerper:
    """Answers ``POST https://google.serper.dev/search``.

    ``responses`` maps a query string to its ``organic`` results (an unknown query is a
    violation), or is a callable taking the request payload and returning either the organic
    list or a complete ``httpx.Response`` (for malformed bodies). ``status_by_call`` is consumed
    one entry per call: a non-200 entry is answered with that status instead; once the list is
    exhausted every call is answered normally. A wrong/missing ``X-API-KEY`` gets a 401.
    """

    def __init__(
        self,
        responses: dict[str, list[dict[str, Any]]] | Responder,
        status_by_call: list[int] | None = None,
        *,
        key: str = FAKE_KEY,
        retry_after: str | None = None,
    ) -> None:
        self.responses = responses
        self.status_by_call = list(status_by_call or [])
        self.key = key
        self.retry_after = retry_after
        self.calls: list[httpx.Request] = []
        self.violations: list[str] = []
        self.transport = httpx.MockTransport(self._handle)

    @property
    def payloads(self) -> list[Payload]:
        return [json.loads(c.content) for c in self.calls]

    def _violation(self, message: str) -> None:
        self.violations.append(message)
        raise AssertionError(message)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.method != "POST":
            self._violation(f"non-POST request to Serper: {request.method} {request.url}")
        if str(request.url) != "https://google.serper.dev/search":
            self._violation(f"unexpected Serper URL: {request.url}")
        if request.headers.get("content-type") != "application/json":
            self._violation("Serper request body is not JSON")
        payload = json.loads(request.content)
        if set(payload) != {"q", "gl", "num", "page"} or payload["num"] != 10:
            self._violation(f"unexpected Serper payload: {payload!r}")
        if request.headers.get("x-api-key") != self.key:
            return httpx.Response(401, json={"message": "Unauthorized.", "statusCode": 401})

        if self.status_by_call:
            status = self.status_by_call.pop(0)
            if status != 200:
                headers = {"Retry-After": self.retry_after} if self.retry_after else {}
                return httpx.Response(status, headers=headers, json={"message": "error"})

        if callable(self.responses):
            answer = self.responses(payload)
            if isinstance(answer, httpx.Response):
                return answer
            items = answer
        else:
            if payload["q"] not in self.responses:
                self._violation(f"unexpected Serper query: {payload['q']!r}")
            items = self.responses[payload["q"]]
        body = {"searchParameters": payload, "organic": items}
        return httpx.Response(
            200, content=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
        )


def organic(url: str, title: str = "", snippet: str = "", position: int = 1) -> dict[str, Any]:
    return {"title": title, "link": url, "snippet": snippet, "position": position}
