"""Turn a teammate's selection into search queries, and price them. Pure logic, no I/O."""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from salesfinder.catalog import Catalog, Product
from salesfinder.config import DiscoveryConfig, Region
from salesfinder.industries import INDUSTRIES, SIDES

MONEY_QUANTUM = Decimal("0.0001")  # money is stored as Numeric(10, 4)
NO_SEARCHES = "Those choices produce no searches. Pick an industry each product belongs to."
TOO_MANY = (
    "That's {n} searches; the limit per job is {max}. Narrow the products, industries or regions."
)


@dataclass(frozen=True)
class Query:
    q: str
    gl: str
    region: str
    country: str


def _unique[T](items: Sequence[T]) -> list[T]:
    return list(dict.fromkeys(items))


def _products(side: str, product_ids: Sequence[str], catalog: Catalog) -> list[Product]:
    products: list[Product] = []
    for pid in _unique(product_ids):
        try:
            product = catalog.by_id(pid)
        except KeyError:
            raise ValueError(f"Unknown product: {pid}.") from None
        if side not in product.sides:
            raise ValueError(f"{product.name} isn't offered on the {side} side.")
        products.append(product)
    return products


def _regions(side: str, region_names: Sequence[str], config: DiscoveryConfig) -> list[Region]:
    pool = config.buyer_regions if side == "buyer" else config.supplier_regions
    available = {r.name: r for r in pool}
    regions: list[Region] = []
    for name in _unique(region_names):
        region = available.get(name)
        if region is None:
            raise ValueError(f"Unknown region: {name}.")
        regions.append(region)
    return regions


def _fill(template: str, *, chemical: str, region: str, keyword: str = "") -> str:
    # str.replace, not str.format: a stray brace in a template or product name can't break it.
    text = template.replace("{chemical}", chemical).replace("{region}", region)
    return " ".join(text.replace("{keyword}", keyword).split())


def build_queries(
    *,
    side: str,
    product_ids: Sequence[str],
    industries: Sequence[str],
    region_names: Sequence[str],
    catalog: Catalog,
    config: DiscoveryConfig,
) -> list[Query]:
    """Buyer: product x industry (the product's own) x keyword x region; supplier: product x
    region (industries ignored). Duplicates (same ``q`` and ``gl``) are dropped, order kept."""
    if side not in SIDES:
        raise ValueError(f"Unknown side: {side}.")
    if not product_ids:
        raise ValueError("Pick at least one product.")
    chosen_industries: list[str] = []
    if side == "buyer":
        if not industries:
            raise ValueError("Pick at least one industry.")
        for key in _unique(industries):
            if key not in INDUSTRIES:
                raise ValueError(f"Unknown industry: {key}.")
            chosen_industries.append(key)
    if not region_names:
        raise ValueError("Pick at least one region.")
    products = _products(side, product_ids, catalog)
    regions = _regions(side, region_names, config)

    template = config.templates["buyer" if side == "buyer" else "supplier"]
    found: dict[tuple[str, str], Query] = {}

    def add(text: str, region: Region) -> None:
        found.setdefault((text, region.gl), Query(text, region.gl, region.name, region.country))

    for product in products:
        if side == "supplier":
            for region in regions:
                add(_fill(template, chemical=product.search_name, region=region.name), region)
            continue
        for industry in chosen_industries:
            if industry not in product.industries:
                continue
            for keyword in config.industry_keywords.get(industry, []):
                for region in regions:
                    text = _fill(
                        template, chemical=product.search_name, region=region.name, keyword=keyword
                    )
                    add(text, region)

    queries = list(found.values())
    if not queries:
        raise ValueError(NO_SEARCHES)
    if len(queries) > config.max_queries_per_job:
        raise ValueError(TOO_MANY.format(n=len(queries), max=config.max_queries_per_job))
    return queries


def search_cost(searches: int, config: DiscoveryConfig) -> Decimal:
    """What ``searches`` Serper searches cost, rounded half up to 0.0001 USD."""
    raw = Decimal(searches) * config.serper_price_per_1000_usd / Decimal(1000)
    return raw.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def estimate_cost(n_queries: int, config: DiscoveryConfig) -> tuple[int, Decimal]:
    """``(searches, cost)``: every query is searched ``pages_per_query`` times."""
    searches = n_queries * config.pages_per_query
    return searches, search_cost(searches, config)
