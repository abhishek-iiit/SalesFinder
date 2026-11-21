"""pick_pages: which same-site homepage links are worth fetching, in priority order."""

from salesfinder.profiling.pages import pick_pages

HOME = """
<html><body>
  <nav>
    <a href="/blog">Blog</a>
    <a href="/export">Export</a>
    <a href="/quality-policy">Quality</a>
    <a href="/careers">Careers</a>
    <a href="/contact-us">Contact</a>
    <a href="/products">Our range</a>
    <a href="/who">About <b>Us</b></a>
    <a href="https://other.com/about">Partner</a>
  </nav>
</body></html>
"""


def test_picks_one_per_group_in_priority_order() -> None:
    pages = pick_pages(HOME, "https://acme.com/", limit=6)
    assert pages == [
        "https://acme.com/who",  # matched by anchor text "About Us"
        "https://acme.com/products",
        "https://acme.com/contact-us",
        "https://acme.com/careers",
        "https://acme.com/quality-policy",
    ]

    assert pick_pages(HOME, "https://acme.com/", limit=8) == [*pages, "https://acme.com/export"]
    assert pick_pages(HOME, "https://acme.com/", limit=3) == pages[:2]
    assert pick_pages(HOME, "https://acme.com/", limit=1) == []


def test_one_url_per_group_first_in_document_order() -> None:
    home = """
      <a href="/about">About</a>
      <a href="/company/history">Company history</a>
      <a href="/about">About (again)</a>
      <a href="/catalogue">Catalogue</a>
      <a href="/products/solvents">Solvents</a>
    """
    assert pick_pages(home, "https://acme.com/", limit=20) == [
        "https://acme.com/about",
        "https://acme.com/catalogue",
    ]


def test_link_matching_two_groups_is_used_once() -> None:
    home = '<a href="/about/products">Products</a><a href="/company">Company</a>'
    assert pick_pages(home, "https://acme.com/", limit=20) == ["https://acme.com/about/products"]


def test_anchor_text_keywords_with_spaces() -> None:
    home = '<a href="/p1">Who we are</a><a href="/p2">Reach us</a><a href="/p3">Join us</a>'
    assert pick_pages(home, "https://acme.com/", limit=20) == [
        "https://acme.com/p1",
        "https://acme.com/p2",
        "https://acme.com/p3",
    ]


def test_skips_files_fragments_queries_and_home() -> None:
    home = """
      <a href="/">Home - about the company</a>
      <a href="https://acme.com/#about">About (fragment of home)</a>
      <a href="/?page=about">About (query on home)</a>
      <a href="/about-us#team">About</a>
      <a href="/about-us?ref=nav">About again</a>
      <a href="/products/catalog.pdf">Product catalog (PDF)</a>
      <a href="/products/brochure.DOCX">Products brochure</a>
      <a href="/img/product.jpg">Product photo</a>
      <a href="/files/products.xlsx">Price list products</a>
      <a href="/downloads/products.zip">Products archive</a>
      <a href="/products/">Products</a>
      <a href="mailto:contact@acme.com">Contact</a>
      <a href="javascript:void(0)">Contact form</a>
      <a href="tel:+15550100">Contact by phone</a>
      <a href="ftp://acme.com/contact">Contact FTP</a>
      <a>Careers (no href)</a>
    """
    assert pick_pages(home, "https://acme.com/", limit=20) == [
        "https://acme.com/about-us",
        "https://acme.com/products/",
    ]


def test_relative_and_subdomain_links() -> None:
    home = """
      <a href="about.html">About</a>
      <a href="https://www.acme.com/products">Products</a>
      <a href="https://shop.other.com/contact">Contact</a>
      <a href="//careers.acme.com/jobs">Jobs</a>
      <a href="https://acme.com.evil.io/quality">Quality</a>
    """
    assert pick_pages(home, "https://acme.com/en/index.html", limit=20) == [
        "https://acme.com/en/about.html",
        "https://www.acme.com/products",
        "https://careers.acme.com/jobs",
    ]


def test_homepage_on_www_is_excluded_and_hosts_lowercased() -> None:
    home = '<a href="https://WWW.Acme.com/">About us</a><a href="https://WWW.Acme.com/About">x</a>'
    assert pick_pages(home, "https://acme.com/", limit=20) == ["https://www.acme.com/About"]


def test_garbage_html_and_bad_links_do_not_raise() -> None:
    home = '<a href="http://[::1">About</a><a href="https://acme.com:99999/about">About</a><<<'
    assert pick_pages(home, "https://acme.com/", limit=20) == []
    assert pick_pages("", "https://acme.com/", limit=20) == []


