"""Domain normalization: hosts, URLs and emails to a registrable domain (offline)."""

from urllib.parse import urlsplit

import tldextract

# Bundled public-suffix snapshot only: no HTTP fetches and no on-disk cache. The PSL's private
# section is included so free website hosts (acme.myshopify.com, y.blogspot.com, w.github.io)
# are each their own registrable domain instead of all collapsing to the host's domain.
_EXTRACT = tldextract.TLDExtract(
    suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True
)

# Free website hosts the bundled PSL snapshot lacks: each subdomain is its own company. (square.site
# and webflow.io are already private suffixes in the PSL, so they are not repeated here.)
EXTRA_PRIVATE_SUFFIXES = ("business.site", "godaddysites.com", "jimdosite.com", "weebly.com")

# Consumer mailbox providers: an address there says nothing about the sender's company, so a
# HubSpot contact at one of these never excludes the whole domain. The single source of truth
# for both the exclusion check and the HubSpot sync.
FREE_MAIL_DOMAINS = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "yahoo.co.in",
        "yahoo.in",
        "yahoo.co.uk",
        "ymail.com",
        "rocketmail.com",
        "outlook.com",
        "hotmail.com",
        "hotmail.co.uk",
        "hotmail.in",
        "live.com",
        "live.in",
        "msn.com",
        "icloud.com",
        "me.com",
        "mac.com",
        "aol.com",
        "proton.me",
        "protonmail.com",
        "rediffmail.com",
        "zoho.com",
        "zohomail.com",
        "gmx.com",
        "gmx.net",
        "mail.com",
        "yandex.com",
        "comcast.net",
        "att.net",
        "sbcglobal.net",
        "verizon.net",
        "cox.net",
        "bellsouth.net",
        "earthlink.net",
        "qq.com",
        "163.com",
        "126.com",
        "sify.com",
        "vsnl.net",
        "indiatimes.com",
    }
)


def host_of(raw: str) -> str | None:
    """Lowercased hostname of a host, URL or bare domain (no port, no trailing dot)."""
    text = raw.strip()
    if not text or any(ch.isspace() for ch in text):
        return None
    if "://" not in text:
        text = "//" + text
    try:
        host = urlsplit(text).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower().rstrip(".")
    return host or None


def split_domain(raw: str) -> tldextract.tldextract.ExtractResult | None:
    """Extract result for a host or URL, or None for IPs, localhost and unknown suffixes."""
    host = host_of(raw)
    if host is None:
        return None
    ext = _EXTRACT(host)
    if not ext.domain or not ext.suffix:
        return None
    return ext


def normalize_domain(raw: str) -> str | None:
    """Registrable domain of a host, URL or bare domain; None if there isn't one."""
    host = host_of(raw)
    if host is None:
        return None
    for suffix in EXTRA_PRIVATE_SUFFIXES:
        if host == suffix:
            return None
        if host.endswith("." + suffix):
            label = host.removesuffix("." + suffix).rsplit(".", 1)[-1]
            return None if label == "www" else f"{label}.{suffix}"
    ext = split_domain(host)
    if ext is None:
        return None
    return f"{ext.domain}.{ext.suffix}"


def domain_of_url(url: str) -> str | None:
    return normalize_domain(url)


def domain_of_email(email: str) -> str | None:
    _, at, tail = email.rpartition("@")
    if not at:
        return None
    return normalize_domain(tail)
