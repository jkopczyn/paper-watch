"""Slack source: paper links posted in #papers-style channels.

Reads message history from configured channels via the Slack Web API
(`conversations.history`) using a per-workspace user token. Each external link
in a message becomes a RawItem; arXiv/DOI ids are recovered downstream by
`normalize.to_entry_fields`, so a paper posted here dedups against the same
paper from arXiv/RSS/Twitter.

Trust (gate bypass) is decided per item: a message in a `trusted` channel, or a
link to an "obviously a paper" domain (`paper_link_domains`), bypasses the
relevance gate; anything else is gated like Twitter/newsletter noise.
"""

from __future__ import annotations

from urllib.parse import urlparse


def is_paper_link(url: str, domains: list[str]) -> bool:
    """True if `url`'s host is, or is a subdomain of, one of `domains`."""
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return False
    host = host.lower()
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in domains)
