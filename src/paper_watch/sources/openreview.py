"""Resolve an OpenReview forum link to its title/abstract/authors via the API.

OpenReview shows the abstract behind a human-check gate in the browser. The
public REST API returns most notes with no auth, but some venues/submissions are
only visible to a logged-in user. OpenReview has no static API key: you POST
`{"id": <email>, "password": <pw>}` to `/login` and get back a bearer token, sent
as `Authorization: Bearer …` on later requests. When `OPENREVIEW_USERNAME` /
`OPENREVIEW_PASSWORD` are set we log in once and read gated notes too; without
them the resolver falls back to the anonymous behavior.

Older venues live on API v1 (`api.openreview.net`, flat `content`), newer ones on
API v2 (`api2.openreview.net`, values wrapped as `{"value": …}`); we try v2 then v1.
"""

from __future__ import annotations

import logging
import os
from typing import Callable
from urllib.parse import parse_qs, urlsplit

from paper_watch.http import get_json, post_json

log = logging.getLogger(__name__)

_V2 = "https://api2.openreview.net/notes"
_V1 = "https://api.openreview.net/notes"
_LOGIN = "https://api2.openreview.net/login"


def _env_login(post=post_json, getenv=os.environ.get) -> str | None:
    """Log in with `OPENREVIEW_USERNAME`/`OPENREVIEW_PASSWORD`, return a token.

    Returns None (and attempts no POST) when either credential is unset, so the
    resolver degrades to anonymous access. Login failures are logged, not raised.
    """
    user, pw = getenv("OPENREVIEW_USERNAME"), getenv("OPENREVIEW_PASSWORD")
    if not user or not pw:
        return None
    try:
        data = post(_LOGIN, {"id": user, "password": pw})
    except Exception as exc:
        log.warning("OpenReview login failed: %s", exc)
        return None
    return data.get("token")


def forum_id(url: str | None) -> str | None:
    """The `id` of an OpenReview `/forum?id=…` (or `/pdf?id=…`) URL, else None."""
    if not url:
        return None
    parts = urlsplit(url)
    if "openreview.net" not in (parts.hostname or "").lower():
        return None
    ids = parse_qs(parts.query).get("id")
    return ids[0] if ids else None


def _field(content: dict, key: str):
    """Read a content field across API versions (v2 wraps values in {'value': …})."""
    val = content.get(key)
    if isinstance(val, dict) and "value" in val:
        return val["value"]
    return val


class OpenReviewResolver:
    def __init__(
        self,
        fetch: Callable[..., dict] = get_json,
        login: Callable[[], str | None] = _env_login,
    ):
        self._fetch = fetch
        self._login = login
        self._token: str | None = None
        self._logged_in = False

    def _auth_headers(self) -> dict | None:
        """Bearer header for the cached token, logging in lazily once. None when
        no credentials are configured (→ anonymous request, as before)."""
        if not self._logged_in:
            self._token = self._login()
            self._logged_in = True
        return {"Authorization": f"Bearer {self._token}"} if self._token else None

    def resolve(self, url: str) -> dict | None:
        """{title, abstract, authors} for an OpenReview forum URL, or None."""
        fid = forum_id(url)
        if fid is None:
            return None
        for endpoint in (_V2, _V1):
            note = self._first_note(endpoint, fid)
            if note is None:
                continue
            content = note.get("content") or {}
            title = _field(content, "title")
            if not title:
                continue
            authors = _field(content, "authors") or []
            return {
                "title": str(title),
                "abstract": _field(content, "abstract"),
                "authors": [str(a) for a in authors] if isinstance(authors, list) else [],
            }
        return None

    def _first_note(self, endpoint: str, fid: str) -> dict | None:
        try:
            data = self._fetch(endpoint, params={"id": fid}, headers=self._auth_headers())
        except Exception as exc:
            log.debug("OpenReview %s failed for %s: %s", endpoint, fid, exc)
            return None
        notes = data.get("notes") or []
        return notes[0] if notes else None
