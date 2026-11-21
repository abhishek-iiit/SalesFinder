"""Emails, people and the contact form, read from a company's own pages."""

from salesfinder.contacts.extract import (
    EmailFound,
    PersonFound,
    contact_form_url,
    extract_emails,
    extract_people,
)
from salesfinder.signals.text import extract


def _emails(html: str, url: str = "https://acme.com/contact", **kw: object) -> dict[str, str]:
    found = extract_emails([extract(html, url)], company_domain="acme.com", **kw)  # type: ignore[arg-type]
    return {e.address: e.kind for e in found}


# --- emails ---------------------------------------------------------------------------------------


def test_emails_obfuscated_and_mailto() -> None:
    html = """<html><body>
      <p>Write to sales [at] acme [dot] com for quotes.</p>
      <a href="mailto:Priya.Nair@ACME.com?subject=Hi">Priya</a>
      <img src="/img/logo@2x.png"> logo@2x.png
      <p>Template: a@example.com, other: x@other.com</p>
    </body></html>"""
    assert _emails(html) == {"sales@acme.com": "role", "priya.nair@acme.com": "personal"}


def test_email_evidence_fields() -> None:
    html = "<p>For purchase enquiries mail purchase@acme.com today.</p>"
    [found] = extract_emails([extract(html, "https://acme.com/contact")], company_domain="acme.com")
    assert found == EmailFound(
        address="purchase@acme.com",
        kind="role",
        url="https://acme.com/contact",
        snippet="For purchase enquiries mail purchase@acme.com today.",
    )


def test_obfuscation_variants() -> None:
    html = """<p>a: rajesh(at)acme(dot)com</p><p>b: anil AT acme DOT com</p>
      <p>c: info {at} acme {dot} com</p><p>d: kavita [AT] acme.com.</p>
      <p>e: we are at acme dot com</p>"""
    assert _emails(html) == {
        "rajesh@acme.com": "personal",
        "anil@acme.com": "personal",
        "info@acme.com": "role",
        "kavita@acme.com": "personal",
    }


def test_trailing_punctuation_subdomains_and_dedupe() -> None:
    html = """<p>Mail: export@acme.com, info@mail.acme.com; (sales@acme.com).
      Again EXPORT@acme.com: and <a href="mailto:%20export@acme.com">x</a></p>"""
    found = extract_emails([extract(html, "https://acme.com/about")], company_domain="acme.com")
    assert [e.address for e in found] == ["export@acme.com", "info@mail.acme.com", "sales@acme.com"]
    assert all(e.kind == "role" for e in found)


def test_rejects_assets_placeholders_and_trackers() -> None:
    html = """<p>icon@2x.webp bg@3x.jpg sprite@acme.png you@domain.com me@example.com
      abc123@sentry.io 605a7baede844d278b89dc95ae0a9123@sentry-next.wixpress.com
      not-an-email@ acme@ @acme.com a..b@acme.com</p>"""
    assert _emails(html) == {}


def test_company_domain_is_normalized() -> None:
    page = extract("<p>info@acme.co.in</p>", "https://www.acme.co.in/contact")
    found = extract_emails([page], company_domain="https://WWW.Acme.co.in/")
    assert [e.address for e in found] == ["info@acme.co.in"]


def test_free_mail_only_on_contact_pages() -> None:
    html = "<p>Email: acmechem@gmail.com</p>"
    assert _emails(html, "https://acme.com/contact-us") == {"acmechem@gmail.com": "role"}
    assert _emails(html, "https://acme.com/blog/post-1") == {}
    assert _emails(html, "https://acme.com/our-team") == {"acmechem@gmail.com": "role"}


def test_free_mail_matching_a_person_is_personal() -> None:
    person = PersonFound("Rajesh Sharma", "Proprietor", "owner_exec", "https://acme.com/about", "")
    html = "<p>rajesh.sharma1975@gmail.com or acmechem@yahoo.co.in</p>"
    assert _emails(html, "https://acme.com/about", people=[person]) == {
        "rajesh.sharma1975@gmail.com": "personal",
        "acmechem@yahoo.co.in": "role",
    }


