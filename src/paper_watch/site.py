"""The public site: every paper paper-watch has put in a digest, with the
reading group's ratings where they exist.

The site is built from the `shown` table, so a paper that never appeared in a
digest never appears here. The internal `score`, the `why` text, the stored
`abstract`, source labels, and everything in `mentions` are never published,
and no field identifies an individual voter: the store holds only per-paper
aggregates. Only http and https links are published; a paper whose first
display link is anything else is shown without a link.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlsplit

from jinja2 import Environment

from paper_watch.runtime import _display_links, _pub_display

_ALLOWED_SCHEMES = frozenset({"http", "https"})


def public_url(url: str | None) -> str | None:
    """`url` if it is a plain http or https link, otherwise None.

    This is the gate on everything that reaches an `href`. It rejects
    `javascript:`, `data:` and `slack://` links, protocol-relative URLs, URLs
    with no host, and any string carrying whitespace or a control character.
    The accepted string is returned unchanged, so the published link is the
    one the digest email shows.
    """
    if not url:
        return None
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        return None
    try:
        host = parts.hostname
    except ValueError:
        return None
    if not host:
        return None
    return url


@dataclass(frozen=True)
class SiteRecord:
    """One appearance of one paper in one digest.

    `entry_id` is internal: it is how the renderers tell two appearances of the
    same paper apart, and `to_json` never serializes it.
    """

    entry_id: int
    digest_at: str
    rank: int | None
    resurfaced: bool
    title: str
    authors: list[str]
    url: str | None
    arxiv_id: str | None
    published: str
    is_estimate: bool
    tags: list[str]
    tldr: str | None
    group_rating: int | None
    picked: bool
    read: bool

    def to_json(self) -> dict:
        """The published form of this record. The key set here is the public
        contract of `papers.json`."""
        return {
            "digest_at": self.digest_at,
            "rank": self.rank,
            "resurfaced": self.resurfaced,
            "title": self.title,
            "authors": list(self.authors),
            "url": self.url,
            "arxiv_id": self.arxiv_id,
            "published": self.published,
            "is_estimate": self.is_estimate,
            "tags": list(self.tags),
            "tldr": self.tldr,
            "group_rating": self.group_rating,
            "picked": self.picked,
            "read": self.read,
        }


def load_records(store) -> list[SiteRecord]:
    """One record per `shown` row, newest digest first and in rank order.

    The displayed link and the publication date come from the same helpers the
    digest email uses, so the site and the email agree on both.
    """
    records: list[SiteRecord] = []
    for row in store.shown_history():
        entry_id = int(row["id"])
        links = _display_links(store, entry_id, json.loads(row["links_json"]))
        first_link = next(iter(links.values()), None)
        published, is_estimate = _pub_display(store, row)
        records.append(
            SiteRecord(
                entry_id=entry_id,
                digest_at=row["digest_at"],
                rank=row["rank"],
                resurfaced=bool(row["resurfaced"]),
                title=row["title"],
                authors=json.loads(row["authors_json"]),
                url=public_url(first_link),
                arxiv_id=row["arxiv_id"],
                published=published,
                is_estimate=is_estimate,
                tags=json.loads(row["tags_json"]),
                tldr=row["tldr"],
                group_rating=row["latest_rating"],
                picked=bool(row["picked"]),
                read=bool(row["read"]),
            )
        )
    return records


def group_by_digest(records: list[SiteRecord]) -> list[tuple[str, list[SiteRecord]]]:
    """`(digest_at, records)` for each digest, keeping the query's order: newest
    digest first, and within a digest the order the email used."""
    groups: list[tuple[str, list[SiteRecord]]] = []
    for record in records:
        if groups and groups[-1][0] == record.digest_at:
            groups[-1][1].append(record)
        else:
            groups.append((record.digest_at, [record]))
    return groups


@dataclass(frozen=True)
class TableRow:
    """One paper in the single table, under the first digest it appeared in."""

    record: SiteRecord
    first_digest_at: str
    times_shown: int


def table_rows(records: list[SiteRecord]) -> list[TableRow]:
    """One row per paper rather than per appearance.

    `record` is the paper's first appearance, so its rank is the rank it had in
    the digest the row is filed under, and `times_shown` counts every
    appearance including that first one. Rows keep the order of the first
    appearances, which is by digest newest first and then by rank.
    """
    first_seen: dict[int, SiteRecord] = {}
    counts: dict[int, int] = {}
    for record in records:
        counts[record.entry_id] = counts.get(record.entry_id, 0) + 1
        # Records are newest first, so the last one seen is the earliest digest.
        first_seen[record.entry_id] = record

    rows = [
        TableRow(
            record=first_seen[record.entry_id],
            first_digest_at=first_seen[record.entry_id].digest_at,
            times_shown=counts[record.entry_id],
        )
        for record in records
        if record is first_seen[record.entry_id]
    ]
    return rows


# The name and the one-sentence description shown at the top of the index.
# Both are placeholders for the user to confirm, kept here so that the wording
# can be changed in one place.
SITE_TITLE = "paper-watch"
SITE_DESCRIPTION = (
    "Papers suggested by paper-watch, a scan of new AI safety research, with "
    "the reading group's ratings where they exist."
)

# Labels on content that a reader could otherwise mistake for the group's own
# words or for a rating of our own.
TLDR_LABEL = "AI summary"
RATING_FORMAT = "group rating {n}/5"
RATINGS_NOTE = "Ratings come from the reading group's weekly poll."

# Every file publish writes into the site repo. Nothing else in that repo is
# touched, and a file listed here is overwritten on every publish.
MANAGED_FILES = (
    "index.html",
    "all.html",
    "papers.json",
    "style.css",
    "CNAME",
    "README.md",
)

STYLE_CSS = """\
:root {
  --bg: #ffffff;
  --fg: #1a1a1a;
  --muted: #666666;
  --rule: #e5e7eb;
  --chip-bg: #eeeef8;
  --chip-fg: #333344;
  --flag-bg: #fde68a;
  --flag-fg: #92400e;
  --link: #1d4ed8;
}

