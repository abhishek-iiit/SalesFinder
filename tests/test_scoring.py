"""score_company: pure scoring of a company's latest observations."""

from salesfinder.config import ScoringConfig
from salesfinder.profiling.scoring import ScoreResult, is_present, score_company
from salesfinder.signals.catalog import for_side

CONFIG = ScoringConfig()
BUYER_CODES = frozenset(s.code for s in for_side("buyer"))
SUPPLIER_CODES = frozenset(s.code for s in for_side("supplier"))

BUYER_ALL = {
    "USES_CATALOG_CHEMICAL": ["sles", "capb"],
    "INDUSTRY_MATCH": ["personal_care"],
    "COMPANY_TYPE": "manufacturer",
    "US_LOCATION": {"state": "TX", "city": "Houston"},
    "NEAR_WAREHOUSE": {"warehouse": "Houston, TX", "miles": 3},
    "HIRING_PROCUREMENT": {"hiring": True, "roles": ["procurement"]},
    "QUALITY_CERTS": ["ISO 9001"],
}


def _buyer(latest: dict[str, object], confirmed: frozenset[str] = BUYER_CODES) -> ScoreResult:
    return score_company(latest, side="buyer", confirmed=confirmed, config=CONFIG)


def test_buyer_full_score() -> None:
    result = _buyer(BUYER_ALL)
    assert result.score == 100
    assert result.disqualified is False
    # One reason per buyer signal that earned points (7 buyer signals, weights sum to 100).
    assert result.reasons == [
        "Works in our industries: personal_care (+15)",
        "Mentions chemicals we sell: sles, capb (+30)",
        "What kind of company they are: manufacturer (+20)",
        "Based in the US: Houston, TX (+5)",
        "Close to an Elchemy warehouse: 3 miles from Houston, TX (+10)",
        "Hiring in purchasing: procurement (+10)",
        "Quality certifications: ISO 9001 (+10)",
    ]


def test_buyer_distributor_and_contract_manufacturer() -> None:
    assert _buyer({"COMPANY_TYPE": "distributor"}).score == 10
    assert _buyer({"COMPANY_TYPE": "contract_manufacturer"}).reasons == [
        "What kind of company they are: contract manufacturer (+20)"
    ]
    assert _buyer({"COMPANY_TYPE": "private_label"}).score == 20
    assert _buyer({"COMPANY_TYPE": "unknown"}) == ScoreResult(0, False, [])


def test_buyer_disqualified_retailer() -> None:
    result = _buyer({**BUYER_ALL, "COMPANY_TYPE": "retailer"})
    assert result == ScoreResult(score=0, disqualified=True, reasons=["Disqualified: retailer"])
    assert _buyer({"COMPANY_TYPE": "service"}).disqualified is True


def test_unconfirmed_signals_ignored() -> None:
    result = _buyer(BUYER_ALL, confirmed=frozenset({"QUALITY_CERTS"}))
    assert result.score == 10
    assert result.reasons == ["Quality certifications: ISO 9001 (+10)"]
    # An unconfirmed COMPANY_TYPE can't disqualify either.
    retailer = _buyer({"COMPANY_TYPE": "retailer"}, confirmed=frozenset({"QUALITY_CERTS"}))
    assert retailer == ScoreResult(0, False, [])


def test_near_warehouse_threshold() -> None:
    near = _buyer({"NEAR_WAREHOUSE": {"warehouse": "Houston, TX", "miles": 499}})
    far = _buyer({"NEAR_WAREHOUSE": {"warehouse": "Houston, TX", "miles": 501}})
    assert near.score == 10
    assert far.score == 0
    assert _buyer({"NEAR_WAREHOUSE": {"warehouse": "Houston, TX", "miles": 500}}).score == 10
    assert _buyer({"NEAR_WAREHOUSE": {"found": False}}).score == 0


def test_negative_values_score_nothing() -> None:
    negatives = {
        "USES_CATALOG_CHEMICAL": [],
        "INDUSTRY_MATCH": [],
        "COMPANY_TYPE": "unknown",
        "US_LOCATION": {"found": False},
        "NEAR_WAREHOUSE": {"found": False},
        "HIRING_PROCUREMENT": {"hiring": False, "roles": [], "careers_page": False},
        "QUALITY_CERTS": [],
    }
    assert _buyer(negatives) == ScoreResult(0, False, [])


