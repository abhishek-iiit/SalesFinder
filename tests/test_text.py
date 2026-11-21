from salesfinder.signals.text import extract, snippet


def test_extract_strips_scripts_and_collapses():
    page = extract("<script>x</script><p>Hello   <b>world</b></p>", "https://a.com/")
    assert page.text == "Hello world"


def test_extract_title_links_and_hidden_content():
    html = (
        "<html><head><title> Acme  Chem </title><style>p{}</style></head><body>"
        "<noscript>nojs</noscript><svg><text>icon</text></svg><template>tpl</template>"
        "<a href='/about'>About   us</a><p>Houston<span>,</span> TX</p></body></html>"
    )
    page = extract(html, "https://a.com/")
    assert page.title == "Acme Chem"
    assert page.links == (("/about", "About us"),)
    assert "nojs" not in page.text and "icon" not in page.text and "tpl" not in page.text
    assert "Houston, TX" in page.text


def test_extract_empty():
    page = extract("", "https://a.com/")
    assert (page.title, page.text, page.links) == ("", "", ())


def test_snippet_window_and_ellipsis():
    text = " ".join(f"word{i}" for i in range(400))
    start = text.index("word200")
    out = snippet(text, start, start + 7)
    assert len(out) <= 300
    assert out.startswith("…") and out.endswith("…")
    assert "word200" in out
    assert not out[1:].startswith("ord")


def test_snippet_short_text_unchanged_and_edges():
    assert snippet("hello world", 0, 5) == "hello world"
    text = "x " * 400
    assert not snippet(text, 0, 1).startswith("…")
    assert snippet(text, 0, 1).endswith("…")
    assert len(snippet("a" * 1000, 10, 900)) <= 300
