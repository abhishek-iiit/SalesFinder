"""Query building and cost estimates (pure logic, shipped catalog and config)."""

from decimal import Decimal
from pathlib import Path

import pytest

from salesfinder.catalog import Catalog, load_catalog
from salesfinder.config import DiscoveryConfig, _read_yaml
from salesfinder.discovery.queries import Query, build_queries, estimate_cost

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
# The shipped base file only (never a developer's salesfinder.local.yaml).
cfg = DiscoveryConfig.model_validate(_read_yaml(CONFIG_DIR / "salesfinder.yaml")["discovery"])
cat: Catalog = load_catalog(CONFIG_DIR)

SLES_PC_TEXAS = [
    '"sodium laureth sulfate" shampoo manufacturer Texas',
    '"sodium laureth sulfate" skin care manufacturer Texas',
    '"sodium laureth sulfate" cosmetics manufacturer Texas',
]


def _build(
    side: str, products: list[str], industries: list[str], regions: list[str]
) -> list[Query]:
    return build_queries(
        side=side,
        product_ids=products,
        industries=industries,
        region_names=regions,
        catalog=cat,
        config=cfg,
    )


def test_buyer_queries_cross_product_keyword_region() -> None:
    qs = _build("buyer", ["sles"], ["personal_care"], ["Texas"])
    assert [q.q for q in qs] == SLES_PC_TEXAS
    assert {q.gl for q in qs} == {"us"}
    assert {(q.region, q.country) for q in qs} == {("Texas", "US")}


def test_buyer_order_is_product_industry_keyword_region() -> None:
    qs = _build("buyer", ["sles", "capb"], ["personal_care"], ["Texas", "Ohio"])
    assert [q.q for q in qs[:2]] == [
        '"sodium laureth sulfate" shampoo manufacturer Texas',
        '"sodium laureth sulfate" shampoo manufacturer Ohio',
    ]
    assert len(qs) == 2 * 3 * 2
    assert qs[-1].q == '"cocamidopropyl betaine" cosmetics manufacturer Ohio'


def test_buyer_skips_industries_the_product_lacks() -> None:
    qs = _build("buyer", ["sles"], ["personal_care", "oil_gas"], ["Texas"])
    assert [q.q for q in qs] == SLES_PC_TEXAS

    with pytest.raises(ValueError) as info:
        _build("buyer", ["sles"], ["oil_gas"], ["Texas"])
    assert str(info.value) == (
        "Those choices produce no searches. Pick an industry each product belongs to."
    )


def test_supplier_queries() -> None:
    qs = _build("supplier", ["capb"], [], ["Gujarat"])
    assert qs == [
        Query(
            q='"cocamidopropyl betaine" manufacturer exporter Gujarat',
            gl="in",
            region="Gujarat",
            country="IN",
        )
    ]
    # Industries are ignored for suppliers, even unknown ones.
    assert _build("supplier", ["capb"], ["oil_gas", "bogus"], ["Gujarat"]) == qs


def test_duplicates_removed_order_kept() -> None:
    qs = _build("buyer", ["sles", "sles"], ["personal_care", "personal_care"], ["Texas", "Texas"])
    assert [q.q for q in qs] == SLES_PC_TEXAS


@pytest.mark.parametrize(
    ("side", "products", "industries", "regions", "message"),
    [
        ("buyer", ["nope"], ["personal_care"], ["Texas"], "Unknown product: nope."),
        ("buyer", ["sles"], ["nope"], ["Texas"], "Unknown industry: nope."),
        ("buyer", ["sles"], ["personal_care"], ["Gujarat"], "Unknown region: Gujarat."),
        ("supplier", ["capb"], [], ["Texas"], "Unknown region: Texas."),
        ("buyer", [], ["personal_care"], ["Texas"], "Pick at least one product."),
        ("buyer", ["sles"], [], ["Texas"], "Pick at least one industry."),
        ("buyer", ["sles"], ["personal_care"], [], "Pick at least one region."),
        ("both", ["sles"], ["personal_care"], ["Texas"], "Unknown side: both."),
    ],
)
def test_invalid_selections(
    side: str, products: list[str], industries: list[str], regions: list[str], message: str
) -> None:
    with pytest.raises(ValueError) as info:
        _build(side, products, industries, regions)
    assert str(info.value) == message


def test_product_not_offered_on_side() -> None:
    only_buyer = cat.model_copy(
        update={
            "products": [
                p.model_copy(update={"sides": ["buyer"]}) if p.id == "capb" else p
                for p in cat.products
            ]
        }
    )
    with pytest.raises(ValueError) as info:
        build_queries(
            side="supplier",
            product_ids=["capb"],
            industries=[],
            region_names=["Gujarat"],
            catalog=only_buyer,
            config=cfg,
        )
    assert str(info.value) == "Cocamidopropyl Betaine (CAPB) isn't offered on the supplier side."


def test_limit() -> None:
    regions = [r.name for r in cfg.buyer_regions]  # 8 regions
    products = [p.id for p in cat.products if "personal_care" in p.industries]
    # 3 keywords x 8 regions = 24 per product; 9 products -> 216 > 200.
    assert len(products) >= 9
    with pytest.raises(ValueError) as info:
        _build("buyer", products[:9], ["personal_care"], regions)
    assert "limit per job is 200" in str(info.value)
    assert str(info.value) == (
        "That's 216 searches; the limit per job is 200. Narrow the products, industries or regions."
    )
    small = cfg.model_copy(update={"max_queries_per_job": 3})
    assert (
        len(
            build_queries(
                side="buyer",
                product_ids=["sles"],
                industries=["personal_care"],
                region_names=["Texas"],
                catalog=cat,
                config=small,
            )
        )
        == 3
    )


def test_estimate_cost() -> None:
    two_pages = cfg.model_copy(
        update={"pages_per_query": 2, "serper_price_per_1000_usd": Decimal("1.00")}
    )
    assert estimate_cost(30, two_pages) == (60, Decimal("0.0600"))
    searches, cost = estimate_cost(3, cfg)
    assert (searches, cost) == (3, Decimal("0.0030"))
    assert str(cost) == "0.0030"
    odd = cfg.model_copy(update={"serper_price_per_1000_usd": Decimal("1.25")})
    assert estimate_cost(1, odd) == (1, Decimal("0.0013"))  # 0.00125 rounds half up
    assert isinstance(estimate_cost(0, cfg)[1], Decimal)
