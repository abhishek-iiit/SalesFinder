"""Elchemy product catalog and facts (team-edited YAML, no .local merge)."""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from salesfinder.config import ConfigError, _read_yaml
from salesfinder.industries import INDUSTRIES

CATALOG_FILE = "catalog.yaml"
FACTS_FILE = "facts.yaml"

Side = Literal["buyer", "supplier"]


class Product(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(pattern=r"^[a-z0-9_]+$")]
    name: str
    search_name: str
    synonyms: list[str] = []
    inci: str | None = None
    cas: list[Annotated[str, Field(pattern=r"^\d{2,7}-\d{2}-\d$")]] = []
    industries: Annotated[list[str], Field(min_length=1)]
    sides: list[Side] = ["buyer", "supplier"]
    confirmed: bool = False

    @field_validator("industries")
    @classmethod
    def _known_industries(cls, v: list[str]) -> list[str]:
        unknown = [i for i in v if i not in INDUSTRIES]
        if unknown:
            raise ValueError(f"unknown industry key(s): {', '.join(unknown)}")
        return v


class Catalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    products: list[Product]

    @model_validator(mode="after")
    def _unique_ids(self) -> "Catalog":
        seen: set[str] = set()
        for p in self.products:
            if p.id in seen:
                raise ValueError(f"duplicate product id {p.id!r}")
            seen.add(p.id)
        return self

    def by_id(self, product_id: str) -> Product:
        for p in self.products:
            if p.id == product_id:
                return p
        raise KeyError(product_id)

    def for_side(self, side: Side) -> list[Product]:
        return [p for p in self.products if side in p.sides]


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    confirmed: bool = False


class Persona(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = ""
    title: str = ""
    signature: str = ""
    confirmed: bool = False


class Warehouse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    city: str
    state: str
    country: str = "US"
    label: str
    lat: float
    lon: float


class Facts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_name: str
    website: str
    postal_address: str = ""
    postal_address_confirmed: bool = False
    warehouses: list[Warehouse]
    claims: list[Claim]
    personas: dict[Side, Persona]


def _load[M: BaseModel](config_dir: Path, filename: str, model: type[M]) -> M:
    path = config_dir / filename
    if not path.is_file():
        raise ConfigError(f"{path}: required config file {filename} not found")
    data = _read_yaml(path)
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']} (value: {err['input']!r})"
            for err in exc.errors()
        )
        raise ConfigError(f"{path}: invalid config: {problems}") from exc


def load_catalog(config_dir: Path) -> Catalog:
    return _load(config_dir, CATALOG_FILE, Catalog)


def load_facts(config_dir: Path) -> Facts:
    return _load(config_dir, FACTS_FILE, Facts)