def test_role_local_parts() -> None:
    html = """<p>info@acme.com sales.india@acme.com exports2@acme.com hr@acme.com
      customercare@acme.com rajesh@acme.com r.sharma@acme.com</p>"""
    assert _emails(html) == {
        "info@acme.com": "role",
        "sales.india@acme.com": "role",
        "exports2@acme.com": "role",
        "hr@acme.com": "role",
        "customercare@acme.com": "role",
        "rajesh@acme.com": "personal",
        "r.sharma@acme.com": "personal",
    }


def test_emails_odd_input_never_raises() -> None:
    pages = [extract("", "https://acme.com/"), extract("<<<@@@ [at] [dot]", "not a url")]
    assert extract_emails(pages, company_domain="acme.com") == []
    assert extract_emails(pages, company_domain="") == []


def test_email_snippets_are_bounded() -> None:
    html = "<p>" + "word " * 200 + "info@acme.com " + "word " * 200 + "</p>"
    [found] = extract_emails([extract(html, "https://acme.com/contact")], company_domain="acme.com")
    assert len(found.snippet) <= 300
    assert "info@acme.com" in found.snippet


# --- people ---------------------------------------------------------------------------------------


def _people(html: str, url: str = "https://acme.com/about-us") -> list[tuple[str, str | None, str]]:
    return [(p.full_name, p.title, p.category) for p in extract_people([(url, html)])]


PEOPLE_PAGE = """<html><head><title>About Us | Acme</title></head><body>
  <nav><a href="/">Home</a> | <a href="/about">About Us</a> | <a href="/products">Products</a></nav>
  <p>Home | About Us | Products</p>
  <p>Mr. Rajesh K. Sharma \u2013 Managing Director</p>
  <p>Priya Nair | Head of Procurement</p>
  <div class="member"><h3>Anil Mehta</h3><p>R&amp;D Manager</p></div>
  <p>Contact: Purchase Dept.</p>
</body></html>"""


def test_people_layouts() -> None:
    assert _people(PEOPLE_PAGE) == [
        ("Rajesh K. Sharma", "Managing Director", "owner_exec"),
        ("Priya Nair", "Head of Procurement", "procurement"),
        ("Anil Mehta", "R&D Manager", "rnd"),
    ]


def test_people_only_on_people_pages() -> None:
    page = PEOPLE_PAGE.replace("About Us | Acme", "Products | Acme")
    assert _people(page, "https://acme.com/products") == []
    # ...but a people title on an odd URL is enough
    assert len(_people(PEOPLE_PAGE, "https://acme.com/index.php")) == 3
    # and so is a team/leadership URL
    assert len(_people(page, "https://acme.com/leadership")) == 3
    assert len(_people(page, "https://acme.com/index.php?page=team")) == 3


def test_person_evidence() -> None:
    [first, *_] = extract_people([("https://acme.com/about", PEOPLE_PAGE)])
    assert first == PersonFound(
        full_name="Rajesh K. Sharma",
        title="Managing Director",
        category="owner_exec",
        url="https://acme.com/about",
        snippet="Mr. Rajesh K. Sharma - Managing Director",
    )


def test_realistic_indian_sme_lines() -> None:
    html = """<title>Contact Us</title><body>
      <p>Mr. Suresh Patel, Founder &amp; CEO</p>
      <p>Sr. Manager \u2013 Purchase: Kavita Desai</p>
      <p>Shri Ramesh Gupta (Proprietor)</p>
      <p>Contact Person: Mr. Vijay Kumar</p>
      <p>Head Office: New Delhi</p>
      <p>New Delhi | Head Office</p>
      <p>Phone: +91 98765 43210 | Email: info@acme.com</p>
    </body>"""
    assert _people(html, "https://acme.com/contact") == [
        ("Suresh Patel", "Founder & CEO", "owner_exec"),
        ("Kavita Desai", "Sr. Manager - Purchase", "procurement"),
        ("Ramesh Gupta", "Proprietor", "owner_exec"),
    ]


