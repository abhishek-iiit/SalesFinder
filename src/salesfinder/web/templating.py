"""The Jinja environment (autoescaping), its filters, and ``render``."""

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request
from fastapi.templating import Jinja2Templates
from starlette.responses import HTMLResponse

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=TEMPLATES_DIR)


def usd(value: Decimal | int | None, places: int = 2, *, down: bool = False) -> str:
    """``$1,234.56`` (or ``places`` decimals). ``down`` truncates instead of rounding half even,
    for amounts that must never be overstated (what's left of a budget)."""
    amount = Decimal(value or 0)
    if down:
        amount = amount.quantize(Decimal(1).scaleb(-places), rounding=ROUND_DOWN)
    return f"${amount:,.{places}f}"


def utc(value: datetime | None) -> str:
    if value is None:
        return "—"
    at = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return at.strftime("%Y-%m-%d %H:%M UTC")


def web_url(value: str | None) -> str | None:
    """``value`` if it is an http(s) URL, else None: stored URLs come from search results, and a
    ``javascript:`` link must never be rendered."""
    if not value:
        return None
    try:
        scheme = urlsplit(value).scheme.lower()
    except ValueError:
        return None
    return value if scheme in ("http", "https") else None


templates.env.filters["usd"] = usd
templates.env.filters["utc"] = utc
templates.env.filters["web_url"] = web_url


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request, name, context or {}, status_code=status_code, headers=headers
    )
