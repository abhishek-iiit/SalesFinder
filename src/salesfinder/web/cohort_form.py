"""The cohort form: its choices, the one parser from submitted fields to a ``CohortDefinition``,
the form state used to (re-)render it, and the plain-English summary. Pure.

Field names, per signal ``CODE`` of the form's side:

- ``mode.CODE``: ``ignore`` (default) / ``must`` / ``must_not``;
- ``values.CODE`` (repeated): ticked choices for list and enum signals (none ticked = any);
- ``number.CODE``: miles for NEAR_WAREHOUSE (within), MT/year for STATED_CAPACITY (at least);

plus ``min_score`` (blank or 0-100), ``include_disqualified``, ``include_unprofiled`` and
``require_sendable_contact``. Any other field is ignored.
"""

import math
import re
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Literal, cast

from pydantic import ValidationError
from starlette.datastructures import FormData

from salesfinder.catalog import Catalog
from salesfinder.cohorts.evaluate import condition_holds
from salesfinder.cohorts.model import CohortDefinition, Condition
from salesfinder.industries import INDUSTRIES, SIDES
from salesfinder.signals.catalog import Side, SignalDef, by_code, for_side
from salesfinder.signals.detect_buyer import COMPANY_TYPES
from salesfinder.signals.detect_common import CERT_NAMES
from salesfinder.signals.detect_supplier import PRODUCER_KINDS

MODES = ("ignore", "must", "must_not")
MODE_LABELS = {"ignore": "Ignore", "must": "Must match", "must_not": "Must not match"}

# Lower-case nouns, so they read inside a sentence ("not a distributor").
COMPANY_TYPE_LABELS = {
    "manufacturer": "manufacturer",
    "contract_manufacturer": "contract manufacturer",
    "private_label": "private-label maker",
    "distributor": "distributor",
    "retailer": "retailer",
    "service": "service company",
}
PRODUCER_LABELS = {kind: kind for kind in PRODUCER_KINDS}

SENDABLE_LABEL = "Only companies with a sendable contact"
UNPROFILED_LABEL = "Include companies we couldn't read yet"
MIN_SCORE_ERROR = "Minimum score must be a whole number from 0 to 100."
BAD_FORM = "That form isn't valid. Reload the page and try again."
_FLAG_ON = frozenset({"1", "on", "true"})
_MAX_ECHO = 300  # longest raw text echoed back into a re-rendered form


@dataclass(frozen=True)
class NumberInput:
    field: str
    op: Literal["lte", "gte"]
    label: str
    unit: str
    maximum: int


NUMBER_INPUTS = {
    "NEAR_WAREHOUSE": NumberInput("miles", "lte", "Within (miles)", "miles", 25_000),
    "STATED_CAPACITY": NumberInput(
        "mt_per_year", "gte", "At least (MT per year)", "MT/year", 1_000_000_000
    ),
}


def choices_for(code: str, catalog: Catalog) -> list[tuple[str, str]] | None:
    """(value, label) checkboxes for a list or enum signal; None for signals without choices."""
    match code:
        case "USES_CATALOG_CHEMICAL":
            return [(p.id, p.name) for p in catalog.for_side("buyer")]
        case "MAKES_CATALOG_CHEMICAL":
            return [(p.id, p.name) for p in catalog.for_side("supplier")]
        case "INDUSTRY_MATCH":
            return list(INDUSTRIES.items())
        case "QUALITY_CERTS" | "SUPPLIER_CERTS":
            return [(c, c) for c in CERT_NAMES]
        case "COMPANY_TYPE":
            return [(k, _cap(COMPANY_TYPE_LABELS[k])) for k in COMPANY_TYPES]
        case "MANUFACTURER_NOT_TRADER":
            return [(k, _cap(PRODUCER_LABELS[k])) for k in PRODUCER_KINDS]
    return None


# --------------------------------------------------------------------------- parsing


def _one(form: FormData, name: str) -> str | None:
    value = form.get(name)
    if value is None or isinstance(value, str):
        return value
    raise ValueError(BAD_FORM)  # a file upload where text belongs