def test_dotted_initial_names_are_extracted() -> None:
    card = '<title>Team</title><div class="card"><h3>K.K. Nair</h3><p>Head of Procurement</p></div>'
    assert _people(card) == [("K.K. Nair", "Head of Procurement", "procurement")]
    line = "<title>Team</title><p>S.R. Iyer - Purchase Manager</p>"
    assert _people(line) == [("S.R. Iyer", "Purchase Manager", "procurement")]


def test_all_caps_and_strong_br_layouts() -> None:
    html = """<title>Leadership</title><body>
      <div class="person"><h4>RAJESH SHARMA</h4><span>FOUNDER &amp; CEO</span></div>
      <p><strong>Priya Nair</strong><br>Export Manager</p>
      <p><b>Anil Mehta</b> Supply Chain Head</p>
    </body>"""
    assert _people(html, "https://acme.com/leadership") == [
        ("Rajesh Sharma", "FOUNDER & CEO", "owner_exec"),
        ("Priya Nair", "Export Manager", "export"),
        ("Anil Mehta", "Supply Chain Head", "supply_chain"),
    ]


def test_card_with_extra_line_between_name_and_title() -> None:
    html = """<title>Our Team</title><div class="card"><img src="a.jpg" alt="Photo">
      <h3>Kavita Desai</h3><p class="qual">M.Sc, MBA</p><p>Head - Procurement</p></div>"""
    assert _people(html, "https://acme.com/team") == [
        ("Kavita Desai", "Head - Procurement", "procurement")
    ]


TEAM_GRID = """<html><head><title>Our Team - Acme Chemicals</title></head><body>
<header><nav><ul><li><a href="/">Home</a></li><li><a href="/about">About Us</a></li>
<li><a href="/team">Our Team</a></li><li><a href="/contact">Contact Us</a></li></ul></nav></header>
<section class="team">
  <h2>Meet Our Team</h2>
  <div class="grid">
    <div class="card"><h3>Rajesh Sharma</h3><p>Managing Director</p></div>
    <div class="card"><h3>Priya Nair</h3><p>Head of Procurement</p></div>
    <div class="card"><h3>Anil Mehta</h3><p>R&amp;D Manager</p></div>
    <div class="card"><h3>Kavita Desai</h3><p>Export Manager</p></div>
    <div class="card"><h3>Suresh Patel</h3><p>Marketing Head</p></div>
    <div class="card"><h3>Meena Iyer</h3><p>Accountant</p></div>
  </div>
</section>
<footer><p>Quick Links</p><ul><li>Home</li><li>About Us</li><li>Products</li></ul>
<p>Sodium Benzoate</p><p>Export Quality</p>
<p>&copy; 2024 Acme Chemicals Pvt Ltd. All Rights Reserved.</p>
<p>Designed by Web Solutions</p></footer>
</body></html>"""


def test_team_grid_of_six_cards() -> None:
    # The accountant's title categorizes as "other", which the card layout does not accept.
    assert _people(TEAM_GRID, "https://acme.com/team") == [
        ("Rajesh Sharma", "Managing Director", "owner_exec"),
        ("Priya Nair", "Head of Procurement", "procurement"),
        ("Anil Mehta", "R&D Manager", "rnd"),
        ("Kavita Desai", "Export Manager", "export"),
        ("Suresh Patel", "Marketing Head", "marketing"),
    ]


