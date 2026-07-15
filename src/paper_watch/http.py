"""Tiny HTTP helper. Sources accept an injectable fetcher so tests stay offline."""

from __future__ import annotations

import time
from typing import Any, Callable

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


def get_bytes(
    url: str,
    timeout: float = DEFAULT_TIMEOUT,
    *,
    max_retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> bytes:
    """GET `url` and return the raw response bytes (e.g. a PDF), retrying on 429."""
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
        return resp.content
    raise RuntimeError("get_bytes: exhausted retries without returning")


def get_json(
    url: str,
    timeout: float = DEFAULT_TIMEOUT,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    max_retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """GET `url` and return the parsed JSON body, retrying on 429.

    Like `get_text` but for JSON APIs (the Slack Web API). Caller-supplied
    `headers` (e.g. an Authorization bearer token) are merged over the default
    User-Agent. Slack rate-limits with HTTP 429 + Retry-After, which this
    handles via the same backoff as `get_text`.
    """
    merged_headers = {"User-Agent": USER_AGENT, **(headers or {})}
    for attempt in range(max_retries + 1):
        resp = httpx.get(
            url,
            timeout=timeout,
            headers=merged_headers,
            params=params,
            follow_redirects=True,
        )
        if resp.status_code == 429 and attempt < max_retries:
            sleep(_retry_after(resp, attempt))
            continue
        resp.raise_for_status()
        return resp.json()
    # Unreachable: the final attempt either returns or raises above.
    raise RuntimeError("get_json: exhausted retries without returning")


def post_json(
    url: str,
    payload: dict[str, Any],
    timeout: float = DEFAULT_TIMEOUT,
    *,
    max_retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """POST `payload` as JSON to `url` and return the parsed JSON body.

    Like `get_json` but for APIs that take a JSON request body (the ForumMagnum
    GraphQL endpoint), with the same 429 backoff.
    """
    for attempt in range(max_retries + 1):
        resp = httpx.post(
            url,
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
            json=payload,
            follow_redirects=True,
        )
        if resp.status_code == 429 and attempt < max_retries:
            sleep(_retry_after(resp, attempt))
            continue
        resp.raise_for_status()
        return resp.json()
    # Unreachable: the final attempt either returns or raises above.
    raise RuntimeError("post_json: exhausted retries without returning")