def _values(form: FormData, sig: SignalDef, choices: list[tuple[str, str]]) -> list[str]:
    allowed = [v for v, _ in choices]
    picked = form.getlist(f"values.{sig.code}")
    if any(not isinstance(v, str) or v not in allowed for v in picked):
        raise ValueError(f"One of the choices for “{sig.label}” isn't valid.")
    return [v for v in allowed if v in picked]  # choice order, no duplicates


def _number(form: FormData, sig: SignalDef, spec: NumberInput) -> float | None:
    raw = (_one(form, f"number.{sig.code}") or "").strip()
    if not raw:
        return None
    message = f"Enter a number from 0 to {spec.maximum:,} for “{sig.label}”, or leave it blank."
    try:
        number = float(raw)
    except ValueError:
        raise ValueError(message) from None
    if not math.isfinite(number) or not 0 <= number <= spec.maximum:
        raise ValueError(message)
    return number


def _condition(form: FormData, sig: SignalDef, catalog: Catalog) -> Condition:
    choices = choices_for(sig.code, catalog)
    if choices is not None:
        values = _values(form, sig, choices)
        if values:
            op = "contains_any" if sig.value_kind == "list" else "in"
            return Condition(code=sig.code, op=op, values=values)
    spec = NUMBER_INPUTS.get(sig.code)
    if spec is not None:
        number = _number(form, sig, spec)
        if number is not None:
            return Condition(code=sig.code, op=spec.op, field=spec.field, number=number)
    return Condition(code=sig.code, op="present")


def _min_score(raw: str | None) -> int | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    if not re.fullmatch(r"[0-9]{1,3}", raw) or int(raw) > 100:
        raise ValueError(MIN_SCORE_ERROR)
    return int(raw)


def _flag(raw: str | None, label: str) -> bool:
    if not raw:
        return False
    if raw not in _FLAG_ON:
        raise ValueError(f"“{label}” isn't a valid choice.")
    return True


def cohort_from_form(form: FormData, side: str, *, catalog: Catalog) -> CohortDefinition:
    """The cohort definition the submitted fields describe, for one side's signals.

    Raises ``ValueError`` with a message for the user when a value is invalid."""
    if side not in SIDES:
        raise ValueError("That side isn't valid.")
    must: list[Condition] = []
    must_not: list[Condition] = []
    for sig in for_side(cast(Side, side)):
        mode = _one(form, f"mode.{sig.code}") or "ignore"
        if mode not in MODES:
            raise ValueError(f"“{sig.label}”: that isn't a valid choice.")
        if mode != "ignore":
            (must if mode == "must" else must_not).append(_condition(form, sig, catalog))
    try:
        return CohortDefinition(
            side=cast(Side, side),
            must=must,
            must_not=must_not,
            min_score=_min_score(_one(form, "min_score")),
            include_disqualified=_flag(_one(form, "include_disqualified"), "Include disqualified"),
            include_unprofiled=_flag(_one(form, "include_unprofiled"), UNPROFILED_LABEL),
            require_sendable_contact=_flag(_one(form, "require_sendable_contact"), SENDABLE_LABEL),
        )
    except ValidationError:  # pragma: no cover - the parser only builds valid conditions
        raise ValueError(BAD_FORM) from None


# --------------------------------------------------------------------------- form state


@dataclass
class RowState:
    mode: str = "ignore"
    values: list[str] = field(default_factory=list)
    number: str = ""


@dataclass
class FormState:
    """What the form shows: a saved cohort, or the fields just submitted (kept on errors)."""

    name: str = ""
    rows: dict[str, RowState] = field(default_factory=dict)
    min_score: str = ""
    include_disqualified: bool = False
    include_unprofiled: bool = False
    require_sendable_contact: bool = False

    def row(self, code: str) -> RowState:
        return self.rows.get(code) or RowState()


