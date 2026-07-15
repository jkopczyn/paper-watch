"""Fill a URL-less paper entry by searching for its title.

Semantic Scholar's title-search endpoint first (best precision for papers, no
API key), Crossref as a fallback. Deterministic and best-effort: a missing or
low-confidence match returns None rather than fabricating a link, so we never
attach the wrong paper to an entry. A title-overlap guard rejects a top hit that
doesn't actually match the query — the common case for a blog post or newsletter
that isn't a paper at all.
"""

from __future__ import annotations

import json
import urllib.parse

from paper_watch.http import get_text
from paper_watch.identity import normalize_title
from paper_watch.sources import Fetcher

S2_SEARCH = "https://api.semanticscholar.org/graph/v1/paper/search"
CROSSREF = "https://api.crossref.org/works"
_S2_FIELDS = "title,externalIds,openAccessPdf,url,publicationDate,year,authors,abstract"
_MATCH_THRESHOLD = 0.6  # fraction of the query's title tokens the hit must share


def s2_search_url(title: str) -> str:
    params = urllib.parse.urlencode(
        {"query": title, "fields": _S2_FIELDS, "limit": 1}
    )
    return f"{S2_SEARCH}?{params}"


def crossref_url(title: str) -> str:
    params = urllib.parse.urlencode({"query.title": title, "rows": 1})
    return f"{CROSSREF}?{params}"


def _title_matches(query: str, candidate: str) -> bool:
    """True if `candidate` shares enough title tokens with `query`.

    Guards against a search engine returning an unrelated top hit for a
    non-paper title (a blog post, a newsletter issue).
    """
    q = set(normalize_title(query).split())
    c = set(normalize_title(candidate).split())
    if not q or not c:
        return False
    return len(q & c) / len(q) >= _MATCH_THRESHOLD


def _iso_date(value: str | None) -> str | None:
    """Normalize a 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD' string to an ISO-8601 Z."""
    if not value:
        return None
    parts = str(value).strip().split("-")
    if not parts[0].isdigit():
        return None
    y = int(parts[0])
    m = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
    d = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
    return f"{y:04d}-{m:02d}-{d:02d}T00:00:00Z"


def _crossref_date(obj: dict | None) -> str | None:
    if not obj:
        return None
    dp = (obj.get("date-parts") or [[]])[0]
    if not dp:
        return None
    y = dp[0]
    m = dp[1] if len(dp) > 1 else 1
    d = dp[2] if len(dp) > 2 else 1
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}T00:00:00Z"


def parse_s2_search(text: str, query_title: str) -> dict | None:
    items = json.loads(text).get("data") or []
    if not items:
        return None
    item = items[0]
    candidate = item.get("title") or ""
    if not _title_matches(query_title, candidate):
        return None
    ext = item.get("externalIds") or {}
    arxiv_id = ext.get("ArXiv")
    doi = ext.get("DOI")
    if arxiv_id:
        url = f"https://arxiv.org/abs/{arxiv_id}"
    elif (item.get("openAccessPdf") or {}).get("url"):
        url = item["openAccessPdf"]["url"]
    else:
        url = item.get("url")
    if not url:
        return None
    year = str(item["year"]) if item.get("year") else None
    return {
        "url": url,
        "arxiv_id": arxiv_id,
        "doi": doi,
        "published_at": _iso_date(item.get("publicationDate") or year),
        "title": candidate,
        "authors": [a.get("name") for a in item.get("authors") or [] if a.get("name")],
        "abstract": item.get("abstract"),
    }


def parse_crossref(text: str, query_title: str) -> dict | None:
    items = (json.loads(text).get("message") or {}).get("items") or []
    if not items:
        return None
    item = items[0]
    titles = item.get("title") or []
    candidate = titles[0] if titles else ""
    if not _title_matches(query_title, candidate):
        return None
    doi = item.get("DOI")
    if not doi:
        return None
    published = item.get("published") or item.get("published-print") or item.get("issued")
    authors = [
        f"{a.get('given', '')} {a.get('family', '')}".strip()
        for a in item.get("author") or []
    ]
    return {
        "url": f"https://doi.org/{doi}",
        "arxiv_id": None,
        "doi": doi,
        "published_at": _crossref_date(published),
        "title": candidate,
        "authors": [a for a in authors if a],
        "abstract": item.get("abstract"),
    }


class PaperSearchResolver:
    """Resolve a paper's canonical link from its title, S2 then Crossref."""

    def __init__(self, fetch: Fetcher = get_text):
        self._fetch = fetch

    def search(self, title: str | None) -> dict | None:
        if not title or not title.strip():
            return None
        for build_url, parse in (
            (s2_search_url, parse_s2_search),
            (crossref_url, parse_crossref),
        ):
            try:
                result = parse(self._fetch(build_url(title)), title)
            except Exception:  # best-effort: a bad response is never fatal
                result = None
            if result:
                return result
        return None