body {
  font-family: -apple-system, Helvetica, Arial, sans-serif;
  max-width: 760px;
  margin: 0 auto;
  padding: 0 16px 48px;
  background: var(--bg);
  color: var(--fg);
  line-height: 1.45;
}

a { color: var(--link); }
h1 { font-size: 22px; margin-bottom: 4px; }
h2 { font-size: 15px; margin: 32px 0 8px; color: var(--muted); }
h3 { font-size: 16px; margin: 0 0 2px; }
p.lede { color: var(--muted); margin-top: 0; }
p.authors { color: var(--muted); font-size: 13px; margin: 0 0 6px; }
p.meta { margin: 6px 0; font-size: 12px; }
article.paper { border-top: 1px solid var(--rule); padding: 12px 0; }
details { font-size: 14px; }
summary { color: var(--muted); cursor: pointer; }

.chip {
  display: inline-block;
  background: var(--chip-bg);
  color: var(--chip-fg);
  padding: 1px 6px;
  border-radius: 3px;
  margin-right: 4px;
  font-size: 12px;
}
.chip.flag { background: var(--flag-bg); color: var(--flag-fg); }
.chip.tag { cursor: pointer; }
.chip.tag.active { outline: 1px solid var(--link); }

footer {
  border-top: 1px solid var(--rule);
  margin-top: 32px;
  padding-top: 12px;
  font-size: 13px;
  color: var(--muted);
}

table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--rule); vertical-align: top; }
th { color: var(--muted); font-weight: 600; }
td.num { text-align: right; white-space: nowrap; }
input.filter { width: 100%; padding: 6px 8px; font-size: 14px; margin: 12px 0; }

@media (prefers-color-scheme: dark) {
  :root {
    --bg: #15171a;
    --fg: #e8e8ea;
    --muted: #9ba1a8;
    --rule: #2c3034;
    --chip-bg: #262b33;
    --chip-fg: #c8cfd8;
    --flag-bg: #4a3a12;
    --flag-fg: #f3d58a;
    --link: #7aa2f7;
  }
}
"""

SITE_README = """\
# paper-watch site

Every file in this repository is generated by paper-watch and served from the
default branch at https://{domain}/. Hand edits are overwritten on the next
publish, so a change belongs in the paper-watch source instead.
"""


def rating_markers(record: SiteRecord) -> list[str]:
    """The short markers shown next to a paper.

    A rating and a reading are independent, so both can show at once. "picked"
    is shown only when the group chose the paper but it has neither been read
    nor rated, because in the other cases those markers say more.
    """
    markers: list[str] = []
    if record.group_rating is not None:
        markers.append(RATING_FORMAT.format(n=record.group_rating))
    if record.read:
        markers.append("read")
    if record.picked and not record.read and record.group_rating is None:
        markers.append("picked")
    return markers


def latest_digest_at(records: list[SiteRecord]) -> str | None:
    """The newest `digest_at` in `records`, or None when nothing was ever shown.

    The site carries this instead of a wall-clock build time, so that a publish
    that changes nothing produces no diff.
    """
    return records[0].digest_at if records else None


_INDEX_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ site_title }}</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<h1>{{ site_title }}</h1>
<p class="lede">{{ site_description }}</p>
{% for digest_at, records in groups %}
<section>
<h2>{{ digest_at[:10] }}</h2>
{% for r in records %}
<article class="paper">
<h3>{% if r.url %}<a href="{{ r.url }}">{{ r.title }}</a>{% else %}{{ r.title }}{% endif %}</h3>
{% if r.authors %}<p class="authors">{{ r.authors|join(", ") }}</p>{% endif %}
<p class="meta">
{%- if r.published %}<span class="chip">{{ "~" if r.is_estimate }}{{ r.published }}</span>{% endif %}
{%- if r.resurfaced %}<span class="chip flag">resurfaced</span>{% endif %}
{%- for t in r.tags %}<span class="chip tag">{{ t }}</span>{% endfor %}
{%- for m in markers(r) %}<span class="chip">{{ m }}</span>{% endfor %}
</p>
{% if r.tldr %}<details><summary>{{ tldr_label }}</summary><p>{{ r.tldr }}</p></details>{% endif %}
</article>
{% endfor %}
</section>
{% endfor %}
<footer>
<p><a href="all.html">All papers in one table</a> · <a href="papers.json">papers.json</a></p>
<p>{{ ratings_note }}</p>
{% if latest %}<p>Latest digest: {{ latest[:10] }}</p>{% endif %}
</footer>
</body>
</html>
"""