def test_people_deduped_across_pages_and_capped() -> None:
    cards = "".join(
        f"<div><h3>Person {chr(65 + i)}{chr(97 + i)}son</h3><p>Director</p></div>"
        for i in range(25)
    )
    pages = [
        ("https://acme.com/about", "<p>Rajesh Sharma - Director</p>"),
        ("https://acme.com/contact", "<p>MR. RAJESH SHARMA - DIRECTOR</p>"),
        ("https://acme.com/team", cards),
    ]
    found = extract_people(pages)
    assert len(found) == 20
    assert found[0].url == "https://acme.com/about"
    assert sum(p.full_name == "Rajesh Sharma" for p in found) == 1


def test_prose_and_navigation_are_not_people() -> None:
    html = """<title>About Us</title><body>
      <p>Founded in 1985 by visionary leaders, Acme Chemicals is a leading manufacturer.</p>
      <p>Our Vision | Our Mission | Board of Directors</p>
      <h3>Quality Policy</h3><p>Managing Director</p>
      <h3>Read More</h3><p>General Manager</p>
      <ul><li>Citric Acid</li><li>Export Manager</li></ul>
      <p>Terms of Use - Privacy Policy - Sitemap</p>
    </body>"""
    assert _people(html) == []


def test_people_odd_input_never_raises() -> None:
    assert extract_people([]) == []
    assert extract_people([("https://acme.com/about", "")]) == []
    assert extract_people([("not a url", "<<<>>>")]) == []
    deep = "<div>" * 5000 + "Rajesh Sharma - Director" + "</div>" * 5000
    assert _people(deep) == [("Rajesh Sharma", "Director", "owner_exec")]


def test_person_snippet_bounded() -> None:
    html = "<p>" + "x " * 400 + "</p><div><h3>Anil Mehta</h3><p>R&amp;D Manager</p></div>"
    [person] = extract_people([("https://acme.com/about", html)])
    assert len(person.snippet) <= 300
    assert "Anil Mehta" in person.snippet


# --- contact form ---------------------------------------------------------------------------------


def test_contact_form() -> None:
    home = '<form action="/search"><input type="text" name="q"></form>'
    newsletter = '<form><input type="email" name="e"></form>'
    enquiry = '<form><input name="name"><textarea name="msg"></textarea></form>'
    pages = [
        ("https://acme.com/", home + newsletter),  # homepage is not a contact page
        ("https://acme.com/about", enquiry),  # not a contact page either
        ("https://acme.com/contact-us", "<p>no form here</p>"),
        ("https://other.com/contact", enquiry),  # a different site
        ("https://www.acme.com/contact", enquiry),
        ("https://acme.com/reach-us", enquiry),
    ]
    assert contact_form_url(pages) == "https://www.acme.com/contact"


def test_contact_form_email_input_and_none() -> None:
    pages = [
        ("https://acme.com/", "<p>Home</p>"),
        ("https://acme.com/contact", '<form><input type="EMAIL" name="e"></form>'),
    ]
    assert contact_form_url(pages) == "https://acme.com/contact"
    search_only = '<form><input type="search"></form>'
    assert contact_form_url([("https://acme.com/contact", search_only)]) is None
    assert contact_form_url([]) is None
    assert contact_form_url([("::::", "<form><textarea></textarea>")]) is None


def test_self_review_false_positives() -> None:
    html = """<title>Our Team</title>
      <div class="testimonial"><p>"Great quality!"</p><h5>Rohit Verma</h5>
        <span>Purchase Manager</span></div>
      <div><h5>Amit Rao</h5><span>Purchase Manager, XYZ Pharma Ltd</span></div>
      <div class="team"><h3>DR. SUNITA RAO</h3><h6>PhD Chemistry</h6><p>Head - R&amp;D</p></div>
      <div><h3>John Deere</h3><p>Our Partner</p></div>
      <p>Gujarat Alkalies - Supplier Partner</p>
      <p>Sales Head - North India</p>
      <p>Andheri East | Head Office</p>"""
    assert _people(html, "https://acme.com/team") == [("Sunita Rao", "Head - R&D", "rnd")]


