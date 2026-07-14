"""Recover a paper/post's title (and blurb) from an HTML landing page.

Many entries link an HTML page rather than a PDF or arXiv abstract — an Anthropic
research post, a Transformer Circuits article, a lab blog. With no resolver for
those, the entry kept whatever title it was born with: the raw URL, or a link's
anchor text ("announced", "watched", "idea"). This reads the page's own metadata
— Open Graph tags first, then the <title> element — the same signals a link
preview uses, and only from <head>, never the body.
"""

from __future__ import annotations

import logging
from html.parser import HTMLParser
from typing import Callable

from paper_watch.http import get_text
from paper_watch.identity import strip_site_suffix

log = logging.getLogger(__name__)

_MIN_TITLE_CHARS = 4
# Bare site/navigation labels a page may put in <title>; never a paper title.
_NON_TITLES = frozenset({"home", "index", "untitled", "loading", "404", "not found"})


class _MetaCollector(HTMLParser):
    """Pulls the title/description signals out of <head> and stops at <body>.

    Only <head> carries metadata; parsing stops when the body opens so a stray
    <title> or heading further down the document can't leak in.
    """

    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, str] = {}  # og:title, twitter:title, description...
        self.title: str | None = None
        self._in_title = False
        self._title_parts: list[str] = []
        self._done = False

    def handle_starttag(self, tag, attrs):
        if self._done:
            return
        if tag == "body":
            self._done = True
        elif tag == "title" and self.title is None:
            self._in_title = True
            self._title_parts = []
        elif tag == "meta":
            a = dict(attrs)
            key = (a.get("property") or a.get("name") or "").strip().lower()
            content = (a.get("content") or "").strip()
            if key and content and key not in self.meta:
                self.meta[key] = content

    def handle_data(self, data):
        if self._in_title:
            self._title_parts.append(data)

    def handle_endtag(self, tag):
        if tag == "title" and self._in_title:
            self.title = " ".join("".join(self._title_parts).split())
            self._in_title = False
        elif tag == "head":
            self._done = True


def _clean(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = strip_site_suffix(" ".join(value.split())).strip()
    if len(cleaned) < _MIN_TITLE_CHARS or cleaned.lower() in _NON_TITLES:
        return None
    if not any(c.isalpha() for c in cleaned):
        return None
    # archive.is and some CDNs echo the source URL into og:title. A URL is not a
    # title; accepting it just swaps one junk title for another.
    if cleaned.startswith(("http://", "https://")):
        return None
    return cleaned


def parse_html_meta(html: str) -> dict | None:
    """{title, abstract} from a page's head metadata, or None without a title.

    Title precedence: og:title, twitter:title, then <title> (site suffix
    stripped). Abstract: og:description, then the meta description.
    """
    p = _MetaCollector()
    try:
        p.feed(html)
    except Exception as exc:  # malformed markup — keep whatever parsed first
        log.debug("html meta parse failed: %s", exc)
    meta = p.meta
    title = (
        _clean(meta.get("og:title"))
        or _clean(meta.get("twitter:title"))
        or _clean(p.title)
    )
    if title is None:
        return None
    abstract = _clean(meta.get("og:description")) or _clean(meta.get("description"))
    return {"title": title, "abstract": abstract or None}


class HtmlMetaResolver:
    def __init__(self, fetch: Callable[[str], str] = get_text):
        self._fetch = fetch

    def resolve(self, url: str) -> dict | None:
        """{title, abstract} for an HTML page URL, or None. Never raises."""
        try:
            html = self._fetch(url)
        except Exception as exc:
            log.debug("HTML fetch failed for %s: %s", url, exc)
            return None
        return parse_html_meta(html)
