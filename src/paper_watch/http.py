"""Tiny HTTP helper. Sources accept an injectable fetcher so tests stay offline."""

from __future__ import annotations

import time
from typing import Callable

import httpx

DEFAULT_TIMEOUT = 20.0
USER_AGENT = "paper-watch/0.1 (AI-safety paper digest; contact: ja.kopczynski@gmail.com)"


def _retry_after(resp: httpx.Response, attempt: int) -> float:
    """Seconds to wait before retrying a 429: honor Retry-After, else backoff."""
    header = resp.headers.get("Retry-After")
    if header and header.strip().isdigit():
        return float(header.strip())
    return float(2**attempt)


def get_text(
    url: str,
    timeout: float = DEFAULT_TIMEOUT,
    *,
    max_retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """GET `url` and return the body text, retrying on 429 (Too Many Requests).

    arXiv and Semantic Scholar both rate-limit; on a 429 we wait per the
    Retry-After header (or exponential backoff) and try again, up to
    `max_retries` times, before raising.
    """
    for attempt in range(max_retries + 1):
        resp = httpx.get(
            url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            follow_redirects=True,
        )
        if resp.status_code == 429 and attempt < max_retries:
            sleep(_retry_after(resp, attempt))
            continue
        resp.raise_for_status()
        return resp.text
    # Unreachable: the final attempt either returns or raises above.
    raise RuntimeError("get_text: exhausted retries without returning")