def test_us_leadership_cards_with_long_bios() -> None:
    bio = "She joined in 1998 and grew the business into a national supplier. " * 8
    html = f"""<title>Leadership Team | Acme Ingredients, Inc.</title>
      <div class="leader"><h2>Mary O'Neil</h2><h3>Vice President, Supply Chain</h3>
        <p>{bio}</p></div>
      <div class="leader"><h2>Robert Chen</h2><h3>Director of Purchasing</h3></div>"""
    assert _people(html, "https://acme.com/about/leadership") == [
        ("Mary O'Neil", "Vice President, Supply Chain", "supply_chain"),
        ("Robert Chen", "Director of Purchasing", "procurement"),
    ]


# --- fix round 1 ----------------------------------------------------------------------------------


def test_testimonial_attributions_are_not_people() -> None:
    probes = [
        "<p>— Sarah Lee, Purchasing Manager at OtherCo</p>",
        "<p>“Excellent quality.” - Sarah Lee, Purchasing Manager</p>",
        "<div class='client'><p>Excellent quality…</p><h4>Sarah Lee</h4>"
        "<span>Purchasing Manager</span></div>",
        "<p>Sarah Lee - Purchasing Manager at OtherCo</p>",
        "<p>Sarah Lee | Purchasing Manager @ OtherCo</p>",
        "<p>Sarah Lee, Purchasing Manager from OtherCo</p>",
        "<p>~ Sarah Lee, Purchasing Manager</p>",
        "<div id='what-clients-say'><h4>Sarah Lee</h4><p>Purchasing Manager</p></div>",
        "<div class='owl-carousel'><h4>Sarah Lee</h4><p>Purchasing Manager</p></div>",
        "<blockquote><h4>Sarah Lee</h4><p>Purchasing Manager</p></blockquote>",
        "<q>Great</q><cite><b>Sarah Lee</b><br>Purchasing Manager</cite>",
    ]
    for html in probes:
        assert _people(html, "https://acme.com/about") == [], html


def test_team_carousel_is_not_skipped() -> None:
    html = "<div class='team-carousel'><h4>Sarah Lee</h4><p>Purchasing Manager</p></div>"
    assert _people(html, "https://acme.com/team") == [
        ("Sarah Lee", "Purchasing Manager", "procurement")
    ]


def test_placeholder_names_are_not_people() -> None:
    html = "<p>John Doe \u2013 Director</p><p>Lorem Ipsum - Managing Director</p>"
    assert _people(html, "https://acme.com/team") == []


def test_free_mail_person_match_needs_both_names() -> None:
    anil = PersonFound("Anil Mehta", "Director", "owner_exec", "https://acme.com/about", "")
    rajesh = PersonFound("Rajesh Sharma", "MD", "owner_exec", "https://acme.com/about", "")
    html = """<p>vanillachem@gmail.com anil.mehta@gmail.com rsharma@yahoo.co.in
      rajeshsharma75@gmail.com anil1980@gmail.com mehtaanil@gmail.com</p>"""
    assert _emails(html, "https://acme.com/about", people=[anil, rajesh]) == {
        "vanillachem@gmail.com": "role",
        "anil.mehta@gmail.com": "personal",
        "rsharma@yahoo.co.in": "personal",
        "rajeshsharma75@gmail.com": "personal",
        "anil1980@gmail.com": "role",
        "mehtaanil@gmail.com": "personal",
    }


# Cloudflare: "info@acme.com" XOR 0x42, and "sales@acme.com" XOR 0x1f.
def _cf(address: str, key: int) -> str:
    return f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in address)


def test_cloudflare_href_form() -> None:
    html = (
        f'<p>Mail us: <a href="/cdn-cgi/l/email-protection#{_cf("info@acme.com", 0x42)}">'
        "[email&#160;protected]</a></p>"
        '<a href="/cdn-cgi/l/email-protection#zz12">bad</a>'
        '<a href="/cdn-cgi/l/email-protection#4">odd</a>'
        '<a href="/cdn-cgi/l/email-protection">no hash</a>'
    )
    found = extract_emails([extract(html, "https://acme.com/contact")], company_domain="acme.com")
    assert [(e.address, e.kind) for e in found] == [("info@acme.com", "role")]
    assert "info@acme.com" in found[0].snippet


