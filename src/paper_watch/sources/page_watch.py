"""Watch blog index pages that have no RSS feed, by diffing their link sets.

Some paper blogs (alignment.anthropic.com, transformer-circuits.pub) publish
by prepending a link to an index page. Each run fetches the page, extracts its
outgoing links, and diffs them against the set recorded in the store: links
never seen before are new posts. The first fetch of a page seeds that baseline
and yields nothing — otherwise every historical post would flood the digest.

The seen-link set lives in the store's `source_state` cursor as a JSON array,
keyed by the page URL (not its config name, so renames don't re-seed). Links
that later disappear from the index stay in the set, so a post being pushed
off the front page never re-triggers as "new".
"""

from __future__ import annotations

import json
import logging
from typing import Iterable, Iterator, Protocol
from urllib.parse import urldefrag, urljoin

from paper_watch.http import get_text
from paper_watch.models import RawItem
from paper_watch.sources import Fetcher
from paper_watch.sources.html_links import collect_links

log = logging.getLogger(__name__)

_MAX_TEXT_CHARS = 500  # index blurbs run ~100-200 chars; keep them whole


class PageState(Protocol):
    """The slice of Store this source needs (per-source cursor persistence)."""

    def get_cursor(self, source: str) -> str | None: ...

    def set_cursor(self, source: str, cursor: str) -> None: ...


def extract_post_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Candidate post links on an index page: (absolute url, anchor text).

    Keeps every http(s) link with non-empty anchor text — post entries on these
    pages sometimes point off-site (arXiv, anthropic.com/research), so no
    same-host restriction. Nav/footer noise is harmless: it lands in the seeded
    baseline and never diffs as new, and the LLM relevance gate catches the rare
    later addition. Fragments are stripped; the page itself and empty-text icon
    links are dropped; order is preserved and duplicates collapse.
    """
    page_url = urldefrag(urljoin(base_url, ""))[0]
    seen: set[str] = set()
    links: list[tuple[str, str]] = []
    for href, anchor in collect_links(html):
        if not anchor:
            continue
        url = urldefrag(urljoin(base_url, href))[0]
        if not url.startswith(("http://", "https://")):
            continue
        if url.rstrip("/") == page_url.rstrip("/") or url in seen:
            continue
        seen.add(url)
        links.append((url, anchor))
    return links


class PageWatchSource:
    name = "page"

    def __init__(self, pages: Iterable, state: PageState, fetch: Fetcher = get_text):
        # `pages` items need `.name`, `.url`, `.trusted` (e.g. config.PageConfig).
        self.pages = list(pages)
        self._state = state
        self._fetch = fetch

    def fetch(self, since: str | None = None) -> Iterator[RawItem]:
        # `since` is ignored: novelty comes from the link diff, not timestamps
        # (the index gives none), so items are never re-fetched into the window.
        for page in self.pages:
            try:
                html = self._fetch(page.url)
            except Exception as exc:  # one bad page must not abort the rest
                log.warning("watched page failed: %s (%s)", page.url, exc)
                continue
            links = extract_post_links(html, page.url)
            if not links:
                # An empty/unparseable page is more likely an outage than a
                # site wipe; bail rather than seed (or diff against) nothing.
                log.warning("watched page had no links, skipping: %s", page.url)
                continue
            yield from self._diff_page(page, links)

    def _diff_page(self, page, links: list[tuple[str, str]]) -> Iterator[RawItem]:
        key = f"page:{page.url}"
        cursor = self._state.get_cursor(key)
        known: set[str] = set(json.loads(cursor)) if cursor else set()
        if cursor is None:
            log.info(
                "seeding baseline for %s (%d links, none yielded)",
                page.url,
                len(links),
            )
        else:
            for url, anchor in links:
                if url in known:
                    continue
                yield RawItem(
                    source=f"page:{page.name}",
                    url=url,
                    # Index anchors read "Title Blurb…" with no separator, so
                    # no clean title; normalize promotes the text's first line.
                    title=None,
                    text=anchor[:_MAX_TEXT_CHARS],
                    trusted=page.trusted,
                )
        merged = known | {url for url, _ in links}
        if cursor is None or merged != known:
            self._state.set_cursor(key, json.dumps(sorted(merged)))