def _plain_number(n: float) -> str:
    return str(int(n)) if n.is_integer() else f"{n:g}"


def state_from_definition(name: str, defn: CohortDefinition) -> FormState:
    rows: dict[str, RowState] = {}
    for mode, conditions in (("must", defn.must), ("must_not", defn.must_not)):
        for cond in conditions:
            number = _plain_number(cond.number) if cond.number is not None else ""
            rows[cond.code] = RowState(mode, list(cond.values), number)
    return FormState(
        name=name,
        rows=rows,
        min_score="" if defn.min_score is None else str(defn.min_score),
        include_disqualified=defn.include_disqualified,
        include_unprofiled=defn.include_unprofiled,
        require_sendable_contact=defn.require_sendable_contact,
    )


def _text(value: object) -> str:
    return value[:_MAX_ECHO] if isinstance(value, str) else ""


def state_from_form(form: FormData, side: str, catalog: Catalog) -> FormState:
    """The submitted selections, leniently: shown again with an error message, so invalid
    entries in text boxes are kept as typed, unknown choices are dropped."""
    rows: dict[str, RowState] = {}
    for sig in for_side(cast(Side, side)):  # callers pass a checked side
        mode = form.get(f"mode.{sig.code}")
        if mode not in ("must", "must_not"):
            continue
        allowed = [v for v, _ in choices_for(sig.code, catalog) or []]
        picked = form.getlist(f"values.{sig.code}")
        rows[sig.code] = RowState(
            str(mode),
            [v for v in allowed if v in picked],
            _text(form.get(f"number.{sig.code}")),
        )
    return FormState(
        name=_text(form.get("name")),
        rows=rows,
        min_score=_text(form.get("min_score")),
        include_disqualified=form.get("include_disqualified") in _FLAG_ON,
        include_unprofiled=form.get("include_unprofiled") in _FLAG_ON,
        require_sendable_contact=form.get("require_sendable_contact") in _FLAG_ON,
    )


# --------------------------------------------------------------------------- summary

# For "present" / "absent": (positive phrase, negative phrase).
_PRESENT: dict[str, tuple[str, str]] = {
    "INDUSTRY_MATCH": ("works in one of our industries", "works in none of our industries"),
    "USES_CATALOG_CHEMICAL": ("uses a chemical we sell", "uses none of our chemicals"),
    "COMPANY_TYPE": ("company type known", "company type unknown"),
    "US_LOCATION": ("based in the US", "not based in the US"),
    "NEAR_WAREHOUSE": ("near an Elchemy warehouse", "not near an Elchemy warehouse"),
    "HIRING_PROCUREMENT": ("hiring in purchasing", "not hiring in purchasing"),
    "QUALITY_CERTS": ("has a quality certification", "no quality certification"),
    "MAKES_CATALOG_CHEMICAL": ("makes a chemical we sell", "makes none of our chemicals"),
    "MANUFACTURER_NOT_TRADER": ("manufacturer or trader known", "manufacturer or trader unclear"),
    "EXPORTS": ("exports", "doesn't export"),
    "EXPORTS_TO_US": ("exports to the US", "doesn't export to the US"),
    "SUPPLIER_CERTS": ("has a quality certification", "no quality certification"),
    "STATED_CAPACITY": ("states production capacity", "doesn't state production capacity"),
    "REGION": ("region known", "region unknown"),
}
# For lists of values: (positive verb, negative verb).
_VERBS: dict[str, tuple[str, str]] = {
    "INDUSTRY_MATCH": ("works in", "doesn't work in"),
    "USES_CATALOG_CHEMICAL": ("uses", "doesn't use"),
    "MAKES_CATALOG_CHEMICAL": ("makes", "doesn't make"),
    "QUALITY_CERTS": ("has", "doesn't have"),
    "SUPPLIER_CERTS": ("has", "doesn't have"),
    "REGION": ("in", "not in"),
}
_NOUNS = {"COMPANY_TYPE": COMPANY_TYPE_LABELS, "MANUFACTURER_NOT_TRADER": PRODUCER_LABELS}


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def _join(items: list[str], word: str) -> str:
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} {word} {items[-1]}"


