"""Paper identity: extract stable IDs, normalize titles, and dedup entries.

The same paper shows up across arXiv, newsletters, and tweets. We resolve it to a
single `entries` row by arXiv ID, then DOI, then normalized title.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

from paper_watch.store import Store

# New-style: 2406.01234 or 2406.01234v3  (4-digit YYMM + 4-5 digit number)
_ARXIV_NEW = re.compile(r"\b(\d{4}\.\d{4,5})(?:v\d+)?\b")
# Old-style: hep-th/9901001 or cs.AI/0701001. The archive prefix must be a real
# arXiv archive name — a bare [a-z-]+ matches ordinary URL path segments like
# "technology/5934266".
_ARXIV_ARCHIVES = (
    "astro-ph|cond-mat|gr-qc|hep-ex|hep-lat|hep-ph|hep-th|math-ph|nlin|nucl-ex|"
    "nucl-th|physics|quant-ph|math|cs|q-bio|q-fin|stat|eess|econ|cmp-lg|chao-dyn|"
    "q-alg|alg-geom|dg-ga|funct-an|solv-int|patt-sol|adap-org"
)
_ARXIV_OLD = re.compile(rf"\b((?:{_ARXIV_ARCHIVES})(?:\.[A-Z]{{2}})?/\d{{7}})\b")
# DOI per Crossref's recommended pattern.
_DOI = re.compile(r"\b(10\.\d{4,9}/[-._;()/:A-Za-z0-9]+)\b")
# A DOI lifted out of a publisher URL runs to the end of the path, taking the
# file extension with it ("10.1007/s11023-020-09539-2.pdf"), which then matches
# no other source's DOI for the same paper. Only these known extensions are
# stripped — a bare dot is legal inside a DOI ("10.1145/1234567.8901234").
_DOI_FILE_EXT = re.compile(r"\.(?:pdf|html?|xml|epub|txt|json|full|abstract)$", re.IGNORECASE)
_TITLE_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")
# Trailing "— LessWrong" / "| OpenAI"-style site suffix (em/en dash or pipe with
# surrounding spaces). Only stripped when a substantial title remains.
_SITE_SUFFIX = re.compile(r"\s+[|–—]\s+[^|–—]{1,40}$")
_SITE_SUFFIX_MIN_REMAINDER = 20

# Tweet permalinks: /<user>/status/<id> on twitter/x or any Nitter mirror.
_TWEET_PATH = re.compile(r"^/([A-Za-z0-9_]{1,15})/status(?:es)?/(\d+)")
_TWITTER_HOSTS = {"twitter.com", "www.twitter.com", "mobile.twitter.com", "x.com", "www.x.com"}


def canonicalize_url(url: str | None) -> str | None:
    """Normalize a mention URL so the same item dedups across fetch routes.

    Tweets seen via any Nitter instance (or x.com share links with tracking
    params) collapse to https://twitter.com/<user>/status/<id>; other URLs keep
    their query but lose the fragment (#m, #section anchors).
    """
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    host = (parts.hostname or "").lower()
    m = _TWEET_PATH.match(parts.path or "")
    if m and (
        host in _TWITTER_HOSTS or "nitter" in host or host in ("localhost", "127.0.0.1")
    ):
        return f"https://twitter.com/{m.group(1)}/status/{m.group(2)}"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def extract_arxiv_id(text: str | None) -> str | None:
    """Return the canonical (version-stripped) arXiv ID found in `text`, if any."""
    if not text:
        return None
    m = _ARXIV_NEW.search(text)
    if m:
        return m.group(1)
    m = _ARXIV_OLD.search(text)
    if m:
        return m.group(1)
    return None


def extract_doi(text: str | None) -> str | None:
    """Return the first DOI found in `text`, trailing punctuation stripped."""
    if not text:
        return None
    m = _DOI.search(text)
    if not m:
        return None
    return _DOI_FILE_EXT.sub("", m.group(1).rstrip(".,;)"))


def normalize_title(title: str | None) -> str:
    """Lowercase, drop punctuation, and collapse whitespace for fuzzy matching.

    A trailing site suffix ("Paper Title — LessWrong") is stripped first so the
    same paper dedups against its bare title.
    """
    if not title:
        return ""
    m = _SITE_SUFFIX.search(title)
    if m and m.start() >= _SITE_SUFFIX_MIN_REMAINDER:
        title = title[: m.start()]
    stripped = _TITLE_PUNCT.sub(" ", title.lower())
    return _WS.sub(" ", stripped).strip()


# A title is only evidence of identity if it names one specific paper. PDF text
# extraction routinely returns the cover page's boilerplate instead of the title —
# two unrelated Anthropic system cards both come back as "System Card", and
# Springer's PDFs all carry the running head "Vol.:(0123456789)". Matching on
# those fuses unrelated documents into one entry, which loses a paper outright;
# refusing to match merely leaves a duplicate. URLs, arXiv ids and DOIs cannot
# collide this way and are always trusted.
_MIN_TITLE_CHARS = 12
_GENERIC_TITLES = frozenset(
    {
        "system card",
        "model card",
        "technical report",
        "white paper",
        "whitepaper",
        "preprint",
        "working paper",
        "abstract",
        "introduction",
        "appendix",
        "supplementary material",
    }
)


def is_distinctive_title(title_norm: str | None) -> bool:
    """Whether `title_norm` is specific enough to identify a paper by itself."""
    if not title_norm or len(title_norm) < _MIN_TITLE_CHARS:
        return False
    if title_norm in _GENERIC_TITLES:
        return False
    # Mostly-numeric strings are page furniture ("vol 0123456789"), not titles.
    words = title_norm.split()
    return sum(w.isdigit() for w in words) * 2 < len(words)


def resolve_or_create(store: Store, fields: dict) -> tuple[int, bool]:
    """Find the existing entry for `fields`, or create it.

    Match order: source URL, arXiv ID, DOI, then normalized title. Returns
    (entry_id, created).

    Source URL comes first because it is the only one of the four that a metadata
    resolver never rewrites. An entry linked as a bare PDF is born titled with its
    own URL; the resolver then replaces title_norm with the real title, and a
    title-only match would miss on the next run and create the entry again —
    once per run, forever.
    """
    source_url = fields.get("source_url")
    arxiv_id = fields.get("arxiv_id")
    doi = fields.get("doi")
    title_norm = fields.get("title_norm") or normalize_title(fields.get("title"))

    existing = None
    if source_url:
        existing = store.get_entry_by_source_url(source_url)
    if existing is None and arxiv_id:
        existing = store.get_entry_by_arxiv_id(arxiv_id)
    if existing is None and doi:
        existing = store.get_entry_by_doi(doi)
    if existing is None and is_distinctive_title(title_norm):
        existing = store.get_entry_by_title_norm(title_norm)
    if existing is not None:
        entry_id = int(existing["id"])
        # Teach the entry this URL too, so a match found the slow way (arXiv id,
        # DOI, title) is found by URL next run — even if a resolver later rewrites
        # the title out from under us. This is how one paper accumulates its
        # aliases: the arXiv link, the AF post, the PDF.
        if source_url:
            store.add_entry_url(entry_id, source_url)
        return entry_id, False

    entry_id = store.insert_entry(
        title=fields["title"],
        title_norm=title_norm,
        first_seen_at=fields["first_seen_at"],
        arxiv_id=arxiv_id,
        doi=doi,
        authors=fields.get("authors") or [],
        abstract=fields.get("abstract"),
        links=fields.get("links") or {},
        source_url=source_url,
    )
    return entry_id, True
