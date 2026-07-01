"""Slack source: paper links posted in #papers-style channels.

Reads message history from configured channels via the Slack Web API
(`conversations.history`) using a per-workspace user token. Each external link
in a message becomes a RawItem; arXiv/DOI ids are recovered downstream by
`normalize.to_entry_fields`, so a paper posted here dedups against the same
paper from arXiv/RSS/Twitter.

Trust (gate bypass) is decided per item: a message in a `trusted` channel, or a
link to an "obviously a paper" domain (`paper_link_domains`), bypasses the
relevance gate; anything else is gated like Twitter/newsletter noise.

Slack instances are flaky and tokens may be missing; a channel that errors is
skipped (logged), never fatal, mirroring the Nitter source.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Callable, Iterator

from paper_watch.http import get_json
from paper_watch.models import RawItem

log = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"
_PAGE_LIMIT = 200
_MAX_PAGES = 20  # safety bound on pagination per channel

# Slack wraps links as <url> or <url|label>; mentions/channels/commands use
# other <...> forms that we don't want to treat as links.
_LINK_RE = re.compile(r"<(https?://[^>|]+)(?:\|[^>]*)?>")
# Slack-internal / non-content hosts to ignore.
_SKIP_HOSTS = ("slack.com", "giphy.com")

# (token, channel_id, oldest, cursor) -> parsed conversations.history response.
HistoryFetcher = Callable[[str, str, "str | None", "str | None"], dict]
TokenGetter = Callable[[str], "str | None"]


def is_paper_link(url: str, domains: list[str]) -> bool:
    """True if `url`'s host is, or is a subdomain of, one of `domains`."""
    host = _host(url)
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in domains)


def _host(url: str) -> str:
    from urllib.parse import urlparse

    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def extract_urls(text: str | None) -> list[str]:
    """Pull http(s) URLs out of Slack message markup, preserving order."""
    if not text:
        return []
    return _LINK_RE.findall(text)


def ts_to_iso(ts: str | None) -> str | None:
    """Convert a Slack message `ts` (epoch seconds) to an ISO-8601 'Z' string."""
    if not ts:
        return None
    try:
        epoch = float(ts)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_to_ts(iso: str | None) -> str | None:
    """Convert an ISO-8601 'Z' cutoff to a Slack `oldest` epoch string."""
    if not iso:
        return None
    try:
        dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return str(dt.timestamp())


def _clean_text(text: str | None) -> str | None:
    """Render Slack markup as plain text for titles / enrichment.

    `<url|label>`→label, `<url>`→url, `<@U…|name>`/`<#C…|name>`→name, drop
    `<!cmd>`, and unescape Slack's HTML entities.
    """
    if not text:
        return None
    s = re.sub(r"<(https?://[^>|]+)\|([^>]*)>", r"\2", text)
    s = re.sub(r"<(https?://[^>]+)>", r"\1", s)
    s = re.sub(r"<[@#][^>|]+\|([^>]*)>", r"\1", s)
    s = re.sub(r"<[@#]([^>|]+)>", r"\1", s)
    s = re.sub(r"<![^>]+>", "", s)
    s = s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    return s.strip() or None


def slack_history(
    token: str,
    channel_id: str,
    oldest: str | None,
    cursor: str | None,
    *,
    limit: int = _PAGE_LIMIT,
    fetch=get_json,
) -> dict:
    """Default HistoryFetcher: one `conversations.history` page."""
    params: dict[str, object] = {"channel": channel_id, "limit": limit}
    if oldest:
        params["oldest"] = oldest
    if cursor:
        params["cursor"] = cursor
    return fetch(
        f"{SLACK_API}/conversations.history",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )


class SlackSource:
    name = "slack"

    def __init__(
        self,
        workspaces,
        paper_link_domains: list[str],
        *,
        fetch: HistoryFetcher = slack_history,
        get_token: TokenGetter = os.environ.get,
    ):
        self.workspaces = workspaces
        self.paper_link_domains = paper_link_domains
        self._fetch = fetch
        self._get_token = get_token

    def fetch(self, since: str | None = None) -> Iterator[RawItem]:
        oldest = iso_to_ts(since)
        for ws in self.workspaces:
            token = self._get_token(ws.token_env)
            if not token:
                log.warning(
                    "No Slack token for workspace %s (env %s); skipping",
                    ws.name,
                    ws.token_env,
                )
                continue
            for channel in ws.channels:
                try:
                    yield from self._fetch_channel(token, ws, channel, since, oldest)
                except Exception as exc:
                    log.warning(
                        "Slack channel %s/%s failed (%s)", ws.name, channel.name, exc
                    )

    def _fetch_channel(self, token, ws, channel, since, oldest) -> Iterator[RawItem]:
        cursor: str | None = None
        for _ in range(_MAX_PAGES):
            page = self._fetch(token, channel.id, oldest, cursor)
            if not page.get("ok", False):
                raise RuntimeError(page.get("error", "slack api error"))
            for msg in page.get("messages", []):
                yield from self._message_items(msg, ws, channel, since)
            cursor = (page.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor:
                break

    def _message_items(self, msg, ws, channel, since) -> Iterator[RawItem]:
        published_at = ts_to_iso(msg.get("ts"))
        if since and published_at and published_at < since:
            return

        text = msg.get("text", "")
        att_by_url: dict[str, tuple[str | None, str | None]] = {}
        for att in msg.get("attachments") or []:
            link = att.get("title_link") or att.get("from_url")
            if link:
                att_by_url.setdefault(link, (att.get("title"), att.get("text")))

        seen: set[str] = set()
        urls: list[str] = []
        for url in [*extract_urls(text), *att_by_url]:
            host = _host(url)
            if not host or any(host == h or host.endswith("." + h) for h in _SKIP_HOSTS):
                continue
            if url in seen:
                continue
            seen.add(url)
            urls.append(url)

        clean = _clean_text(text)
        for url in urls:
            title, abstract = att_by_url.get(url, (None, None))
            yield RawItem(
                source=f"slack:{ws.name}:{channel.name}",
                url=url,
                title=title,
                abstract=abstract,
                text=clean,
                published_at=published_at,
                trusted=channel.trusted or is_paper_link(url, self.paper_link_domains),
            )


def list_channels(token: str, *, fetch=get_json) -> list[dict]:
    """Return [{id, name}] for channels the token can see (for the CLI helper).

    Listing private channels needs `groups:read`; a token scoped only for public
    channels (`channels:read`) 400s with missing_scope if we ask for both. So we
    ask for public+private, and on that specific missing-scope error fall back to
    public-only rather than failing the whole helper.
    """
    types = "public_channel,private_channel"
    out: list[dict] = []
    cursor: str | None = None
    for _ in range(_MAX_PAGES):
        params: dict[str, object] = {
            "limit": 1000,
            "types": types,
            "exclude_archived": True,
        }
        if cursor:
            params["cursor"] = cursor
        page = fetch(
            f"{SLACK_API}/conversations.list",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
        if not page.get("ok", False):
            if (
                page.get("error") == "missing_scope"
                and "private_channel" in types
            ):
                log.warning(
                    "slack conversations.list: %s (needed %s); "
                    "retrying public channels only",
                    page.get("error"),
                    page.get("needed"),
                )
                types = "public_channel"
                cursor = None
                out.clear()
                continue
            raise RuntimeError(page.get("error", "slack api error"))
        for ch in page.get("channels", []):
            out.append({"id": ch.get("id"), "name": ch.get("name")})
        cursor = (page.get("response_metadata") or {}).get("next_cursor") or None
        if not cursor:
            break
    return out
