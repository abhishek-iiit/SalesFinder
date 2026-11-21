"""What the writer may tell Claude about one company: only confirmed Elchemy facts and the
safe facts about the recipient (spec: truthfulness). Never evidence snippets or page text.

``recipient`` values (company name, the person's name and title) are scraped and untrusted: the
prompt puts them in a delimited data block, JSON-escaped.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from salesfinder.catalog import Catalog, Facts, Side
from salesfinder.config import AppConfig
from salesfinder.db.models import Campaign, Company, OutboxFeedback, Person
from salesfinder.signals.catalog import SIGNALS
from salesfinder.signals.format import NOT_FOUND, format_value
from salesfinder.signals.latest import latest_values
from salesfinder.signals.settings import confirmed_codes

# The signals whose readable values the writer may use, with the label the prompt shows. The
# chemical signals become ``products`` instead (catalog names only). Left out on purpose: hiring
# roles and stated capacity (page text), company type, exports and manufacturer-or-trader (not
# on the allowed list), and certifications: the gate holds any certification an email names
# that isn't in Elchemy's confirmed facts, the recipient's own included.
WRITER_SIGNALS: dict[str, str] = {
    "INDUSTRY_MATCH": "industries",
    "US_LOCATION": "location",
    "NEAR_WAREHOUSE": "nearest Elchemy warehouse",
    "REGION": "region",
}
_PRODUCT_CODES = ("USES_CATALOG_CHEMICAL", "MAKES_CATALOG_CHEMICAL")


@dataclass(frozen=True)
class WriterInput:
    side: Side
    # company_name, website, claims (confirmed texts), warehouses (labels), persona_name,
    # persona_title.
    elchemy: dict[str, Any]
    # first_name, full_name, title, company_name: scraped, untrusted.
    recipient: dict[str, str]
    # Signal code -> readable value, for confirmed signals with a value found (WRITER_SIGNALS).
    signals: dict[str, str]
    # Catalog product names the company uses or makes (confirmed chemical signals).
    products: list[str]
    # Teammates' rejection reasons for this campaign, newest first.
    feedback: list[str]
    booking_url: str
    # The gate's limits, so the writer aims inside them: first_email_max_words,
    # followup_max_words, subject_chars.
    limits: dict[str, int]
    banned_phrases: list[str]


def _persona_problem(side: Side) -> ValueError:
    return ValueError(f"Persona for {side} isn't confirmed in facts.yaml.")


def _products(values: dict[str, Any], catalog: Catalog) -> list[str]:
    names: list[str] = []
    for code in _PRODUCT_CODES:
        value = values.get(code)
        if not isinstance(value, list):
            continue
        for product_id in value:
            try:
                name = catalog.by_id(str(product_id)).name
            except KeyError:
                continue
            if name not in names:
                names.append(name)
    return names


async def _feedback(db: AsyncSession, campaign_id: uuid.UUID, limit: int) -> list[str]:
    rows = await db.scalars(
        select(OutboxFeedback.reason)
        .where(OutboxFeedback.campaign_id == campaign_id)
        .order_by(OutboxFeedback.created_at.desc(), OutboxFeedback.id.desc())
        .limit(limit)
    )
    return list(rows)


async def build_context(
    db: AsyncSession,
    *,
    campaign: Campaign,
    company: Company,
    person: Person | None,
    catalog: Catalog,
    facts: Facts,
    config: AppConfig,
) -> WriterInput:
    """The writer's input for ``company`` in ``campaign``. Raises ``ValueError`` if the side's
    persona isn't confirmed (with a name and title: the signature needs both) or the company is
    on the other side."""
    side: Side
    if campaign.side == "buyer":
        side = "buyer"
    elif campaign.side == "supplier":
        side = "supplier"
    else:
        raise ValueError(f"Unknown campaign side {campaign.side!r}.")
    if company.side != side:
        raise ValueError("Company is on the other side from the campaign.")
    persona = facts.personas.get(side)
    if persona is None or not persona.confirmed:
        raise _persona_problem(side)
    if not persona.name.strip() or not persona.title.strip():
        raise _persona_problem(side)

    codes = (await confirmed_codes(db, side)) & {*WRITER_SIGNALS, *_PRODUCT_CODES}
    values = (await latest_values(db, codes, [company.id])).get(company.id, {})
    signals: dict[str, str] = {}
    for definition in SIGNALS:  # catalog order: stable prompts
        if definition.code in values and definition.code in WRITER_SIGNALS:
            text = format_value(definition.code, values[definition.code], catalog).strip()
            if text and text != NOT_FOUND:
                signals[definition.code] = text

    writing = config.writing
    return WriterInput(
        side=side,
        elchemy={
            "company_name": facts.company_name,
            "website": facts.website,
            "claims": [c.text for c in facts.claims if c.confirmed],
            "warehouses": [w.label for w in facts.warehouses],
            "persona_name": persona.name.strip(),
            "persona_title": persona.title.strip(),
        },
        recipient={
            "first_name": (person.first_name or "") if person else "",
            "full_name": person.full_name if person else "",
            "title": (person.title or "") if person else "",
            "company_name": company.name,
        },
        signals=signals,
        products=_products(values, catalog),
        feedback=await _feedback(db, campaign.id, writing.max_feedback_items),
        booking_url=writing.booking_url,
        limits={
            "first_email_max_words": writing.first_email_max_words,
            "followup_max_words": writing.followup_max_words,
            "subject_chars": writing.subject_max_chars,
        },
        banned_phrases=list(writing.banned_phrases),
    )
