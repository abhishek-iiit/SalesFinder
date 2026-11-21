"""Job-title categories and target ranking per side."""

from dataclasses import dataclass

import pytest

from salesfinder.contacts.titles import CATEGORIES, categorize_title, rank_people
from salesfinder.db.models.contacts import TITLE_CATEGORIES


def test_categories_are_the_model_categories() -> None:
    assert CATEGORIES == TITLE_CATEGORIES


def test_categorize() -> None:
    assert categorize_title("Head of Procurement") == "procurement"
    assert categorize_title("MD") == "owner_exec"
    assert categorize_title("Export Manager") == "export"
    assert categorize_title("Accountant") == "other"


@pytest.mark.parametrize(
    "title,category",
    [
        ("Purchase Manager", "procurement"),
        ("Sr. Manager - Purchase", "procurement"),
        ("Sr. Manager \u2013 Purchase", "procurement"),
        ("Chief Procurement Officer", "procurement"),
        ("Purchasing Head", "procurement"),
        ("Senior Buyer", "procurement"),
        ("Strategic Sourcing Lead", "procurement"),
        ("Supply Chain Manager", "supply_chain"),
        ("Head - Logistics", "supply_chain"),
        ("Materials Manager", "supply_chain"),
        ("Head of R&D", "rnd"),
        ("R&D Manager", "rnd"),
        ("Research Scientist", "rnd"),
        ("Formulation Chemist", "rnd"),
        ("Technical Director", "rnd"),
        ("Founder & CEO", "owner_exec"),
        ("FOUNDER & CEO", "owner_exec"),
        ("Co-Founder", "owner_exec"),
        ("Proprietor", "owner_exec"),
        ("Managing Partner", "owner_exec"),
        ("Managing Director", "owner_exec"),
        ("Chairman", "owner_exec"),
        ("Promoter Director", "owner_exec"),
        ("General Manager", "owner_exec"),
        ("Chief Executive Officer", "owner_exec"),
        ("Vice President", "owner_exec"),
        ("Exports", "export"),
        ("International Sales Manager", "export"),
        ("Business Development Manager", "business_dev"),
        ("BD Head", "business_dev"),
        ("Head of Sales", "business_dev"),
        ("Sales Head", "business_dev"),
        ("VP Sales", "business_dev"),
        ("Marketing Manager", "marketing"),
        ("Sales Executive", "other"),
        ("Sales Director", "business_dev"),
        ("Director of Sales", "business_dev"),
        ("Director - Sales", "business_dev"),
        ("Director \u2013 Sales", "business_dev"),
        ("Vice President Sales", "business_dev"),
        ("Vice President, Sales", "business_dev"),
        ("Director - Business Development", "business_dev"),
        ("Director", "owner_exec"),
        ("Director - Exports", "owner_exec"),
        ("Accounts Officer", "other"),
        ("Buyers' Guide", "other"),  # whole words only
        ("Chemistry Teacher", "other"),
        ("Exporter of the Year", "export"),  # export* is a stem
        ("", "other"),
    ],
)
def test_categorize_extras(title: str, category: str) -> None:
    assert categorize_title(title) == category


@dataclass(frozen=True)
class _P:
    name: str
    category: str


PEOPLE = [
    _P("a", "marketing"),
    _P("b", "export"),
    _P("c", "other"),
    _P("d", "owner_exec"),
    _P("e", "procurement"),
    _P("f", "owner_exec"),
]


def test_rank_buyer_vs_supplier() -> None:
    buyer = rank_people(PEOPLE, side="buyer", limit=10)
    assert [p.name for p in buyer] == ["e", "d", "f", "a", "b"]
    supplier = rank_people(PEOPLE, side="supplier", limit=10)
    assert [p.name for p in supplier] == ["b", "d", "f", "a", "e"]
    assert "c" not in {p.name for p in buyer + supplier}


def test_rank_limit_and_odd_input() -> None:
    assert [p.name for p in rank_people(PEOPLE, side="buyer", limit=2)] == ["e", "d"]
    assert rank_people(PEOPLE, side="buyer", limit=0) == []
    assert rank_people([], side="supplier", limit=2) == []
    assert rank_people([_P("x", "other")], side="buyer", limit=2) == []
