"""The cohort condition language: pydantic models with validation against the signal catalog."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from salesfinder.signals.catalog import SIGNALS, by_code

Op = Literal[
    "present", "absent", "contains_any", "contains_all", "in", "is_true", "is_false", "lte", "gte"
]

_BASE = {"present", "absent"}
_OPS_BY_KIND: dict[str, set[str]] = {
    "list": _BASE | {"contains_any", "contains_all"},
    "enum": _BASE | {"in"},
    "text": _BASE | {"in"},
    "bool": _BASE | {"is_true", "is_false"},
    "object": _BASE | {"lte", "gte"},
}
# Numeric fields an object signal can be compared on.
NUMERIC_FIELDS = {"NEAR_WAREHOUSE": "miles", "STATED_CAPACITY": "mt_per_year"}


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    op: Op
    values: list[str] = []
    field: str | None = None
    number: float | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        try:
            sig = by_code(self.code)
        except KeyError:
            raise ValueError(f"Unknown signal {self.code!r}.") from None
        allowed = set(_OPS_BY_KIND[sig.value_kind])
        if self.code == "HIRING_PROCUREMENT":
            allowed = _BASE | {"is_true", "is_false"}
        if self.op not in allowed:
            raise ValueError(f"{self.op!r} does not apply to {self.code}.")
        if self.op in {"contains_any", "contains_all", "in"} and not self.values:
            raise ValueError(f"{self.op!r} needs at least one value.")
        if self.op in {"lte", "gte"}:
            if self.number is None:
                raise ValueError(f"{self.op!r} needs a number.")
            expected = NUMERIC_FIELDS.get(self.code)
            if expected is None or self.field != expected:
                raise ValueError(f"{self.code} is compared on field {expected!r}.")
        elif self.code == "HIRING_PROCUREMENT" and self.op in {"is_true", "is_false"}:
            if self.field not in (None, "hiring"):
                raise ValueError("HIRING_PROCUREMENT is tested on field 'hiring'.")
        elif self.field is not None:
            raise ValueError(f"{self.op!r} does not take a field.")
        return self


class CohortDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    side: Literal["buyer", "supplier"]
    must: list[Condition] = []
    must_not: list[Condition] = []
    min_score: int | None = Field(default=None, ge=0, le=100)
    include_disqualified: bool = False
    # By default only companies whose website was read (profile ok / partial) match: one that
    # is pending, failed, blocked or has no site has no findings, so "must not" can't exclude it.
    include_unprofiled: bool = False
    # Only companies with at least one target who has a sendable address (Phase 4).
    require_sendable_contact: bool = False

    @model_validator(mode="after")
    def _sides(self) -> Self:
        side_of = {s.code: s.side for s in SIGNALS}
        for cond in [*self.must, *self.must_not]:
            if side_of[cond.code] != self.side:
                raise ValueError(f"{cond.code} is not a {self.side} signal.")
        return self