def test_garbage_values_are_ignored() -> None:
    garbage = {
        "USES_CATALOG_CHEMICAL": "sles",
        "COMPANY_TYPE": ["retailer"],
        "US_LOCATION": "TX",
        "NEAR_WAREHOUSE": {"miles": "near"},
        "HIRING_PROCUREMENT": True,
        "QUALITY_CERTS": None,
    }
    assert _buyer(garbage) == ScoreResult(0, False, [])


def test_supplier_full_score() -> None:
    latest = {
        "MAKES_CATALOG_CHEMICAL": ["sles"],
        "MANUFACTURER_NOT_TRADER": "manufacturer",
        "EXPORTS": True,
        "EXPORTS_TO_US": True,
        "SUPPLIER_CERTS": ["ISO 9001", "Halal"],
        "STATED_CAPACITY": {"text": "500 MT per month", "mt_per_year": 6000},
        "REGION": "Gujarat",
    }
    result = score_company(latest, side="supplier", confirmed=SUPPLIER_CODES, config=CONFIG)
    assert result.score == 100
    assert result.reasons == [
        "Makes chemicals we sell: sles (+35)",
        "Makes it themselves: manufacturer (+20)",
        "Exports: yes (+10)",
        "Exports to the US: yes (+10)",
        "Quality certifications: ISO 9001, Halal (+15)",
        "States production capacity: 500 MT per month (+10)",
    ]


def test_supplier_trader_not_disqualified() -> None:
    latest = {"MANUFACTURER_NOT_TRADER": "trader", "EXPORTS": True}
    result = score_company(latest, side="supplier", confirmed=SUPPLIER_CODES, config=CONFIG)
    assert result == ScoreResult(score=10, disqualified=False, reasons=["Exports: yes (+10)"])


def test_score_is_clamped() -> None:
    config = ScoringConfig(buyer={**CONFIG.buyer, "USES_CATALOG_CHEMICAL": 90})
    result = score_company(BUYER_ALL, side="buyer", confirmed=BUYER_CODES, config=config)
    assert result.score == 100


def test_long_lists_are_shortened_in_reasons() -> None:
    chems = [f"chem{i}" for i in range(9)]
    result = _buyer({"USES_CATALOG_CHEMICAL": chems})
    assert result.reasons == [
        "Mentions chemicals we sell: chem0, chem1, chem2, chem3 +5 more (+30)"
    ]


def test_is_present() -> None:
    assert is_present("QUALITY_CERTS", ["ISO 9001"])
    assert not is_present("QUALITY_CERTS", [])
    assert is_present("US_LOCATION", {"state": "TX", "city": "Houston"})
    assert not is_present("US_LOCATION", {"found": False})
    assert not is_present("HIRING_PROCUREMENT", {"hiring": False, "roles": []})
    assert is_present("HIRING_PROCUREMENT", {"hiring": True, "roles": ["buyer"]})
    assert not is_present("COMPANY_TYPE", "unknown")
    assert is_present("COMPANY_TYPE", "retailer")
    assert not is_present("MANUFACTURER_NOT_TRADER", "unclear")
    assert is_present("EXPORTS", True)
    assert not is_present("EXPORTS", False)
    assert not is_present("REGION", None)
    assert is_present("REGION", "Gujarat")


def test_partial_weight_override_keeps_the_other_defaults() -> None:
    config = ScoringConfig(buyer={"USES_CATALOG_CHEMICAL": 50}, supplier={"EXPORTS": 0})
    assert config.buyer == {**ScoringConfig().buyer, "USES_CATALOG_CHEMICAL": 50}
    assert config.supplier["MANUFACTURER"] == 20 and config.supplier["EXPORTS"] == 0
    result = score_company(BUYER_ALL, side="buyer", confirmed=BUYER_CODES, config=config)
    assert result.score == 100  # 120 clamped; no KeyError for the keys not overridden
    supplier = score_company(
        {"MANUFACTURER_NOT_TRADER": "manufacturer", "EXPORTS": True},
        side="supplier",
        confirmed=SUPPLIER_CODES,
        config=config,
    )
    assert supplier.score == 20


def test_unknown_weight_keys_are_still_rejected() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="unknown buyer scoring key 'EXPORTS'"):
        ScoringConfig(buyer={"EXPORTS": 10})


def test_numbers_too_big_for_a_float_earn_nothing_and_never_crash() -> None:
    supplier = score_company(
        {"STATED_CAPACITY": {"mt_per_year": 10**400}},
        side="supplier",
        confirmed=SUPPLIER_CODES,
        config=CONFIG,
    )
    assert supplier == ScoreResult(0, False, [])
    assert _buyer({"NEAR_WAREHOUSE": {"miles": 10**400}}) == ScoreResult(0, False, [])
