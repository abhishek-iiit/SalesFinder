import pytest

from salesfinder.discovery.junk import junk_reason


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://dir.indiamart.com/x", "marketplace"),
        ("https://www.amazon.in/dp/1", "marketplace"),
        ("https://www.thomasnet.com/a", "directory"),
        ("https://in.linkedin.com/company/a", "social"),
        ("https://en.wikipedia.org/wiki/SLES", "reference_or_news"),
        ("https://pubchem.ncbi.nlm.nih.gov/compound/1", "reference_or_news"),
        ("https://chemnews.com/a", "reference_or_news"),
        ("https://www.epa.gov/x", "government_or_education"),
        ("https://www.iitb.ac.in/", "government_or_education"),
        ("https://dept.nic.in/", "government_or_education"),
        ("https://www.nic.in/", "government_or_education"),
        ("https://www.canada.gc.ca/", "government_or_education"),
        ("https://news-chem.com/", "reference_or_news"),
        ("https://chem-news.com/", "reference_or_news"),
        ("https://newsmax.com/", "reference_or_news"),
        ("https://chem-news-daily.com/", "reference_or_news"),
        ("https://acme.com/spec-sheet.PDF", "document"),
        ("http://127.0.0.1/", "no_domain"),
    ],
)
def test_junk(url: str, reason: str) -> None:
    assert junk_reason(url) == reason


@pytest.mark.parametrize(
    "url",
    [
        "https://www.acmecosmetics.com/products",
        "https://renewsol.com/",
        "https://renews-solutions.com/",
        "https://bio-renews.com/",
        "https://acme.news/",
    ],
)
def test_real_company_kept(url: str) -> None:
    assert junk_reason(url) is None


def test_configured_extra() -> None:
    assert junk_reason("https://shop.badsite.com", extra_domains=["badsite.com"]) == "configured"
