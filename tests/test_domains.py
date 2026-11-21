import pytest

from salesfinder.domains import domain_of_email, normalize_domain


@pytest.mark.parametrize(
    "raw",
    [
        "acme.com",
        "WWW.Acme.com.",
        "https://shop.acme.com/x?y=1",
        "http://acme.com:8080",
        " acme.COM ",
    ],
)
def test_same_company(raw: str) -> None:
    assert normalize_domain(raw) == "acme.com"


def test_country_suffix() -> None:
    assert normalize_domain("https://www.sales.acme.co.in/") == "acme.co.in"


@pytest.mark.parametrize("raw", ["", "localhost", "192.168.1.4", "http://10.0.0.1/a", "not a host"])
def test_none(raw: str) -> None:
    assert normalize_domain(raw) is None


def test_email_domain() -> None:
    assert domain_of_email("Info@Mail.ACME.com") == "acme.com"
    assert domain_of_email("no-at-sign") is None


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        ("https://acme.myshopify.com/", "beta.myshopify.com", "acme.myshopify.com"),
        ("http://y.blogspot.com/2024/post", "other.blogspot.com", "y.blogspot.com"),
        ("https://w.github.io/site", "v.github.io", "w.github.io"),
        ("z.wixsite.com/shop", "q.wixsite.com", "z.wixsite.com"),
    ],
)
def test_free_website_hosts_are_distinct_companies(first: str, second: str, expected: str) -> None:
    # PSL private suffixes: two shops on one host are two companies, not one.
    assert normalize_domain(first) == expected
    assert normalize_domain(second) != expected
    assert domain_of_email(f"sales@{expected}") == expected


@pytest.mark.parametrize("bare_host", ["myshopify.com", "github.io", "blogspot.com"])
def test_bare_free_host_has_no_registrable_domain(bare_host: str) -> None:
    assert normalize_domain(bare_host) is None


def test_extra_private_suffixes_make_distinct_companies() -> None:
    assert normalize_domain("https://acme.business.site/x") == "acme.business.site"
    assert normalize_domain("https://beta.business.site") == "beta.business.site"
    assert normalize_domain("https://www.acme.business.site") == "acme.business.site"
    assert normalize_domain("https://www.acme.weebly.com") == "acme.weebly.com"
    assert normalize_domain("shop.godaddysites.com") == "shop.godaddysites.com"
    assert domain_of_email("a@acme.business.site") == "acme.business.site"
    assert normalize_domain("acme.com") == "acme.com"


@pytest.mark.parametrize(
    "raw",
    [
        "business.site",
        "www.business.site",
        "https://weebly.com/",
        "godaddysites.com",
        "jimdosite.com",
    ],
)
def test_bare_extra_private_suffix_has_no_registrable_domain(raw: str) -> None:
    assert normalize_domain(raw) is None
