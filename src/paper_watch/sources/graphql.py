"""ForumMagnum GraphQL source: tag-filtered posts from LessWrong-family forums.

LessWrong and the Alignment Forum both run ForumMagnum, which exposes a public
GraphQL endpoint (no auth for reads). Querying it for posts carrying a tag is
sturdier than the RSS feed — RSS scrapes the same data through shakier
infrastructure — and returns `baseScore`, so the karma threshold is applied
here instead of hoping a feed URL param works.

Karma is read at fetch time: a fresh post below `min_karma` is skipped this
run, but the newest-N window overlaps across runs, so it's picked up on a
later run once its karma crosses the threshold (dedup makes re-seeing cheap).

A linkpost's target URL becomes the item URL (so a linkposted arXiv paper
dedupes with the paper itself, as Slack links do) with the forum post kept as
`mention_url` provenance; a normal post is its own URL.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Iterable, Iterator

from paper_watch.http import post_json
from paper_watch.models import RawItem

log = logging.getLogger(__name__)

# A poster takes (endpoint URL, JSON payload) and returns the parsed JSON
# response. Injected for testing, like sources.Fetcher for GET-based sources.
Poster = Callable[[str, dict[str, Any]], dict[str, Any]]

_MAX_TEXT_CHARS = 2000  # enough body for enrichment context, not whole essays

# `terms` is a JSON scalar in the ForumMagnum schema, so the tag filter and
# sort go through as a variable rather than typed query arguments.
_QUERY = """
query PaperWatchPosts($terms: JSON) {
  posts(input: {terms: $terms}) {
    results {
      title
      url
      pageUrl
      postedAt
      baseScore
      user { displayName }
      coauthors { displayName }
      contents { plaintextDescription }
    }
  }
}
"""


def _terms(feed) -> dict[str, Any]:
    return {
        "filterSettings": {
            "tags": [{"tagId": feed.tag_id, "filterMode": "Required"}]
        },
        "sortedBy": "new",
        "limit": feed.limit,
    }


def _to_iso_z(timestamp: str | None) -> str | None:
    """ForumMagnum's '2026-07-13T17:20:06.976Z' -> the store's second-precision
    'Z' format, so timestamps compare lexicographically with every other source."""
    if not timestamp:
        return None
    try:
        dt = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _authors(post: dict[str, Any]) -> list[str]:
    people = [post.get("user")] + list(post.get("coauthors") or [])
    return [p["displayName"] for p in people if p and p.get("displayName")]


def parse_posts(data: dict[str, Any], feed) -> list[RawItem]:
    """RawItems for the posts in one GraphQL response, karma-filtered."""
    items: list[RawItem] = []
    for post in data["data"]["posts"]["results"]:
        if (post.get("baseScore") or 0) < feed.min_karma:
            continue
        page_url = post.get("pageUrl") or ""
        link_target = post.get("url") or ""
        is_linkpost = link_target.startswith(("http://", "https://"))
        contents = post.get("contents") or {}
        body = (contents.get("plaintextDescription") or "").strip()
        items.append(
            RawItem(
                source=f"graphql:{feed.name}",
                url=link_target if is_linkpost else page_url,
                mention_url=page_url if is_linkpost else None,
                title=" ".join((post.get("title") or "").split()) or None,
                authors=_authors(post),
                text=body[:_MAX_TEXT_CHARS] or None,
                published_at=_to_iso_z(post.get("postedAt")),
                # Ids in the body are citations; a linkpost's identity already
                # comes from its target URL above.
                extract_ids_from_text=False,
            )
        )
    return items


class GraphqlSource:
    name = "graphql"

    def __init__(self, feeds: Iterable, post: Poster = post_json):
        # `feeds` items need `.name`, `.endpoint`, `.tag_id`, `.min_karma`,
        # `.limit` (e.g. config.GraphqlFeedConfig).
        self.feeds = list(feeds)
        self._post = post

    def fetch(self, since: str | None = None) -> Iterator[RawItem]:
        for feed in self.feeds:
            try:
                data = self._post(
                    feed.endpoint, {"query": _QUERY, "variables": {"terms": _terms(feed)}}
                )
                if data.get("errors"):
                    raise RuntimeError(data["errors"][0].get("message", "GraphQL error"))
                items = parse_posts(data, feed)
            except Exception as exc:  # one bad feed must not abort the rest
                log.warning("GraphQL feed failed: %s (%s)", feed.name, exc)
                continue
            for item in items:
                if since and item.published_at and item.published_at < since:
                    continue
                yield item
