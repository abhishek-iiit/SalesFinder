"""The fixed catalog of signal codes the profiler can look for."""

from dataclasses import dataclass
from typing import Literal

Side = Literal["buyer", "supplier"]
ValueKind = Literal["list", "enum", "bool", "object", "text"]


@dataclass(frozen=True)
class SignalDef:
    code: str
    side: Side
    label: str
    description: str
    value_kind: ValueKind
    cost: str = "free"


SIGNALS: tuple[SignalDef, ...] = (
    # Buyer signals.
    SignalDef(
        "INDUSTRY_MATCH",
        "buyer",
        "Works in our industries",
        "Their website describes products in one of Elchemy's eight industries.",
        "list",
    ),
    SignalDef(
        "USES_CATALOG_CHEMICAL",
        "buyer",
        "Mentions chemicals we sell",
        "Their website names one or more chemicals from Elchemy's product catalog.",
        "list",
    ),
    SignalDef(
        "COMPANY_TYPE",
        "buyer",
        "What kind of company they are",
        "Whether their website suggests a manufacturer, contract or private-label maker,"
        " distributor, or something else.",
        "enum",
    ),
    SignalDef(
        "US_LOCATION",
        "buyer",
        "Based in the US",
        "Their website shows a US address, and which state it is in.",
        "object",
    ),
    SignalDef(
        "NEAR_WAREHOUSE",
        "buyer",
        "Close to an Elchemy warehouse",
        "Their US location is within the configured distance of one of Elchemy's warehouses.",
        "object",
    ),
    SignalDef(
        "HIRING_PROCUREMENT",
        "buyer",
        "Hiring in purchasing",
        "Their website has a job posting for a buyer, procurement or supply-chain role.",
        "object",
    ),
    SignalDef(
        "QUALITY_CERTS",
        "buyer",
        "Quality certifications",
        "Their website mentions quality certifications such as ISO 9001, GMP or Kosher.",
        "list",
    ),
    # Supplier signals.
    SignalDef(
        "MAKES_CATALOG_CHEMICAL",
        "supplier",
        "Makes chemicals we sell",
        "Their website says they make one or more chemicals from Elchemy's product catalog.",
        "list",
    ),
    SignalDef(
        "MANUFACTURER_NOT_TRADER",
        "supplier",
        "Makes it themselves",
        "Whether their website suggests a real manufacturer rather than a trader or reseller.",
        "enum",
    ),
    SignalDef(
        "EXPORTS",
        "supplier",
        "Exports",
        "Their website says they ship products to customers in other countries.",
        "bool",
    ),
    SignalDef(
        "EXPORTS_TO_US",
        "supplier",
        "Exports to the US",
        "Their website says they already sell to customers in the United States.",
        "bool",
    ),
    SignalDef(
        "SUPPLIER_CERTS",
        "supplier",
        "Quality certifications",
        "Their website mentions certifications such as ISO 9001, GMP, Kosher or Halal.",
        "list",
    ),
    SignalDef(
        "STATED_CAPACITY",
        "supplier",
        "States production capacity",
        "Their website gives a production capacity figure, such as tons per month.",
        "object",
    ),
    SignalDef(
        "REGION",
        "supplier",
        "Where they operate",
        "The country or region their website says they are based in.",
        "text",
    ),
)

_BY_CODE = {s.code: s for s in SIGNALS}


def by_code(code: str) -> SignalDef:
    return _BY_CODE[code]


def for_side(side: Side) -> tuple[SignalDef, ...]:
    return tuple(s for s in SIGNALS if s.side == side)