_ALL_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ site_title }}: all papers</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<h1>{{ site_title }}: all papers</h1>
<p class="lede">{{ site_description }}</p>
<p><a href="index.html">Back to the digests</a></p>
<input class="filter" id="filter" type="search" placeholder="Filter by title or author">
<table>
<thead>
<tr><th>title</th><th>authors</th><th>published</th><th>tags</th><th>first digest</th><th>rank</th><th>times shown</th><th>rating</th></tr>
</thead>
<tbody>
{% for row in rows %}
<tr data-tags="{{ row.record.tags|join("|") }}" data-text="{{ (row.record.title ~ " " ~ row.record.authors|join(" "))|lower }}">
<td>{% if row.record.url %}<a href="{{ row.record.url }}">{{ row.record.title }}</a>{% else %}{{ row.record.title }}{% endif %}</td>
<td>{{ row.record.authors|join(", ") }}</td>
<td>{{ "~" if row.record.is_estimate }}{{ row.record.published }}</td>
<td>{% for t in row.record.tags %}<span class="chip tag" data-tag="{{ t }}">{{ t }}</span>{% endfor %}</td>
<td>{{ row.first_digest_at[:10] }}</td>
<td class="num">{{ row.record.rank if row.record.rank is not none }}</td>
<td class="num">{{ row.times_shown }}</td>
<td>{{ markers(row.record)|join(", ") }}</td>
</tr>
{% endfor %}
</tbody>
</table>
<footer>
<p><a href="index.html">Back to the digests</a> · <a href="papers.json">papers.json</a></p>
<p>{{ ratings_note }}</p>
{% if latest %}<p>Latest digest: {{ latest[:10] }}</p>{% endif %}
</footer>
<script>
(function () {
  var box = document.getElementById("filter");
  var rows = Array.prototype.slice.call(document.querySelectorAll("tbody tr"));
  var chips = Array.prototype.slice.call(document.querySelectorAll(".chip.tag"));
  var activeTag = null;
  function apply() {
    var needle = box.value.trim().toLowerCase();
    rows.forEach(function (row) {
      var text = row.getAttribute("data-text") || "";
      var tags = (row.getAttribute("data-tags") || "").split("|");
      var textOk = !needle || text.indexOf(needle) !== -1;
      var tagOk = !activeTag || tags.indexOf(activeTag) !== -1;
      row.hidden = !(textOk && tagOk);
    });
    chips.forEach(function (chip) {
      var on = chip.getAttribute("data-tag") === activeTag;
      chip.className = on ? "chip tag active" : "chip tag";
    });
  }
  box.addEventListener("input", apply);
  chips.forEach(function (chip) {
    chip.addEventListener("click", function () {
      var tag = chip.getAttribute("data-tag");
      activeTag = activeTag === tag ? null : tag;
      apply();
    });
  });
  apply();
})();
</script>
</body>
</html>
"""


def _render(template: str, **context) -> str:
    env = Environment(autoescape=True)
    return env.from_string(template).render(
        site_title=SITE_TITLE,
        site_description=SITE_DESCRIPTION,
        tldr_label=TLDR_LABEL,
        ratings_note=RATINGS_NOTE,
        markers=rating_markers,
        **context,
    )


def render_index(
    groups: list[tuple[str, list[SiteRecord]]], *, latest_digest_at: str | None
) -> str:
    """The landing page: one section per digest, newest first."""
    return _render(_INDEX_TEMPLATE, groups=groups, latest=latest_digest_at)


def render_all(rows: list[TableRow], *, latest_digest_at: str | None) -> str:
    """The single filterable table, one row per paper."""
    return _render(_ALL_TEMPLATE, rows=rows, latest=latest_digest_at)


def render_json(
    groups: list[tuple[str, list[SiteRecord]]], *, latest_digest_at: str | None
) -> str:
    """`papers.json`: the same records the pages show, as data."""
    payload = {
        "latest_digest_at": latest_digest_at,
        "digests": [
            {
                "digest_at": digest_at,
                "entries": [record.to_json() for record in records],
            }
            for digest_at, records in groups
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=1) + "\n"


def build_site(store, *, domain: str) -> dict[str, str]:
    """Every managed file's content, built from the store in one pass.

    The result depends only on the store and the domain, so building twice
    without new data gives identical files and publish has nothing to commit.
    """
    records = load_records(store)
    latest = latest_digest_at(records)
    groups = group_by_digest(records)
    return {
        "index.html": render_index(groups, latest_digest_at=latest),
        "all.html": render_all(table_rows(records), latest_digest_at=latest),
        "papers.json": render_json(groups, latest_digest_at=latest),
        "style.css": STYLE_CSS,
        "CNAME": f"{domain}\n",
        "README.md": SITE_README.format(domain=domain),
    }
