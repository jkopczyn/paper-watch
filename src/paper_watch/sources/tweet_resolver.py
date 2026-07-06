"""Resolve a tweet link to the paper it points at, via the local Nitter instance.

A tweet enters the pipeline as a bare status URL (a Slack message "Tweet: x.com/…",
a quote-tweet) whose paper id lives in the tweet *text* — or, worse, in a later
tweet of the author's own thread — which the id extractor never sees. This fetches
the tweet page from the local Nitter instance, pulls its text / expanded links /
quoted-tweet / next self-thread reply, and hands them back so id extraction can run
over real content. Local instance only; zero LLM; results SQLite-cached.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable

import httpx

from paper_watch.http import get_text
from paper_watch.identity import canonicalize_url, extract_arxiv_id, extract_doi
from paper_watch.models import RawItem
from paper_watch.store import Store

log = logging.getLogger(__name__)

# Canonical form only — ingest already ran canonicalize_url, which collapses every
# Nitter/x.com variant to https://twitter.com/<user>/status/<id>.
_TWEET_URL = re.compile(r"^https?://twitter\.com/([A-Za-z0-9_]{1,15})/status/(\d+)")

_CONTENT = re.compile(r'<div class="tweet-content[^"]*"[^>]*>(.*?)</div>', re.DOTALL)
_HREF = re.compile(r'href="(https?://[^"]+)"')
_QUOTE = re.compile(r'class="quote-link"\s+href="(/[^"]+/status/\d+[^"]*)"')
_TWEET_LINK = re.compile(r'<a class="tweet-link"[^>]*href="(/[^"]+/status/\d+[^"]*)"')
_OG_DESC = re.compile(r'<meta property="og:description" content="([^"]*)"')
_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def is_tweet_url(url: str | None) -> tuple[str, str] | None:
    """(user, id) for a canonical tweet URL, else None."""
    if not url:
        return None
    m = _TWEET_URL.match(url)
    return (m.group(1), m.group(2)) if m else None


@dataclass
class TweetResolution:
    text: str | None
    links: list[str]
    quoted_url: str | None
    reply_url: str | None  # next tweet in the author's own thread, if any


def _clean(fragment: str) -> str | None:
    text = _WS.sub(" ", html.unescape(_TAG.sub(" ", fragment))).strip()
    return text or None


def _abs_tweet(path: str) -> str | None:
    """A Nitter-relative `/user/status/id#m` href → canonical twitter.com URL."""
    return canonicalize_url("https://twitter.com" + path)


def parse_tweet_html(html_text: str | None) -> TweetResolution | None:
    """Pull text / links / quoted-tweet / next-thread-reply from a Nitter status page.

    Scoped to the main tweet (up to the replies section) so reply *content* isn't
    mistaken for the tweet's own; the one thing taken from the replies section is
    the first self-thread continuation link, followed (same-author only) by the
    resolver. Best-effort: returns None only when nothing parses.
    """
    if not html_text:
        return None
    idx = html_text.find('class="main-tweet"')
    if idx != -1:
        rep = html_text.find('class="replies"', idx)
        main_block = html_text[idx : rep if rep != -1 else len(html_text)]
        replies_block = html_text[rep:] if rep != -1 else ""
    else:
        main_block, replies_block = html_text, ""

    text: str | None = None
    links: list[str] = []
    cm = _CONTENT.search(main_block)
    if cm:
        links = _HREF.findall(cm.group(1))
        text = _clean(cm.group(1))
    if text is None:
        og = _OG_DESC.search(html_text)
        if og:
            text = _clean(og.group(1))

    qm = _QUOTE.search(main_block)
    quoted_url = _abs_tweet(qm.group(1)) if qm else None
    tm = _TWEET_LINK.search(replies_block)
    reply_url = _abs_tweet(tm.group(1)) if tm else None

    if text is None and not links and quoted_url is None and reply_url is None:
        return None
    return TweetResolution(text=text, links=links, quoted_url=quoted_url, reply_url=reply_url)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TweetResolver:
    def __init__(
        self,
        store: Store,
        instance: str,
        *,
        fetch: Callable[[str], str] = get_text,
        sleep: Callable[[float], None] = time.sleep,
        min_interval: float = 1.0,
        max_thread_hops: int = 4,
    ):
        self._store = store
        self._instance = instance.rstrip("/")
        self._fetch = fetch
        self._sleep = sleep
        self._min_interval = min_interval
        self._max_thread_hops = max_thread_hops
        self._fetched_any = False

    def resolve(self, url: str) -> TweetResolution | None:
        """Cache-first tweet resolution. `miss` is sticky (no refetch); None on any error."""
        cached = self._store.get_tweet_cache(url)
        if cached is not None:
            if cached["status"] == "miss":
                return None
            return TweetResolution(
                text=cached["text"],
                links=json.loads(cached["links_json"]),
                quoted_url=cached["quoted_url"],
                reply_url=cached["thread_url"],
            )

        m = is_tweet_url(url)
        if m is None:
            return None
        user, tweet_id = m
        if self._fetched_any:
            self._sleep(self._min_interval)  # pace consecutive real fetches
        self._fetched_any = True
        try:
            html_text = self._fetch(f"{self._instance}/{user}/status/{tweet_id}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                self._cache_miss(url)  # dead tweet / account — never refetch
            return None
        except Exception:
            return None  # transport/other error: transient, retry next run

        res = parse_tweet_html(html_text)
        if res is None:
            self._cache_miss(url)
            return None
        self._store.set_tweet_cache(
            url,
            text=res.text,
            links=res.links,
            quoted_url=res.quoted_url,
            thread_url=res.reply_url,
            status="ok",
            fetched_at=_now_iso(),
        )
        return res

    def _cache_miss(self, url: str) -> None:
        self._store.set_tweet_cache(
            url,
            text=None,
            links=[],
            quoted_url=None,
            thread_url=None,
            status="miss",
            fetched_at=_now_iso(),
        )

    def augment(self, raw: RawItem) -> RawItem:
        """Enrich a tweet item with resolved text/links so an arXiv id can surface.

        No-op unless the item is a tweet from a non-RSS source that carries no
        recoverable id yet. Follows one quoted tweet and then the author's own
        thread (same-author, bounded) until an id appears. Best-effort.
        """
        try:
            if raw.source == "rss" or raw.source.startswith("rss:"):
                return raw
            m = is_tweet_url(raw.url)
            if m is None:
                return raw
            if extract_arxiv_id(f"{raw.url} {raw.text or ''}") or extract_doi(
                f"{raw.url} {raw.text or ''}"
            ):
                return raw

            res = self.resolve(raw.url)
            if res is None:
                return raw
            user = m[0]
            gathered: list[str] = [res.text or "", *res.links]

            def has_id() -> bool:
                hay = f"{raw.text or ''} {raw.url} " + " ".join(gathered)
                return bool(extract_arxiv_id(hay) or extract_doi(hay))

            if not has_id() and res.quoted_url:
                quoted = self.resolve(res.quoted_url)
                if quoted is not None:
                    gathered += [quoted.text or "", *quoted.links]

            current, current_url, hops = res, raw.url, 0
            while (
                not has_id()
                and current is not None
                and current.reply_url
                and hops < self._max_thread_hops
            ):
                nxt = current.reply_url
                nm = is_tweet_url(nxt)
                if nm is None or nm[0] != user or nxt == current_url:
                    break
                current, current_url = self.resolve(nxt), nxt
                hops += 1
                if current is not None:
                    gathered += [current.text or "", *current.links]

            new_text = (
                (raw.text or "") + "\n" + "\n".join(g for g in gathered if g)
            ).strip()
            title = raw.title
            if not title or title.strip().endswith("on X"):
                title = None
            return replace(raw, text=new_text, title=title)
        except Exception:  # augmentation is never allowed to break ingest
            return raw
