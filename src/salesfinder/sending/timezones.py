"""Where a recipient is, so mail goes out in their office hours."""

import re

from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.db.models import Company
from salesfinder.signals.detect_common import _STATE_NAMES as _NAME_TO_CODE
from salesfinder.signals.latest import latest_values

_ET, _CT, _MT, _PT = "America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"

STATE_ZONES: dict[str, str] = {
    **dict.fromkeys("CT DE DC FL GA ME MD MA NH NJ NY NC OH PA RI SC VT VA WV".split(), _ET),
    **dict.fromkeys("AL AR IL IA KS LA MN MS MO NE ND OK SD TX WI".split(), _CT),
    **dict.fromkeys("CO MT NM UT WY".split(), _MT),
    **dict.fromkeys("CA NV OR WA".split(), _PT),
    "MI": _ET,
    "KY": _ET,
    "TN": _CT,
    "IN": "America/Indiana/Indianapolis",
    "AZ": "America/Phoenix",
    "HI": "Pacific/Honolulu",
    "AK": "America/Anchorage",
    "ID": "America/Boise",
}

COUNTRY_ZONES: dict[str, str] = {
    "IN": "Asia/Kolkata",
    "TH": "Asia/Bangkok",
    "MY": "Asia/Kuala_Lumpur",
    "ID": "Asia/Jakarta",
    "VN": "Asia/Ho_Chi_Minh",
    "SG": "Asia/Singapore",
    "PH": "Asia/Manila",
    "BD": "Asia/Dhaka",
    "LK": "Asia/Colombo",
    "CN": "Asia/Shanghai",
}

UNKNOWN_BUYER = ("America/New_York", "America/Los_Angeles")
UNKNOWN_SUPPLIER = ("Asia/Kolkata", "Asia/Jakarta")

_NAMES_LONGEST_FIRST = sorted(_NAME_TO_CODE, key=lambda n: -len(n))


def _state_code(raw: str) -> str | None:
    """A state code or name in ``raw``. An upper-case, word-bounded two-letter code wins
    ("Washington, DC" is DC); else names match case-insensitively on word boundaries, longest
    first ("West Virginia" is WV). Lower-case "in" or "or" in prose never match."""
    text = raw.strip()
    if text.upper() in STATE_ZONES and (len(text) != 2 or text.isupper()):
        return text.upper()
    for code in re.findall(r"(?<![A-Za-z])[A-Z]{2}(?![A-Za-z])", text):
        if str(code) in STATE_ZONES:
            return str(code)
    for name in _NAMES_LONGEST_FIRST:
        if re.search(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])", text, re.IGNORECASE):
            return _NAME_TO_CODE[name]
    return None


async def recipient_zones(db: AsyncSession, company: Company) -> tuple[str, ...]:
    if company.side == "supplier":
        zone = COUNTRY_ZONES.get((company.country or "").strip().upper())
        return (zone,) if zone else UNKNOWN_SUPPLIER
    value = (await latest_values(db, ["US_LOCATION"], [company.id])).get(company.id, {})
    location = value.get("US_LOCATION")
    state = location.get("state") if isinstance(location, dict) else None
    candidates = [state, company.region]
    for candidate in candidates:
        if isinstance(candidate, str) and (code := _state_code(candidate)):
            return (STATE_ZONES[code],)
    return UNKNOWN_BUYER
