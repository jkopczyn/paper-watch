"""Shared HTML anchor extraction for sources that read raw pages."""

from __future__ import annotations

from html.parser import HTMLParser


class _LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []  # (href, anchor text)
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self._href = href
                self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None
            self._text = []


def collect_links(html: str) -> list[tuple[str, str]]:
    """All (href, whitespace-normalized anchor text) pairs in `html`, in order.

    Malformed HTML yields whatever parsed before the error rather than raising.
    """
    parser = _LinkCollector()
    try:
        parser.feed(html)
    except Exception:
        pass
    return parser.links
