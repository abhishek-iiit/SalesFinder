import pytest

from salesfinder.signals.detect_buyer import detect_company_type, detect_hiring
from salesfinder.signals.text import PageText, extract


def page(text: str, url: str = "https://a.com/", title: str = "") -> PageText:
    return extract(f"<title>{title}</title><p>{text}</p>", url)


def test_company_type_manufacturer():
    f = detect_company_type(
        [page("We manufacture shampoos at our plant in Ohio. Our manufacturing facility is big.")]
    )
    assert (f.code, f.value) == ("COMPANY_TYPE", "manufacturer")
    assert f.evidence is not None
    assert "We manufacture" in f.evidence.snippet


def test_company_type_retailer():
    f = detect_company_type([page("Add to cart now. Free shipping on all orders. Checkout fast.")])
    assert f.value == "retailer"


def test_company_type_unknown_single_hit():
    f = detect_company_type([page("We are a wholesale company.")])
    assert f.value == "unknown"
    assert f.evidence is None


def test_company_type_tie_order():
    f = detect_company_type(
        [page("We manufacture soap. Our plant is new. Distributor of oils. We distribute oils.")]
    )
    assert f.value == "manufacturer"


def test_company_type_counts_across_pages_and_stems():
    f = detect_company_type(
        [
            page("Contract manufacturing for brands."),
            page("Toll manufacturers welcome.", "https://a.com/b"),
        ]
    )
    assert f.value == "contract_manufacturer"


def test_company_type_cmo_whole_word():
    f = detect_company_type([page("CMOS sensors and CMOS chips")])
    assert f.value == "unknown"


def test_hiring_roles_only_on_careers_page():
    text = "Open role: Procurement Manager. Apply today."
    f = detect_hiring([page(text, "https://a.com/careers")])
    assert f.code == "HIRING_PROCUREMENT"
    assert f.value == {"hiring": True, "roles": ["procurement"]}
    assert f.evidence is not None
    assert f.evidence.url == "https://a.com/careers"
    g = detect_hiring([page(text, "https://a.com/about")])
    assert g.value["hiring"] is False
    assert g.evidence is None


def test_hiring_careers_by_title_and_sorted_roles():
    f = detect_hiring(
        [
            page(
                "Buyer wanted. Also R&D chemist and Supply Chain lead.",
                "https://a.com/x",
                "Careers",
            )
        ]
    )
    assert f.value == {"hiring": True, "roles": ["buyer", "r&d chemist", "supply chain"]}


def test_careers_page_without_roles():
    f = detect_hiring([page("We have no openings.", "https://a.com/jobs")])
    assert f.value == {"hiring": False, "roles": []}


def test_no_careers_page():
    f = detect_hiring([page("Procurement", "https://a.com/")])
    assert f.value == {"hiring": False, "roles": [], "careers_page": False}
    assert detect_hiring([]).value == {"hiring": False, "roles": [], "careers_page": False}


@pytest.mark.parametrize("text", ["buyers club", "sourcingx"])
def test_hiring_word_boundaries(text):
    assert detect_hiring([page(text, "https://a.com/careers")]).value["hiring"] is False


# --- final review fixes ------------------------------------------------------------------------


def test_company_type_dtc_maker_with_shop_nav_is_a_manufacturer():
    """A shop nav repeated on every page counts once per page, and any producer evidence rules
    out "retailer"."""
    nav = "Shop Now · Cart · Checkout"
    pages = [page(f"{nav} Welcome.", f"https://a.com/p{i}") for i in range(5)]
    pages.append(
        page(
            f"{nav} About us. We manufacture shampoos in Ohio. Our manufacturing facility is "
            "certified.",
            "https://a.com/about",
        )
    )
    f = detect_company_type(pages)
    assert f.value == "manufacturer"
    assert f.evidence is not None and f.evidence.url == "https://a.com/about"


def test_company_type_phrase_counts_once_per_page():
    f = detect_company_type([page("Shop now. Shop now. Shop now. Shop now.")])
    assert f.value == "unknown"
    g = detect_company_type(
        [page("Shop now.", "https://a.com/1"), page("Shop now.", "https://a.com/2")]
    )
    assert g.value == "retailer"


def test_company_type_retailer_only_without_producer_hits():
    f = detect_company_type(
        [page("Add to cart. Free shipping. Checkout. Shop now. Private label options.")]
    )
    assert f.value == "unknown"  # one producer hit: not a retailer, and too little to say more


def test_company_type_our_plant_based_is_not_a_plant():
    f = detect_company_type(
        [
            page("Try our plant-based shampoo.", "https://a.com/1"),
            page("Our plant-based shampoo is gentle.", "https://a.com/2"),
        ]
    )
    assert f.value == "unknown"
    assert detect_company_type([page("Our plant in Ohio. We manufacture soap.")]).value == (
        "manufacturer"
    )


def test_company_type_cmo_executive_is_not_contract_manufacturing():
    f = detect_company_type(
        [
            page("Our CMO Jane Doe leads marketing.", "https://a.com/1"),
            page("Meet our CMO, Jane.", "https://a.com/2"),
            page("The CMO spoke at the event.", "https://a.com/3"),
        ]
    )
    assert f.value == "unknown"


def test_company_type_cmo_uppercase_only():
    lower = [page("cmo services", "https://a.com/1"), page("a cmo", "https://a.com/2")]
    assert detect_company_type(lower).value == "unknown"
    upper = [page("A trusted CMO for brands.", "https://a.com/1"), page("Toll manufacturing.")]
    assert detect_company_type(upper).value == "contract_manufacturer"