def test_cloudflare_data_attribute_form() -> None:
    url = "https://acme.com/contact"
    html = (
        "<title>Contact Us</title><p>Sales: "
        f'<a class="__cf_email__" data-cfemail="{_cf("sales@acme.com", 0x1F)}" '
        'href="/cdn-cgi/l/email-protection">[email&#160;protected]</a></p>'
        '<span data-cfemail="nothex">x</span><span data-cfemail="">y</span>'
        f'<span data-cfemail="{_cf("acmechem@gmail.com", 0x33)}">z</span>'
    )
    page = extract(html, url)
    assert extract_emails([page], company_domain="acme.com") == []
    found = extract_emails([page], company_domain="acme.com", pages_html=[(url, html)])
    assert [(e.address, e.kind, e.url) for e in found] == [
        ("sales@acme.com", "role", url),
        ("acmechem@gmail.com", "role", url),
    ]
    assert "sales@acme.com" in found[0].snippet
    assert len(found[0].snippet) <= 300
    # raw HTML of a non-people page: the free-mail address is not kept
    blog = "https://acme.com/blog"
    found = extract_emails(
        [],
        company_domain="acme.com",
        pages_html=[(blog, html.replace("<title>Contact Us</title>", ""))],
    )
    assert [e.address for e in found] == ["sales@acme.com"]


# --- final review: other companies' people --------------------------------------------------------


def _own_people(
    html: str, url: str = "https://acme.com/about", *, name: str = "Acme Chemicals"
) -> list[tuple[str, str | None]]:
    found = extract_people([(url, html)], company_name=name, company_domain="acme.com")
    return [(p.full_name, p.title) for p in found]


def test_title_at_another_organisation_is_not_our_person() -> None:
    probes = [
        "<ul><li>Rakesh Gupta - Purchase Manager, Hindustan Unilever</li></ul>",
        "<p>“Great supplier.” Rakesh Gupta, Purchase Manager, Hindustan Unilever</p>",
        "<p>“Great supplier.”<br>Rakesh Gupta, Purchase Manager, Hindustan Unilever</p>",
        "<p>Great supplier, on time every time.</p>"
        "<p>Rakesh Gupta, Purchase Manager, Hindustan Unilever</p>",
        "<div><p>Rakesh Gupta</p><p>Purchase Manager, Hindustan Unilever</p></div>",
        "<p>Rakesh Gupta - Purchase Manager, Mumbai, Hindustan Unilever</p>",
        "<div><h4>Mary Jones</h4><p>Director, Brenntag USA</p></div>",
        "<p>Mary Jones — Director, Brenntag USA</p>",
        "<p>Purchase Manager, Hindustan Unilever</p>",
    ]
    for html in probes:
        assert _own_people(html) == [], html
        assert _people(html) == [], html  # without the company's name too


def test_partners_heading_section_is_skipped() -> None:
    html = """<title>About Us</title><body>
      <section><h2>Our Partners</h2>
        <div class="card"><h4>Mary Jones</h4><p>Director</p></div>
        <div class="card"><h4>Tom Brown</h4><p>Managing Director</p></div>
      </section>
      <section><h2>Leadership</h2>
        <div class="card"><h4>Priya Nair</h4><p>Head of Procurement</p></div>
      </section></body>"""
    assert _own_people(html) == [("Priya Nair", "Head of Procurement")]