def test_userinfo_is_stripped_from_links() -> None:
    home = """
      <a href="https://user:pw@acme.com/about">About</a>
      <a href="https://u:p@WWW.acme.com:8443/contact">Contact</a>
    """
    assert pick_pages(home, "https://acme.com/", limit=20) == [
        "https://acme.com/about",
        "https://www.acme.com:8443/contact",
    ]


def test_team_and_leadership_groups() -> None:
    from salesfinder.profiling.pages import PAGE_GROUPS

    names = [name for name, _ in PAGE_GROUPS]
    assert names == [
        "about",
        "products",
        "contact",
        "team",
        "leadership",
        "careers",
        "quality",
        "export",
    ]
    home = """
      <a href="/careers">Careers</a>
      <a href="/board-of-directors">Board</a>
      <a href="/our-team">Meet the team</a>
      <a href="/contact">Contact</a>
    """
    # "/board-of-directors" is the first team-group link in document order; one URL per group.
    assert pick_pages(home, "https://acme.com/", limit=20) == [
        "https://acme.com/contact",
        "https://acme.com/board-of-directors",
        "https://acme.com/careers",
    ]
    home = '<a href="/m">Management</a><a href="/founders-message">Message</a>'
    assert pick_pages(home, "https://acme.com/", limit=20) == ["https://acme.com/m"]
    home = '<a href="/founders-message">Message</a>'
    assert pick_pages(home, "https://acme.com/", limit=20) == ["https://acme.com/founders-message"]


def test_word_boundary_keywords() -> None:
    home = """
      <a href="/orange-juice">Juice</a>
      <a href="/peoples-choice">Awards</a>
      <a href="/dashboard">Login</a>
      <a href="/stafford-branch">Stafford</a>
      <a href="/contacts.html">x</a>
      <a href="/our-range">Range</a>
      <a href="/peoples">x</a>
      <a href="/people">x</a>
    """
    assert pick_pages(home, "https://acme.com/", limit=20) == [
        "https://acme.com/our-range",
        "https://acme.com/contacts.html",
        "https://acme.com/people",
    ]


def test_concatenated_and_stem_keywords() -> None:
    home = """
      <a href="/aboutus.php">x</a>
      <a href="/contactus.html">x</a>
      <a href="/product_list.asp">x</a>
      <a href="/current-vacancies">x</a>
      <a href="/certifications">x</a>
      <a href="/exporters">x</a>
      <a href="/x1">Who We Are</a>
    """
    assert pick_pages(home, "https://acme.com/", limit=20) == [
        "https://acme.com/aboutus.php",
        "https://acme.com/product_list.asp",
        "https://acme.com/contactus.html",
        "https://acme.com/current-vacancies",
        "https://acme.com/certifications",
        "https://acme.com/exporters",
    ]


def test_query_routed_pages_keep_their_route() -> None:
    home = """
      <a href="/index.php?page=about&amp;utm_source=nav">About</a>
      <a href="index.php?page=products">Our products</a>
      <a href="/index.php?ref=home&amp;page=contact#form">Reach</a>
      <a href="/default.aspx?p=team">x</a>
      <a href="/products?sort=asc">Products again</a>
    """
    assert pick_pages(home, "https://acme.com/index.php", limit=20) == [
        "https://acme.com/index.php?page=about",
        "https://acme.com/index.php?page=products",
        "https://acme.com/index.php?page=contact",
        "https://acme.com/default.aspx?p=team",
    ]


def test_script_queries_are_kept_whole_minus_tracking() -> None:
    home = """
      <a href="/index.php?option=com_content&amp;view=article&amp;id=5&amp;utm_medium=x">About</a>
      <a href="/index.php?page_id=12&amp;PHPSESSID=abc&amp;fbclid=f&amp;gclid=g">Contact</a>
      <a href="/index.php?id=5&amp;view=article&amp;option=com_content&amp;Ref=nav">Again</a>
      <a href="/index.php?utm_source=nav&amp;sid=1&amp;SessionId=2">Products</a>
    """
    assert pick_pages(home, "https://acme.com/", limit=20) == [
        "https://acme.com/index.php?id=5&option=com_content&view=article",
        "https://acme.com/index.php",
        "https://acme.com/index.php?page_id=12",
    ]


def test_joined_team_and_quality_keywords() -> None:
    home = """
      <a href="/qualitypolicy.html">x</a>
      <a href="/managementteam.php">x</a>
      <a href="/ourteam.html">x</a>
      <a href="/x">Meet the Team</a>
    """
    assert pick_pages(home, "https://acme.com/", limit=20) == [
        "https://acme.com/ourteam.html",
        "https://acme.com/managementteam.php",
        "https://acme.com/qualitypolicy.html",
    ]
    assert pick_pages('<a href="/meettheteam">x</a>', "https://acme.com/", limit=20) == [
        "https://acme.com/meettheteam"
    ]
