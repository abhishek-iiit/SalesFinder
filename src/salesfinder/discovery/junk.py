"""Drop obviously useless search results (marketplaces, directories, social, ...)."""

from collections.abc import Sequence
from urllib.parse import urlsplit

from salesfinder.domains import host_of, normalize_domain, split_domain

MARKETPLACE_DOMAINS = (
    "indiamart.com",
    "tradeindia.com",
    "alibaba.com",
    "made-in-china.com",
    "exportersindia.com",
    "globalsources.com",
    "ec21.com",
    "go4worldbusiness.com",
)
MARKETPLACE_NAMES = ("amazon", "ebay")  # match any public suffix
DIRECTORY_DOMAINS = (
    "thomasnet.com",
    "yelp.com",
    "yellowpages.com",
    "manta.com",
    "bbb.org",
    "zoominfo.com",
    "dnb.com",
    "crunchbase.com",
    "kompass.com",
    "justdial.com",
    "chemicalbook.com",
    "echemi.com",
    "lookchem.com",
    "guidechem.com",
)
SOCIAL_DOMAINS = (
    "linkedin.com",
    "facebook.com",
    "instagram.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "pinterest.com",
    "tiktok.com",
    "reddit.com",
)
REFERENCE_DOMAINS = (
    "wikipedia.org",
    "sciencedirect.com",
    "researchgate.net",
    "reuters.com",
    "prnewswire.com",
    "businesswire.com",
    "globenewswire.com",
)
REFERENCE_HOSTS = ("pubchem.ncbi.nlm.nih.gov",)
NEWS_MARKER = "news"
GOV_EDU_SUFFIXES = ("gov", "mil", "edu")
GOV_EDU_SECOND_LEVELS = ("gov", "gc", "ac", "edu")
GOV_EDU_SUFFIX_TAILS = ("nic.in",)
DOCUMENT_EXTENSIONS = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt")


def _path_of(url: str) -> str:
    text = url.strip()
    if "://" not in text:
        text = "//" + text
    try:
        return urlsplit(text).path.lower()
    except ValueError:
        return ""


def _is_news_label(label: str) -> bool:
    """True for a 'news' hyphen token, or (unhyphenated label) a news prefix/suffix."""
    if "-" in label:
        return NEWS_MARKER in label.split("-")
    return label.startswith(NEWS_MARKER) or label.endswith(NEWS_MARKER)


def junk_reason(url: str, extra_domains: Sequence[str] = ()) -> str | None:
    """Why a search result should be dropped, or None to keep it."""
    ext = split_domain(url)
    if ext is None:
        return "no_domain"
    registrable = f"{ext.domain}.{ext.suffix}"
    host = host_of(url) or registrable

    if registrable in MARKETPLACE_DOMAINS or ext.domain in MARKETPLACE_NAMES:
        return "marketplace"
    if registrable in DIRECTORY_DOMAINS:
        return "directory"
    if registrable in SOCIAL_DOMAINS:
        return "social"
    if (
        registrable in REFERENCE_DOMAINS
        or any(host == h or host.endswith("." + h) for h in REFERENCE_HOSTS)
        or _is_news_label(ext.domain)
    ):
        return "reference_or_news"
    suffix_labels = ext.suffix.split(".")
    if (
        ext.suffix in GOV_EDU_SUFFIXES
        or (len(suffix_labels) > 1 and suffix_labels[0] in GOV_EDU_SECOND_LEVELS)
        or any(ext.suffix == t or ext.suffix.endswith("." + t) for t in GOV_EDU_SUFFIX_TAILS)
    ):
        return "government_or_education"
    if _path_of(url).endswith(DOCUMENT_EXTENSIONS):
        return "document"
    if registrable in {normalize_domain(d) for d in extra_domains}:
        return "configured"
    return None