def test_quote_headings() -> None:
    cases = {
        "What our clients say": True,
        "Trusted by": True,
        "Testimonials": True,
        "Our Customers": True,
        "Our Bankers": True,
        "Associates": True,
        "Our Team": False,
        "Meet the Team & Partners": False,
        "Managing Partner": False,
        "Board of Directors": False,
    }
    for heading, skipped in cases.items():
        html = (
            f"<title>About</title><h2>{heading}</h2><div><h4>Mary Jones</h4><p>Director</p></div>"
        )
        assert (_own_people(html) == []) is skipped, heading


def test_quote_heading_scope_ends_with_its_section_or_a_peer_heading() -> None:
    flat = """<title>About</title><h2>Clients</h2><p>Big logos here.</p>
      <h3>Pharma</h3><div><h4>Mary Jones</h4><p>Director</p></div>
      <h2>Our Directors</h2><div><h4>Priya Nair</h4><p>Director</p></div>"""
    assert _own_people(flat) == [("Priya Nair", "Director")]
    wrapped = """<title>About</title>
      <div class="elementor-section"><div class="title"><h2>Testimonials</h2></div>
        <div><h4>Mary Jones</h4><p>Director</p></div></div>
      <div class="elementor-section"><div><h4>Priya Nair</h4><p>Director</p></div></div>"""
    assert _own_people(wrapped) == [("Priya Nair", "Director")]


def test_company_named_like_a_quote_heading() -> None:
    html = """<title>About</title><h1>About Mehta &amp; Associates</h1>
      <div><h4>Priya Nair</h4><p>Director</p></div>"""
    found = extract_people(
        [("https://mehta.com/about", html)],
        company_name="Mehta & Associates",
        company_domain="mehta.com",
    )
    assert [p.full_name for p in found] == ["Priya Nair"]


def test_own_company_after_the_title_is_kept() -> None:
    assert _own_people("<p>Priya Nair, Head of Procurement, Acme Chemicals</p>") == [
        ("Priya Nair", "Head of Procurement")
    ]
    assert _own_people(
        "<div><h4>Priya Nair</h4><p>Head of Procurement, Acme Chemicals Pvt Ltd</p></div>"
    ) == [("Priya Nair", "Head of Procurement")]
    # the domain label is enough when the name differs ("AcmeChem" on acme.com)
    assert _own_people("<p>Anil Mehta - Director, AcmeChem India</p>", name="Acme Speciality") == [
        ("Anil Mehta", "Director")
    ]


def test_trailing_places_departments_and_people_are_not_organisations() -> None:
    html = """<title>Team</title>
      <p>Rajesh Sharma - Managing Director, Ankleshwar Plant</p>
      <p>Kavita Desai, Head - Exports (Europe)</p>
      <p>Anil Mehta - Director, Priya Nair - Purchase Manager</p>
      <p>Sunita Rao, Head - R&amp;D, Ph.D.</p>"""
    assert _own_people(html, "https://acme.com/team") == [
        ("Rajesh Sharma", "Managing Director"),
        ("Kavita Desai", "Head - Exports"),
        ("Anil Mehta", "Director"),
        ("Priya Nair", "Purchase Manager"),
        ("Sunita Rao", "Head - R&D"),
    ]


def test_role_and_never_send_locals() -> None:
    html = """<p>md@acme.com gm@acme.com ceo@acme.com director@acme.com webmaster@acme.com
      query@acme.com queries@acme.com service@acme.com team@acme.com corporate@acme.com
      trade@acme.com postmaster@acme.com noreply@acme.com no-reply@acme.com
      donotreply@acme.com abuse@acme.com mailer-daemon@acme.com bounce@acme.com
      newsletter@acme.com priya.nair@acme.com</p>"""
    found = _emails(html)
    assert found.pop("priya.nair@acme.com") == "personal"
    assert len(found) == 19 and set(found.values()) == {"role"}


def test_a_lone_name_does_not_take_the_next_persons_line_as_its_title() -> None:
    html = "<div><p>Rajesh Sharma</p><p>Priya Nair - Head of Procurement</p></div>"
    assert _own_people(html) == [("Priya Nair", "Head of Procurement")]
