"""Person-name helpers: normalization, keys, splitting, and the 'is this a person?' gate."""

import pytest

from salesfinder.contacts.names import looks_like_person_name, name_key, normalize_name, split_name


@pytest.mark.parametrize(
    "raw,first,last",
    [
        ("Mr. Rajesh K. Sharma", "Rajesh", "Sharma"),
        ("R. K. Sharma", None, "Sharma"),
        ("PRIYA NAIR", "Priya", "Nair"),
        ("Dr Anil Mehta", "Anil", "Mehta"),
        ("Madonna", "Madonna", None),
        ("José Álvarez", "José", "Álvarez"),
    ],
)
def test_split(raw: str, first: str | None, last: str | None) -> None:
    assert split_name(raw) == (first, last)


@pytest.mark.parametrize(
    "raw,first,last",
    [
        ("Smt. Kavita Desai", "Kavita", "Desai"),
        ("Dr.Anil Mehta", "Anil", "Mehta"),  # honorific glued to the name
        ("R.K. Sharma", None, "Sharma"),
        ("Rajesh K.", "Rajesh", None),  # an initial is never a last name either
        ("R.", None, None),
        ("", None, None),
        ("   ", None, None),
    ],
)
def test_split_extras(raw: str, first: str | None, last: str | None) -> None:
    assert split_name(raw) == (first, last)


def test_name_key_ascii() -> None:
    assert name_key("José Álvarez") == "jose alvarez"
    assert name_key("Mr. Rajesh  K. Sharma") == "rajesh k sharma"
    assert name_key("RAJESH K SHARMA") == name_key("Rajesh K. Sharma")
    assert name_key("D'Souza-Pinto") == "dsouzapinto"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Mr. Rajesh K. Sharma", "Rajesh K. Sharma"),
        ("  Shri   Ramesh   Patel ", "Ramesh Patel"),
        ("PRIYA NAIR", "Priya Nair"),
        ("ANIL D'SOUZA", "Anil D'Souza"),
        ("R. K. SHARMA", "R. K. Sharma"),
        ("Priya McDonald", "Priya McDonald"),  # mixed case is left alone
        ("MRS. KAVITA DESAI", "Kavita Desai"),
        ("Errol Smith", "Errol Smith"),  # "Er" only as a whole honorific
        ("Mr. Dr. Anil Mehta", "Anil Mehta"),
    ],
)
def test_normalize_name(raw: str, expected: str) -> None:
    assert normalize_name(raw) == expected


@pytest.mark.parametrize(
    "text,ok",
    [
        ("Priya Nair", True),
        ("Contact Us", False),
        ("Acme Chemicals Pvt Ltd", False),
        ("Read More", False),
        ("R. K. Sharma", True),
        ("Purchase Dept", False),
        ("John 3:16", False),
    ],
)
def test_looks_like_person(text: str, ok: bool) -> None:
    assert looks_like_person_name(text) is ok


@pytest.mark.parametrize(
    "text,ok",
    [
        ("Mr. Rajesh K. Sharma", True),
        ("RAJESH KUMAR SHARMA", True),
        ("José Álvarez", True),
        ("Anil D'Souza", True),
        ("Ludwig van Beethoven", True),
        ("Madonna", False),  # one token
        ("R. K.", False),  # initials only
        ("Rajesh Kumar Sharma Verma Iyer", False),  # five tokens
        ("Managing Director", False),
        ("Head Office", False),
        ("Sodium Benzoate", False),
        ("Our Team", False),
        ("Home About Us", False),
        ("priya nair", False),
        ("Priya Nair @ Acme", False),
        ("Sales & Marketing", False),
        ("Privacy Policy", False),
        ("Monday Friday", False),
        ("Rajeshwari Venkataraman Subramaniam Iyengar", False),  # over 40 characters
        ("", False),
    ],
)
def test_looks_like_person_extras(text: str, ok: bool) -> None:
    assert looks_like_person_name(text) is ok


@pytest.mark.parametrize(
    "text",
    ["K.K. Nair", "S.R. Iyer", "V.P. Singh", "M.D. Rao", "A.G. Krishnan"],
)
def test_dotted_initials_that_look_like_stop_words_are_people(text: str) -> None:
    assert looks_like_person_name(text) is True


@pytest.mark.parametrize("text", ["Ph.D. Nair", "M.B.A. Rao", "Rao Ph.D.", "Rao M.B.A."])
def test_dotted_qualifications_stay_rejected(text: str) -> None:
    assert looks_like_person_name(text) is False
