"""Observed signal values as readable text, for web pages and the email writer. Pure; values
are stored JSON, so every branch is defensive about shape."""

import math
from typing import Any

from salesfinder.catalog import Catalog
from salesfinder.industries import INDUSTRIES
from salesfinder.profiling.scoring import UNKNOWN_ENUMS

NOT_FOUND = "Not found"
_PRODUCT_CODES = frozenset({"USES_CATALOG_CHEMICAL", "MAKES_CATALOG_CHEMICAL"})


def _product_name(catalog: Catalog, product_id: str) -> str:
    try:
        return catalog.by_id(product_id).name
    except KeyError:
        return product_id


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _number(value: Any) -> float | None:
    """A finite number, or None (a stored int too big for a float is not a number we show)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _list(code: str, value: list[Any], catalog: Catalog) -> str:
    if not value:
        return NOT_FOUND
    items = [str(v) for v in value]
    if code in _PRODUCT_CODES:
        items = [_product_name(catalog, i) for i in items]
    elif code == "INDUSTRY_MATCH":
        items = [INDUSTRIES.get(i, i) for i in items]
    return ", ".join(items)


def _hiring(value: dict[str, Any]) -> str:
    if value.get("hiring") is True:
        roles = value.get("roles")
        names = [str(r) for r in roles] if isinstance(roles, list) else []
        return f"Yes — {', '.join(names)}" if names else "Yes"
    if value.get("careers_page") is False:
        return "No careers page found"
    return "No"


def _object(code: str, value: dict[str, Any]) -> str:
    if value.get("found") is False:
        return NOT_FOUND
    match code:
        case "US_LOCATION":
            state = _text(value.get("state"))
            city = _text(value.get("city"))
            if state is None:
                return NOT_FOUND
            return f"{city}, {state}" if city else state
        case "NEAR_WAREHOUSE":
            miles = _number(value.get("miles"))
            if miles is None:
                return NOT_FOUND
            warehouse = _text(value.get("warehouse"))
            near = f" from {warehouse}" if warehouse else ""
            return f"{round(miles):,} miles{near}"
        case "HIRING_PROCUREMENT":
            return _hiring(value)
        case "STATED_CAPACITY":
            per_year = _number(value.get("mt_per_year"))
            text = _text(value.get("text"))
            if per_year is not None:
                stated = f" ({text})" if text else ""
                return f"{round(per_year):,} MT/year{stated}"
            return text or NOT_FOUND
    return NOT_FOUND


def format_value(code: str, value: Any, catalog: Catalog) -> str:
    """The value of signal ``code`` as one line of plain text (templates escape it)."""
    if isinstance(value, bool):  # before int: bool is an int
        return "Yes" if value else "No"
    if isinstance(value, list):
        return _list(code, value, catalog)
    if isinstance(value, dict):
        return _object(code, value)
    if isinstance(value, str):
        if not value or value in UNKNOWN_ENUMS:
            return NOT_FOUND
        return (
            value.replace("_", " ")
            if code in ("COMPANY_TYPE", "MANUFACTURER_NOT_TRADER")
            else value
        )
    return NOT_FOUND
