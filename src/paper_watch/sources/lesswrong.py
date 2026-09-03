"""Single-post metadata for LessWrong-family forums, read over GraphQL.

lesswrong.com serves a bot challenge to plain HTTP metadata fetches, so a post
URL yields nothing on the HTML path. The same posts are readable through the
ForumMagnum GraphQL backend the `graphql:` feeds already use, one post at a
time, with no auth.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlsplit

from paper_watch.http import post_json
from paper_watch.identity import _LESSWRONG_MIRROR_HOSTS
from paper_watch.sources.graphql import Poster, author_names, to_iso_z

log = logging.getLogger(__name__)

_ENDPOINT = "https://www.lesswrong.com/graphql"

_MAX_ABSTRACT_CHARS = 2000  # matches graphql._MAX_TEXT_CHARS

# canonicalize_url only rewrites mirror hosts for /posts/ paths, so a sequence
# URL arrives on its original host and the host set is checked here directly.
_HOSTS = _LESSWRONG_MIRROR_HOSTS | {"www.lesswrong.com"}

_POST_PATH = re.compile(r"^/posts/([A-Za-z0-9]{5,})(?:/|$)")
_SEQUENCE_PATH = re.compile(r"^/s/[A-Za-z0-9]{5,}/p/([A-Za-z0-9]{5,})(?:/|$)")

# Query shape verified against the live endpoint on 2026-09-02.
_QUERY = """
query PaperWatchPost($id: String!) {
  post(input: {selector: {_id: $id}}) {
    result {
      _id
      title
      postedAt
      user { displayName }
      coauthors { displayName }
      contents { plaintextDescription }
    }
  }
}
"""


def post_id_from_url(url: str | None) -> str | None:
    """The post id in an LW/AF/GreaterWrong post URL, or None for anything else.

    Both served shapes are read: /posts/<id>/<slug> and the sequence form
    /s/<sequence-id>/p/<post-id>, which yields the post id, never the sequence's.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if (parts.hostname or "").lower() not in _HOSTS:
        return None
    path = (parts.path or "").rstrip("/")
    for pattern in (_POST_PATH, _SEQUENCE_PATH):
        m = pattern.match(path)
        if m:
            return m.group(1)
    return None


def parse_post(data: dict[str, Any]) -> dict[str, Any] | None:
    """Title, authors, abstract and date from one single-post response."""
    post = ((data.get("data") or {}).get("post") or {}).get("result") or {}
    title = " ".join((post.get("title") or "").split())
    if not title:
        return None
    body = ((post.get("contents") or {}).get("plaintextDescription") or "").strip()
    return {
        "title": title,
        "authors": author_names(post),
        "abstract": body[:_MAX_ABSTRACT_CHARS] or None,
        "published_at": to_iso_z(post.get("postedAt")),
    }


class LessWrongResolver:
    """Metadata for one LW-family post URL. Never raises: a miss is None."""

    def __init__(self, post: Poster = post_json):
        self._post = post

    def resolve(self, url: str | None) -> dict[str, Any] | None:
        post_id = post_id_from_url(url)
        if not post_id:
            return None
        try:
            data = self._post(_ENDPOINT, {"query": _QUERY, "variables": {"id": post_id}})
            if data.get("errors"):
                raise RuntimeError(data["errors"][0].get("message", "GraphQL error"))
            return parse_post(data)
        except Exception as exc:
            log.debug("LessWrong GraphQL resolve failed: %s (%s)", url, exc)
            return None

    def resolve_date(self, url: str | None) -> str | None:
        return (self.resolve(url) or {}).get("published_at")
