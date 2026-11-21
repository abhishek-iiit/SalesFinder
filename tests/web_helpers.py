"""Helpers shared by the web tests (not collected: no ``test_`` prefix)."""

import html
import re

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.db.models import AuditLog

_CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def text_of(resp: httpx.Response) -> str:
    """Response body with HTML entities decoded (Jinja escapes apostrophes as ``&#39;``)."""
    return html.unescape(resp.text)


def csrf_from(page: str) -> str:
    match = _CSRF_RE.search(page)
    assert match, "no csrf_token field in page"
    return match.group(1)


async def login(
    client: httpx.AsyncClient, email: str, password: str, *, next_path: str | None = None
) -> httpx.Response:
    page = await client.get("/login")
    assert page.status_code == 200
    data = {"email": email, "password": password}
    if next_path is not None:
        data["next"] = next_path
    return await client.post("/login", data=data)


async def csrf_for(client: httpx.AsyncClient, path: str = "/") -> str:
    page = await client.get(path)
    assert page.status_code == 200, page.status_code
    return csrf_from(page.text)


async def audit_actions(db: AsyncSession) -> list[str]:
    db.expire_all()
    return list(await db.scalars(select(AuditLog.action).order_by(AuditLog.at)))
