"""Score a company from the latest value of each confirmed signal. Pure.

Values come from stored observations (JSONB), so every reader is defensive: a value of an
unexpected shape simply earns nothing.
"""

import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from salesfinder.config import ScoringConfig
from salesfinder.signals.catalog import SIGNALS

PRODUCER_TYPES = frozenset({"manufacturer", "contract_manufacturer", "private_label"})
DISQUALIFYING_TYPES = frozenset({"retailer", "service"})
UNKNOWN_ENUMS = frozenset({"unknown", "unclear"})
_LIST_SHOWN = 4


@dataclass(frozen=True)
class ScoreResult:
    score: int  # 0-100
    disqualified: bool
    reasons: list[str]


def is_present(code: str, value: Any) -> bool:
    """Whether an observed value is a positive finding (not an explicit negative)."""
    if isinstance(value, list):
        return bool(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        if value.get("found") is False:
            return False
        if code == "HIRING_PROCUREMENT":
            return value.get("hiring") is True
        return bool(value)
    if isinstance(value, str):
        return bool(value) and value not in UNKNOWN_ENUMS
    return False


def _words(value: str) -> str:
    return value.replace("_", " ")


def _short_list(values: list[Any]) -> str:
    shown = ", ".join(str(v) for v in values[:_LIST_SHOWN])
    more = len(values) - _LIST_SHOWN
    return f"{shown} +{more} more" if more > 0 else shown


def _str_list(value: Any) -> list[Any] | None:
    return value if isinstance(value, list) and value else None


def _finite(value: Any) -> float | None:
    """A finite number (stored JSON: an int too big for a float is not one)."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _miles(value: Any) -> float | None:
    if not isinstance(value, dict):
        return None
    return _finite(value.get("miles"))


def _buyer_points(code: str, value: Any, config: ScoringConfig) -> tuple[str, int] | None:
    """(short value, points) earned by one buyer signal, or None."""
    weights = config.buyer
    match code:
        case "USES_CATALOG_CHEMICAL" | "INDUSTRY_MATCH" | "QUALITY_CERTS":
            items = _str_list(value)
            return (_short_list(items), weights[code]) if items else None
        case "COMPANY_TYPE":
            if not isinstance(value, str):
                return None
            if value in PRODUCER_TYPES:
                return _words(value), weights["COMPANY_TYPE_PRODUCER"]
            if value == "distributor":
                return value, weights["COMPANY_TYPE_DISTRIBUTOR"]
            return None
        case "US_LOCATION":
            if not isinstance(value, dict) or not isinstance(value.get("state"), str):
                return None
            city = value.get("city")
            where = (
                f"{city}, {value['state']}" if isinstance(city, str) and city else value["state"]
            )
            return where, weights[code]
        case "NEAR_WAREHOUSE":
            miles = _miles(value)
            if miles is None or miles > config.near_warehouse_miles:
                return None
            warehouse = value.get("warehouse")
            near = f" from {warehouse}" if isinstance(warehouse, str) and warehouse else ""
            return f"{round(miles)} miles{near}", weights[code]
        case "HIRING_PROCUREMENT":
            if not isinstance(value, dict) or value.get("hiring") is not True:
                return None
            roles = _str_list(value.get("roles"))
            return (_short_list(roles) if roles else "yes"), weights[code]
    return None


def _supplier_points(code: str, value: Any, config: ScoringConfig) -> tuple[str, int] | None:
    """(short value, points) earned by one supplier signal, or None."""
    weights = config.supplier
    match code:
        case "MAKES_CATALOG_CHEMICAL" | "SUPPLIER_CERTS":
            items = _str_list(value)
            return (_short_list(items), weights[code]) if items else None
        case "MANUFACTURER_NOT_TRADER":
            return ("manufacturer", weights["MANUFACTURER"]) if value == "manufacturer" else None
        case "EXPORTS" | "EXPORTS_TO_US":
            return ("yes", weights[code]) if value is True else None
        case "STATED_CAPACITY":
            if not isinstance(value, dict) or value.get("found") is False:
                return None
            text = value.get("text")
            if isinstance(text, str) and text:
                return text[:60], weights[code]
            per_year = _finite(value.get("mt_per_year"))
            if per_year is not None:
                return f"{round(per_year):,} MT/year", weights[code]
            return None
    return None


def score_company(
    latest: Mapping[str, Any], *, side: str, confirmed: frozenset[str], config: ScoringConfig
) -> ScoreResult:
    """Score from ``latest`` (code -> latest observed value). Only ``confirmed`` codes of
    ``side`` count; a buyer that looks like a retailer or service business is disqualified."""
    codes = [s for s in SIGNALS if s.side == side and s.code in confirmed and s.code in latest]
    if side == "buyer":
        company_type = latest.get("COMPANY_TYPE")
        if (
            "COMPANY_TYPE" in confirmed
            and isinstance(company_type, str)
            and company_type in DISQUALIFYING_TYPES
        ):
            return ScoreResult(0, True, [f"Disqualified: {_words(company_type)}"])
    points_for = _buyer_points if side == "buyer" else _supplier_points
    total = 0
    reasons: list[str] = []
    for signal in codes:
        earned = points_for(signal.code, latest[signal.code], config)
        if earned is None or earned[1] <= 0:
            continue
        short, points = earned
        total += points
        reasons.append(f"{signal.label}: {short} (+{points})")
    return ScoreResult(max(0, min(100, total)), False, reasons)
