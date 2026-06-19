"""Tiny HTTP helper. Sources accept an injectable fetcher so tests stay offline."""

from __future__ import annotations

import httpx

DEFAULT_TIMEOUT = 20.0
USER_AGENT = "paper-watch/0.1 (AI-safety paper digest; contact: ja.kopczynski@gmail.com)"


def get_text(url: str, timeout: float = DEFAULT_TIMEOUT) -> str:
    """GET `url` and return the response body as text, raising on HTTP errors."""
    resp = httpx.get(
        url,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.text
