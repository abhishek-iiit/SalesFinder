"""A fake HubSpot CRM API behind ``httpx.MockTransport``: GET-only, paged, with a call log."""

import json
from typing import Any

import httpx

FAKE_TOKEN = "pat-na1-fake-token-0123456789abcdef"  # noqa: S105
PAGE_SIZE = 2

COMPANY_PROPERTIES = "name,domain,website,hs_additional_domains,lifecyclestage,hubspot_owner_id"
CONTACT_PROPERTIES = "email,firstname,lastname,company,lifecyclestage,hubspot_owner_id"

ENDPOINTS = {
    "/crm/v3/objects/companies": "companies",
    "/crm/v3/objects/contacts": "contacts",
    "/crm/v3/owners": "owners",
}


class FakeHubSpot:
    """Serves companies, contacts and owners in pages of two.

    ``fail={"contacts": 500}`` makes that endpoint answer with that status. By default it fails
    every time; ``fail_times={"contacts": 1}`` makes it fail only that many times, then succeed.
    Any non-GET request, a foreign host, or an unknown path is recorded in ``violations`` and
    raises ``AssertionError`` (call ``assert_read_only()`` at the end of each test).
    """

    def __init__(
        self,
        companies: list[dict[str, Any]],
        contacts: list[dict[str, Any]],
        owners: list[dict[str, Any]],
        fail: dict[str, int] | None = None,
        *,
        fail_times: dict[str, int] | None = None,
        retry_after: str | None = "1",
        token: str = FAKE_TOKEN,
        page_size: int = PAGE_SIZE,
        bodies: dict[str, Any] | None = None,
    ) -> None:
        self.data: dict[str, list[dict[str, Any]]] = {
            "companies": companies,
            "contacts": contacts,
            "owners": owners,
        }
        self.fail = dict(fail or {})
        self.fail_times = dict(fail_times or {})
        self.retry_after = retry_after
        self.token = token
        self.page_size = page_size
        self.bodies = dict(bodies or {})  # endpoint -> raw JSON body served with a 200
        self.calls: list[httpx.Request] = []
        self.violations: list[str] = []
        self.transport = httpx.MockTransport(self._handle)

    def calls_to(self, name: str) -> list[httpx.Request]:
        return [c for c in self.calls if ENDPOINTS.get(c.url.path) == name]

    def assert_read_only(self) -> None:
        assert not self.violations, self.violations
        assert self.calls, "no HubSpot calls were made"
        assert all(c.method == "GET" for c in self.calls)
        assert all(c.url.host == "api.hubapi.com" for c in self.calls)

    def _violation(self, message: str) -> None:
        self.violations.append(message)
        raise AssertionError(message)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.method != "GET":
            self._violation(f"non-GET request to HubSpot: {request.method} {request.url}")
        if request.url.scheme != "https" or request.url.host != "api.hubapi.com":
            self._violation(f"request to a foreign host: {request.url}")
        name = ENDPOINTS.get(request.url.path)
        if name is None:
            self._violation(f"unknown HubSpot path: {request.url.path}")
            raise AssertionError  # unreachable; keeps the type checker happy
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json={"status": "error", "message": "Authentication"})

        status = self.fail.get(name)
        if status is not None:
            remaining = self.fail_times.get(name)
            if remaining is None or remaining > 0:
                if remaining is not None:
                    self.fail_times[name] = remaining - 1
                headers = {}
                if status == 429 and self.retry_after is not None:
                    headers["Retry-After"] = self.retry_after
                return httpx.Response(status, headers=headers, json={"status": "error"})

        if name in self.bodies:
            return httpx.Response(200, json=self.bodies[name])

        params = request.url.params
        if params.get("limit") != "100":
            self._violation(f"expected limit=100, got {params.get('limit')!r}")
        if name != "owners":
            expected = COMPANY_PROPERTIES if name == "companies" else CONTACT_PROPERTIES
            if params.get("properties") != expected or params.get("archived") != "false":
                self._violation(f"unexpected query for {name}: {request.url.query!r}")

        items = self.data[name]
        start = int(params.get("after", "0"))
        end = start + self.page_size
        body: dict[str, Any] = {"results": items[start:end]}
        if end < len(items):
            body["paging"] = {"next": {"after": str(end), "link": "ignored"}}
        return httpx.Response(200, content=json.dumps(body).encode(), headers=_JSON)


_JSON = {"Content-Type": "application/json"}


def company(
    hid: str,
    name: str,
    *,
    domain: str | None = None,
    website: str | None = None,
    additional: str | None = None,
    stage: str | None = "customer",
    owner: str | None = None,
) -> dict[str, Any]:
    return {
        "id": hid,
        "archived": False,
        "properties": {
            "name": name,
            "domain": domain,
            "website": website,
            "hs_additional_domains": additional,
            "lifecyclestage": stage,
            "hubspot_owner_id": owner,
        },
    }


def contact(
    hid: str,
    email: str | None,
    *,
    first: str | None = None,
    last: str | None = None,
    company_name: str | None = None,
    stage: str | None = "customer",
    owner: str | None = None,
) -> dict[str, Any]:
    return {
        "id": hid,
        "archived": False,
        "properties": {
            "email": email,
            "firstname": first,
            "lastname": last,
            "company": company_name,
            "lifecyclestage": stage,
            "hubspot_owner_id": owner,
        },
    }


def owner(oid: str, *, first: str = "", last: str = "", email: str = "") -> dict[str, Any]:
    return {"id": oid, "firstName": first, "lastName": last, "email": email, "archived": False}
