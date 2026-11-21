"""Visible-text extraction from HTML and evidence snippets (pure, no I/O)."""

import re
from dataclasses import dataclass

from selectolax.lexbor import LexborHTMLParser

_HIDDEN = "script, style, noscript, svg, template"
_DASHES = dict.fromkeys([*range(0x2010, 0x2016), 0x2212], "-")
_WS = re.compile(r"\s+")
_SPACE_BEFORE_PUNCT = re.compile(r" +([,.;:])")


@dataclass(frozen=True)
class PageText:
    url: str
    title: str
    text: str
    links: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Evidence:
    url: str
    snippet: str


def _collapse(value: str) -> str:
    return _WS.sub(" ", value).strip()


def extract(html: str, url: str) -> PageText:
    """Visible text (scripts/styles/noscript/svg/template removed, whitespace collapsed), title
    and (href, anchor text) links."""
    if not html.strip():
        return PageText(url=url, title="", text="", links=())
    tree = LexborHTMLParser(html)
    title_node = tree.css_first("title")
    title = _collapse(title_node.text()) if title_node is not None else ""
    for node in tree.css(_HIDDEN):
        node.decompose()
    links = tuple(
        (href, _collapse(a.text()))
        for a in tree.css("a[href]")
        if (href := (a.attributes.get("href") or "").strip())
    )
    root = tree.body or tree.root
    raw = root.text(separator=" ") if root is not None else ""
    text = _SPACE_BEFORE_PUNCT.sub(r"\1", _collapse(raw)).translate(_DASHES)
    return PageText(url=url, title=title, text=text, links=links)


def snippet(text: str, start: int, end: int, *, width: int = 300) -> str:
    """A window of ``text`` around [start, end), word-trimmed, at most ``width`` characters, with
    an ellipsis on each cut side."""
    if len(text) <= width:
        return text.strip()
    budget = width - 2  # room for an ellipsis on both sides
    extra = max(budget - (end - start), 0)
    left = max(0, min(start - extra // 2, len(text) - budget))
    right = min(len(text), left + budget)
    if right - left < budget:
        left = max(0, right - budget)
    if left > 0 and text[left - 1] != " ":
        idx = text.find(" ", left, start)
        if idx != -1:
            left = idx + 1
    if right < len(text) and text[right] != " ":
        idx = text.rfind(" ", max(end, left), right)
        if idx != -1:
            right = idx
    body = text[left:right].strip()
    return ("…" if left > 0 else "") + body + ("…" if right < len(text) else "")