def _article(noun: str) -> str:
    return f"an {noun}" if noun[:1].lower() in "aeiou" else f"a {noun}"


def _amount(n: float) -> str:
    return f"{n:,.0f}" if n.is_integer() else f"{n:,g}"


def _value_names(cond: Condition, catalog: Catalog) -> list[str]:
    names: list[str] = []
    for v in cond.values:
        if cond.code in ("USES_CATALOG_CHEMICAL", "MAKES_CATALOG_CHEMICAL"):
            try:
                names.append(catalog.by_id(v).name)
            except KeyError:
                names.append(v)
        elif cond.code == "INDUSTRY_MATCH":
            names.append(INDUSTRIES.get(v, v))
        elif cond.code in _NOUNS:
            names.append(_NOUNS[cond.code].get(v, v.replace("_", " ")))
        else:
            names.append(v)
    return names


def _phrase(cond: Condition, catalog: Catalog, *, negate: bool) -> str:
    """One condition as a short lower-case phrase; ``negate`` for a must-not condition."""
    yes, no = _PRESENT.get(cond.code, (by_code(cond.code).label.lower(), "not known"))
    match cond.op:
        case "present" | "is_true":
            return no if negate else yes
        case "absent" | "is_false":
            return yes if negate else no
        case "contains_any" | "contains_all" | "in":
            names = _value_names(cond, catalog)
            listed = _join(names, "and" if cond.op == "contains_all" else "or")
            if cond.code in _NOUNS:
                return f"not {_article(listed)}" if negate else _article(listed)
            verb, not_verb = _VERBS.get(cond.code, ("is", "isn't"))
            return f"{not_verb if negate else verb} {listed}"
        case "lte" | "gte":
            amount = _amount(cond.number or 0)
            if cond.code == "NEAR_WAREHOUSE":
                text = (
                    f"within {amount} miles of an Elchemy warehouse"
                    if cond.op == "lte"
                    else f"at least {amount} miles from an Elchemy warehouse"
                )
            else:
                bound = "at most" if cond.op == "lte" else "at least"
                text = f"capacity {bound} {amount} MT/year"
            return f"not {text}" if negate else text
    return yes  # pragma: no cover


def describe(defn: CohortDefinition, catalog: Catalog) -> str:
    """The definition in plain English, e.g. "Uses SLES or CAPB · not a distributor ·
    score ≥ 40"."""
    parts: list[str] = [_phrase(c, catalog, negate=False) for c in defn.must]
    parts += [_phrase(c, catalog, negate=True) for c in defn.must_not]
    if defn.min_score is not None:
        parts.append(f"score ≥ {defn.min_score}")
    if not parts:
        parts.append(f"all {defn.side} companies")
    if defn.require_sendable_contact:
        parts.append("has a sendable contact")
    if defn.include_disqualified:
        parts.append("including disqualified")
    if defn.include_unprofiled:
        parts.append("including companies we couldn't read yet")
    return _cap(" · ".join(parts))


def stale_warnings(
    defn: CohortDefinition, confirmed: Collection[str], catalog: Catalog
) -> list[str]:
    """One line per condition on a signal that isn't confirmed now (nothing collects it, so the
    cohort doesn't select what it says), e.g. "Not collected now: “not a distributor” currently
    excludes nobody."."""
    lines: list[str] = []
    for must, conditions in ((True, defn.must), (False, defn.must_not)):
        for cond in conditions:
            if cond.code in confirmed:
                continue
            # Unconfirmed signals are never loaded: every company looks like it never had one.
            holds = condition_holds(cond, None)
            if must:
                effect = "currently has no effect" if holds else "currently matches nobody"
            else:
                effect = "currently excludes everybody" if holds else "currently excludes nobody"
            phrase = _phrase(cond, catalog, negate=not must)
            lines.append(f"Not collected now: “{phrase}” {effect}.")
    return lines
