# Implementation plan: publication-date resolution

**Spec:** `docs/superpowers/specs/2026-09-02-date-resolution.md`
**Date:** 2026-09-02

Ten milestones (M1, M1b, then M2 through M9), one commit each, in order. TDD throughout: each milestone's
tests are written and seen failing before the implementation is written. While
iterating, run the touched test files (`uv run pytest tests/test_dates.py -q`
and so on); before each commit run the full suite (`uv run pytest -q`), because
several milestones change shared call signatures.

The spec's Non-goals section is binding. Nothing here changes scoring, dedup,
or entries whose `published_at` is already set.

---

## Spec assumptions contradicted or under-specified by the code

Read this section before starting. Each item states the deviation and the
choice made in this plan.

1. **Plausibility bounds.** The spec asks the URL-path parser to reject years
   before 1990 and dates more than a month in the future, and says
   `dates.parse_to_iso_date` "already validates plausibility". It does not
   validate the same way: `parse_to_iso_date` rejects years before 1900 and
   anything more than *one day* in the future. A path like `/2026/10/` would be
   accepted by the spec's rule and rejected by `parse_to_iso_date`. Choice:
   `date_from_url` applies the spec's own bounds (year >= 1990, at most 31 days
   in the future) inside itself and builds the ISO string directly, without
   routing through `parse_to_iso_date`. `parse_to_iso_date` is left unchanged,
   so no other caller's behaviour moves.

2. **`HtmlMetaResolver.resolve` cannot serve the date-only pass.**
   `parse_html_meta` returns `None` when it finds no title, and `resolve`
   returns that `None` straight through, so a page with a date meta tag but no
   usable `og:title`/`<title>` (exactly the bot-blocked and script-rendered
   pages the spec is about) yields no date today. Choice: add date-only entry
   points (`parse_html_date`, `HtmlMetaResolver.resolve_date`,
   `PdfMetaResolver.resolve_date`) in M6 rather than reusing `resolve`.

3. **Which entries the date pass covers.** Feature 2 describes entries "with an
   abstract but NULL `published_at`". The abstract is not what matters; an
   entry with no abstract whose earlier resolution failed is in the same
   position, and Feature 5's backfill asks for all NULL-dated entries anyway.
   Choice: the pass selects every entry with `published_at IS NULL`, an http
   link, and fewer than `_MAX_DATE_ATTEMPTS` recorded attempts. This is a
   superset of the spec's set and needs no separate backfill selection logic.

4. **How the date-only pass writes.** Feature 1 says the LW resolver fills
   metadata "via `rewrite_paper_metadata`", which is right for full metadata
   resolution (M3). The date-only pass has no title to write and must not touch
   title/abstract/authors, so it uses the existing `store.fill_published_at`,
   which already sets the column only when it is NULL.

5. **GraphQL single-post query shape (resolved).** An earlier draft of this plan
   marked the single-post query as unverified. It has since been confirmed live
   against `https://www.lesswrong.com/graphql`:
   `post(input: {selector: {_id: $id}}) { result { _id title postedAt
   user{displayName} coauthors{displayName} contents{plaintextDescription} } }`
   works (tested with `EbFABnst8LsidYs5Y`, Goodhart Taxonomy). M2 uses it
   directly; there is no verification step and no introspection fallback left.
   The same draft named the wrong post id for the spec's entry-1504 case: it is
   `GNnHHmm8EzePmKzPk` (`/posts/GNnHHmm8EzePmKzPk/value-is-fragile`), not
   `MrJF3tWiKYMtJepgX`, which returns `app.missing_document`.

6. **The weekly-refresh notice does not describe the keypress interface.**
   `refresh.py` only lists tied polls ("Tie(s) awaiting a human call: ..."). The
   spec's conditional ("if it describes the keypress interface") is therefore
   not triggered, and M9 leaves `refresh.py` alone. Only the CLI docstring and
   the interactive prompt text change.

7. **Attempt tracking and merges.** Attempt state is added as two columns on
   `entries` rather than as a side table. `merge_entries` repoints child tables
   by `entry_id`; a side table would need to be added to that list and to the
   adopted-columns logic. Columns on `entries` disappear with the loser row and
   need no merge handling at all.

8. **LW post URLs have two shapes, and `canonicalize_url` normalizes only one.**
   Besides `/posts/<id>/<slug>`, the forums serve sequence posts as
   `/s/<sequence-id>/p/<post-id>`; twelve undated entries in the current DB use
   that form (for example
   `alignmentforum.org/s/NouBkh4qnK8uKwn3L/p/Z8kLbceGBMWB5HGfn`).
   `identity.canonicalize_url` collapses mirror hosts to `www.lesswrong.com`
   only for paths starting `/posts/`, so a sequence URL keeps its original
   host. `post_id_from_url` therefore checks the host set itself rather than
   canonicalizing first, and matches both path shapes.

9. **`.pdf` detection is broken by query strings.** The existing
   `url.lower().endswith(".pdf")` test in `runtime._entry_pdf_url` and
   `runtime._is_html_page_url` misses PDF links carrying a query string, and
   the undated backlog contains real cases (`mbs.edu ...pdf?rev=...`,
   `iowaattorneygeneral.gov ...pdf?utm_...`). These are routed to the HTML
   resolver today. M1b adds `identity.is_pdf_url` and moves all three call
   sites onto it. This fixes existing routing as well as the new pass, which is
   a small behaviour change outside the spec's four diagnosed causes; it is in
   scope because the date-only pass would otherwise inherit the same bug.

10. **A per-run cap plus newest-first ordering can starve the backlog.** Entries
    the pass cannot resolve are skipped without charging an attempt, so if the
    selection query can return them they refill every slot on every tick and
    the resolvable backlog is never reached. The link test therefore belongs in
    the SQL (M5), not only in the Python loop. Keeping it a query-time
    `EXISTS` over mentions rather than a stored flag also means an entry that
    gains an http mention later becomes eligible again on its own.

    "Cannot resolve" is narrower than "has no http link at all": an entry can
    hold an http value in a `links_json` field the pass never reads, such as a
    `code` link, and it starves the budget in exactly the same way. So the
    query must test the *same defined set of link fields* that
    `_date_candidate_url` reads, not the whole `links_json` document. Both
    derive from one shared `DATE_LINK_FIELDS = ("abstract", "pdf")` tuple; see
    M5 and M7.

11. **"URL date first" has to reach newly ingested entries too.** The spec says
    the URL parse "runs first in every date-resolution path". A pass that only
    runs over the backlog does not satisfy that: a freshly ingested
    no-abstract entry reaches `resolve_paper_metadata` and its HTTP resolver
    before its URL is ever inspected, and the date-only pass can then fetch the
    same URL again later. M3 adds the free URL-date fill inside
    `resolve_paper_metadata`, ahead of resolver scheduling; M7 keeps the pass
    for the backlog.

---

## Design decisions made in this plan

- **Attempt tracking schema:** two columns on `entries`, added through the
  existing `Store._add_column_if_missing` pattern: `date_attempts INTEGER NOT
  NULL DEFAULT 0` and `date_attempted_at TEXT`. Rationale: matches how
  `published_at`, `relevance` and `enrich_version` were added, and needs no
  merge bookkeeping.
- **Attempt cap:** 3 failed attempts, as `runtime._MAX_DATE_ATTEMPTS = 3`.
  Rationale: at roughly one date pass per tick a dead page stops being fetched
  within about half a day, matching `alert_after_failures = 3`.
- **Per-run cap:** `Config.max_date_resolve_per_run: int = 25`, top level, not
  under `llm`. Rationale: the budget bounds *fetches*, most of which never
  reach a model, so it does not belong with the LLM settings.
- **Free URL step never counts as an attempt.** Only a pass that fetched
  something and still found no date increments the counter.
- **Ordering:** newest first by `entries.first_seen_at DESC`, with the
  http-link test applied in the same query so unresolvable entries cannot hold
  the budget (see assumption 10).
- **One list of candidate link fields:** `DATE_LINK_FIELDS = ("abstract",
  "pdf")`, used by both the selection SQL and `_date_candidate_url`. Rationale:
  if the query admits a field the resolver never reads, that entry takes a slot
  on every run and is never charged an attempt.
- **PDF detection:** one shared `identity.is_pdf_url`, testing the URL *path*
  rather than the whole string, used by the existing metadata routing and the
  new pass alike. Rationale: the two must agree, and a bare `endswith(".pdf")`
  misroutes links that carry a query string.
- **`date_model` default:** `claude-sonnet-5`, taken from the spec.
- **New module for the LW resolver:** `src/paper_watch/sources/lesswrong.py`,
  importing the shared helpers from `sources/graphql.py`. Rationale:
  `graphql.py` is a feed source with a `fetch` loop; a per-URL resolver is a
  different shape, and separating them keeps each module's tests focused.

---

## M1: URL-path date parser (spec Feature 3)

**Commit:** `feat(dates): read a publication date stated in a URL path`

### Tests first (`tests/test_dates.py`)

Add a section for `date_from_url`:

- `test_date_from_url_reads_dashed_path_date`: `.../research/2026-05-08-some-post`
  returns `"2026-05-08T00:00:00Z"`.
- `test_date_from_url_reads_slashed_path_date`: `.../2019/03/11/title/`
  returns `"2019-03-11T00:00:00Z"`.
- `test_date_from_url_reads_year_month_path`: `.../2018/07/title` returns
  `"2018-07-01T00:00:00Z"` (day defaults to the 1st, as `parse_to_iso_date`
  already does for "March 2019").
- `test_date_from_url_ignores_arxiv_ids`: `https://arxiv.org/abs/2608.14825`,
  `https://arxiv.org/pdf/2608.14825v2`, and `.../abs/1706.03762` all return
  `None`.
- `test_date_from_url_ignores_bare_numbers`: `.../posts/12345/slug`,
  `.../2026`, `.../id/20260508` (an undelimited digit run) return `None`.
- `test_date_from_url_rejects_implausible_years`: `.../1985/03/11/x` and
  `.../1200-01-01-x` return `None`.
- `test_date_from_url_rejects_far_future_dates`: with `now` injected, a path
  two months ahead returns `None` while one two days ahead is accepted.
- `test_date_from_url_accepts_an_aware_now`: the same near-future path with
  `now=datetime(..., tzinfo=timezone.utc)` gives the same answer as with a
  naive `now`. This guards the naive/aware mixing that the plan's first draft
  had: `datetime(year, month, day)` is naive, and comparing it against an aware
  `now` raises `TypeError`.
- `test_date_from_url_accepts_a_naive_now`: an explicit naive `now` works too.
- `test_date_from_url_rejects_impossible_calendar_dates`: `.../2019/02/31/x`
  and `.../2019-13-01-x` return `None`.
- `test_date_from_url_handles_none_and_non_urls`: `None`, `""`, and a
  non-http string return `None` without raising.
- `test_date_from_url_ignores_the_query_string`: a date only in `?d=2019-03-11`
  is not read (path only).

### Implementation (`src/paper_watch/dates.py`)

Add below `parse_to_iso_date`:

- Constants `_URL_MIN_YEAR = 1990` and `_URL_MAX_FUTURE = timedelta(days=31)`,
  with a one-line comment giving the reason (a URL date is a strong signal but
  a bare number run is not, so the bounds are tighter than
  `parse_to_iso_date`'s).
- Two regexes over the *path only*:
  - `_URL_DASHED = re.compile(r"/(\d{4})-(\d{2})-(\d{2})(?=[-/_.]|$)")`
  - `_URL_SLASHED = re.compile(r"/(\d{4})/(\d{2})(?:/(\d{2}))?(?=[/-]|$)")`
  Both anchor on a leading `/` and require the separators, which is what keeps
  `2608.14825` and `20260508` out.
- `def date_from_url(url: str | None, *, now: datetime | None = None) -> str | None`:
  parse with `urlsplit`, take `parts.path`, try the dashed pattern then the
  slashed one, build `datetime(year, month, day or 1)` inside a
  `try/except ValueError` (this rejects month 13 and February 31), then apply
  the bounds. Return `dt.strftime("%Y-%m-%dT%H:%M:%SZ")`.
- **The comparison base must be normalized the way `parse_to_iso_date` already
  does it**, or a caller passing an aware `now` (which every caller in this
  plan does, since `datetime.now(timezone.utc)` is aware) raises `TypeError` on
  the comparison. Copy that line verbatim:
  `base = (now or datetime.now(timezone.utc)).replace(tzinfo=None)` for a naive
  argument, and `astimezone(timezone.utc).replace(tzinfo=None)` when
  `now.tzinfo is not None`. The clearest form is one helper line reused by both
  functions; if you factor one out, keep `parse_to_iso_date`'s behaviour byte
  for byte and re-run `tests/test_dates.py` in full. The bound checks are then
  `year < _URL_MIN_YEAR or dt > base + _URL_MAX_FUTURE`.
- Docstring: says it is path-only, free, and deliberately conservative.

---

## M1b: PDF detection that survives a query string

**Commit:** `fix(identity): treat a .pdf URL with a query string as a PDF`

Numbered `1b` so the later milestone numbers keep their meaning; it is a full
milestone with its own commit. It comes before M3 because both the existing
metadata routing and the new date-only pass depend on it.

Today three places test for a PDF with a bare `url.lower().endswith(".pdf")`:
`runtime._entry_pdf_url`, `runtime._is_html_page_url`, and (as first drafted)
the date pass's resolver choice. That misses PDF URLs carrying a query string
or fragment, of which the undated backlog holds real examples: an `mbs.edu`
link ending `...pdf?rev=...` and an `iowaattorneygeneral.gov` link ending
`...pdf?utm_...`. Those are currently routed to the HTML resolver, which reads
no metadata from PDF bytes.

### Tests first (`tests/test_identity.py`)

- `test_is_pdf_url_accepts_a_plain_pdf_link`.
- `test_is_pdf_url_accepts_a_pdf_with_a_query_string`: `.../paper.pdf?rev=3`
  and `.../paper.pdf?utm_source=x` are PDFs.
- `test_is_pdf_url_accepts_a_pdf_with_a_fragment`: `.../paper.pdf#page=4`.
- `test_is_pdf_url_is_case_insensitive`: `.../PAPER.PDF`.
- `test_is_pdf_url_rejects_html_pages`: a page whose *query* mentions `.pdf`
  (`.../view?file=paper.pdf`) is not a PDF by this test, since the path is what
  the fetcher will retrieve. State this in the docstring: it is a deliberate
  choice, and such URLs stay on the HTML path as they do today.
- `test_is_pdf_url_handles_none_and_junk`: `None`, `""` and a non-URL string
  give `False` without raising.

### Implementation

- `src/paper_watch/identity.py`: `def is_pdf_url(url: str | None) -> bool`,
  implemented as `urlsplit(url).path.lower().endswith(".pdf")` inside the same
  `try/except ValueError` guard `canonicalize_url` uses. It sits with the other
  URL-shape helpers.
- `src/paper_watch/runtime.py`: `_entry_pdf_url` uses it for the
  abstract-URL test, and `_is_html_page_url` becomes
  `url.startswith(("http://", "https://")) and not is_pdf_url(url)`.
- Re-run `tests/test_runtime.py` in full: this changes routing for any existing
  test URL with a query string.

---

## M2: ForumMagnum single-post resolver (spec Feature 1, parsing half)

**Commit:** `feat(sources): resolve LessWrong/AF post metadata over GraphQL`

### The query shape (verified live, 2026-09-02)

The single-post form below has been confirmed against the live public endpoint
(no auth needed for reads), so there is nothing left to verify at
implementation time:

```
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
```

It was tested with `EbFABnst8LsidYs5Y` (Goodhart Taxonomy, `postedAt`
`2017-12-30T16:38:39.661Z`). The field names inside the selection set also
match `sources/graphql.py`'s `_QUERY`.

Capture the fixture from the entry-1504 case named in the spec:

```
curl -s https://www.lesswrong.com/graphql -H 'content-type: application/json' \
  -d '{"query":"query($id:String!){post(input:{selector:{_id:$id}}){result{_id title postedAt user{displayName} coauthors{displayName} contents{plaintextDescription}}}}","variables":{"id":"GNnHHmm8EzePmKzPk"}}' \
  > tests/fixtures/lesswrong_post.json
```

`GNnHHmm8EzePmKzPk` is "Value is Fragile"
(`/posts/GNnHHmm8EzePmKzPk/value-is-fragile`). Note the id: an earlier draft of
this plan used `MrJF3tWiKYMtJepgX`, which is not that post and returns
`app.missing_document`. Its `postedAt` is `2009-01-29T08:46:30.000Z`, but do
not hard-code that from this document: read the saved fixture and derive every
timestamp assertion from what it actually contains. Also capture a second
fixture from a sequence-form post,
`tests/fixtures/lesswrong_sequence_post.json`, using id `Z8kLbceGBMWB5HGfn`
(reached in the DB as
`alignmentforum.org/s/NouBkh4qnK8uKwn3L/p/Z8kLbceGBMWB5HGfn`). All parsing
tests run against the saved fixtures, so no test touches the network.

### Tests first (`tests/test_lesswrong.py`, new file)

Follow `tests/test_graphql.py` conventions: a `_post(...)` helper building a
response dict, and a `Poster` stub `lambda url, payload: response`.

- `test_post_id_from_url_reads_the_id`: `https://www.lesswrong.com/posts/GNnHHmm8EzePmKzPk/value-is-fragile`
  gives `"GNnHHmm8EzePmKzPk"`.
- `test_post_id_from_url_accepts_mirror_hosts`: the same id from
  `https://www.alignmentforum.org/posts/GNnHHmm8EzePmKzPk/value-is-fragile`,
  `https://www.greaterwrong.com/posts/GNnHHmm8EzePmKzPk/value-is-fragile`, and
  a bare `lesswrong.com` spelling.
- `test_post_id_from_url_reads_the_sequence_form`:
  `https://www.alignmentforum.org/s/NouBkh4qnK8uKwn3L/p/Z8kLbceGBMWB5HGfn`
  gives `"Z8kLbceGBMWB5HGfn"` (the post id, never the sequence id). Cover the
  `www.lesswrong.com` and `greaterwrong.com` spellings of the same shape, and a
  trailing slash.
- `test_post_id_from_url_ignores_non_post_urls`: the LW front page, a tag
  page, a sequence index (`/s/NouBkh4qnK8uKwn3L` with no `/p/` segment), an
  arXiv URL, and `None` all give `None`.
- `test_post_id_from_url_ignores_lookalike_hosts`: a `/posts/<id>/<slug>` path
  on an unrelated host gives `None`.
- `test_post_id_from_url_keeps_comment_links`: a URL with `?commentId=abc`
  still yields the parent post id (the spec accepts resolving to the parent).
- `test_resolve_sends_the_extracted_id_in_the_variables`: capture the payload
  the poster receives (as `test_graphql.test_fetch_maps_posts_and_normalizes_timestamps`
  captures `payloads[url] = payload`) and assert
  `payload["variables"]["id"] == "GNnHHmm8EzePmKzPk"` and that the endpoint is
  `https://www.lesswrong.com/graphql`. A stub that only returns a canned
  response cannot catch a wrong request, so this assertion is required, not
  optional.
- `test_resolve_sends_the_post_id_for_a_sequence_url`: the same assertion for
  the `/s/.../p/...` form, with the sequence-form fixture as the response.
- `test_resolve_returns_title_authors_abstract_and_date`: load
  `tests/fixtures/lesswrong_post.json`; assert the title is whitespace
  collapsed, the authors come from `user`/`coauthors`, the abstract is the
  trimmed `plaintextDescription` truncated to the module cap, and
  `published_at` equals the fixture's own `postedAt` converted to second
  precision with the milliseconds dropped. Derive the expected string from the
  fixture in the test body rather than pasting a literal, so a re-captured
  fixture cannot silently disagree with the assertion.
- `test_resolve_returns_none_for_a_missing_post`: a response whose `result` is
  `None` gives `None` (this is what the endpoint returns for an unknown id,
  alongside an `app.missing_document` error).
- `test_resolve_returns_none_on_graphql_errors`: a body with `errors` gives
  `None` and does not raise.
- `test_resolve_returns_none_for_a_non_post_url`: never posts at all (assert
  the stub was not called).
- `test_resolve_date_returns_only_the_date`: `resolve_date` gives the ISO
  string for a good response and `None` for a missing post.
- `test_resolve_never_raises_on_transport_failure`: a poster that raises gives
  `None`.

### Implementation

`src/paper_watch/sources/graphql.py`:

- Rename `_to_iso_z` to `to_iso_z` and `_authors` to `author_names`, updating
  their two call sites in `parse_posts`. Rationale: the new module needs both
  and cross-module private imports read badly. Nothing outside the package
  imports either name (`tests/test_graphql.py` imports only `_MAX_TEXT_CHARS`
  and `GraphqlSource`), so this rename is contained.

`src/paper_watch/sources/lesswrong.py` (new):

- Module docstring: says lesswrong.com serves a bot challenge to plain HTTP
  metadata fetches, so LW/AF posts are resolved through the same ForumMagnum
  GraphQL backend the `graphql:` feeds use.
- `_ENDPOINT = "https://www.lesswrong.com/graphql"` (mirrors canonicalize to
  this host).
- `_MAX_ABSTRACT_CHARS = 2000`, matching `graphql._MAX_TEXT_CHARS`.
- Two path patterns, because LW serves posts under both:
  - `_POST_PATH = re.compile(r"^/posts/([A-Za-z0-9]{5,})(?:/|$)")`
  - `_SEQUENCE_PATH = re.compile(r"^/s/[A-Za-z0-9]{5,}/p/([A-Za-z0-9]{5,})(?:/|$)")`
  The sequence form captures the **post** id (the `/p/` segment), not the
  sequence id. Twelve undated entries in the current DB use this shape, for
  example `alignmentforum.org/s/NouBkh4qnK8uKwn3L/p/Z8kLbceGBMWB5HGfn`, and
  those post ids resolve normally through the same query.
- `_QUERY`: the single-post query from the section above, with a comment
  recording that the shape was verified live on 2026-09-02.
- `def post_id_from_url(url: str | None) -> str | None`: check the host
  directly against `identity._LESSWRONG_MIRROR_HOSTS` (plus
  `www.lesswrong.com`, which that set omits because the bare spelling covers
  dedup) rather than relying on `canonicalize_url` to normalize the host first.
  `canonicalize_url` only rewrites mirror hosts for paths starting `/posts/`,
  so an `alignmentforum.org/s/.../p/...` URL comes back on its original host
  and a `www.lesswrong.com`-only check would drop every sequence-form URL.
  Parse with `urlsplit`, lowercase the hostname, strip a trailing slash from
  the path, then try `_POST_PATH` and `_SEQUENCE_PATH` in turn. Export the
  host set as a module constant in `identity.py` if reaching for the private
  name reads badly; that rename is contained (grep first).
- `def parse_post(data: dict) -> dict | None`: read `data["data"]["post"]["result"]`
  defensively (`(data.get("data") or {}).get("post") or {}`); return `None` when
  it is missing or has no title. Otherwise return
  `{"title": ..., "authors": author_names(post), "abstract": ...,
  "published_at": to_iso_z(post.get("postedAt"))}`, title whitespace-collapsed
  like `parse_posts` does, abstract stripped and truncated, empty abstract to
  `None`.
- `class LessWrongResolver` with `__init__(self, post: Poster = post_json)`,
  `resolve(self, url) -> dict | None` (returns `None` for non-post URLs, checks
  `data.get("errors")` the way `GraphqlSource.fetch` does, catches every
  exception and logs at debug, never raises), and
  `resolve_date(self, url) -> str | None` returning
  `(self.resolve(url) or {}).get("published_at")`.

---

## M3: Route LW post URLs through the new resolver (spec Feature 1, wiring half)

**Commit:** `feat(runtime): resolve LessWrong posts before the HTML metadata path`

### Tests first (`tests/test_runtime.py`)

Near `test_resolve_paper_metadata_turns_post_into_paper`:

- `test_resolve_paper_metadata_uses_the_lesswrong_resolver_for_lw_posts`:
  an entry whose `links_json` abstract link is an LW post URL, a stub
  `lw_resolver` returning title/authors/abstract/published_at, and a stub
  `html_resolver` that fails the test if called. Assert the entry gains the
  title, authors, abstract and exact `published_at`.
- `test_resolve_paper_metadata_routes_alignment_forum_mirrors_to_the_lw_resolver`
  covers the same routing for an `alignmentforum.org` post URL.
- `test_resolve_paper_metadata_routes_the_sequence_url_form_to_the_lw_resolver`
  covers `alignmentforum.org/s/<seq>/p/<post>`.
- `test_resolve_paper_metadata_reads_a_url_date_before_fetching`: a
  no-abstract entry whose link is `.../research/2026-05-08-post`, with an
  `html_resolver` stub that returns a title and abstract but **no**
  `published_at`. Assert `published_at` is the URL date, and that a following
  `resolve_missing_dates` call with resolvers that fail the test if called does
  nothing (the entry is no longer selected). This is the spec's "runs first in
  every date-resolution path" requirement, and it also stops the date-only pass
  from refetching a URL the pipeline already fetched.
- `test_a_url_dated_entry_never_reaches_the_network_for_its_date`: the
  end-to-end version of the same case through `run_pipeline`, with
  `max_date_resolve` set. (Write this one in M7, where `run_pipeline` gains the
  budget; noted here so it is not lost.)
- `test_resolve_paper_metadata_without_an_lw_resolver_uses_the_html_path`: the
  same LW post URL with `lw_resolver=None` reaches the HTML resolver and its
  metadata lands. (This pins the routing rule, not a within-run fallback: see
  the implementation note below.)
- `test_resolve_paper_metadata_leaves_an_lw_post_unresolved_when_graphql_fails`:
  `lw_resolver.resolve` returns `None` and the HTML resolver is a stub that
  fails the test if called; the entry keeps its original title and abstract.
- `test_resolve_paper_metadata_leaves_non_lw_pages_on_the_html_path`: an
  ordinary blog URL never reaches the LW resolver.

### Implementation

`src/paper_watch/runtime.py`:

- `resolve_paper_metadata` gains a keyword-only `lw_resolver=None`.
- **The free URL date comes first, before any resolver is scheduled.** In the
  per-entry selection loop, right after `abstract_url` is computed and before
  the `openreview`/`lw`/`pdf`/`html` branches, do:

  ```python
  if row["published_at"] is None:
      iso = date_from_url(abstract_url) or date_from_url(_entry_lookup_url(store, row))
      if iso:
          store.fill_published_at(row["id"], iso)
  ```

  The entry may still be scheduled for an HTTP resolver, because it needs a
  title and abstract that the URL cannot supply. What this buys is the spec's
  ordering requirement and one avoided refetch: the entry now has a date, so
  `entries_needing_date` will never select it and the date-only pass will not
  fetch the same URL a second time. `fill_published_at` only writes when the
  column is NULL, so this can never move a date already learned.
- Add `lw_pending: list[tuple[int, str]] = []` and, in the routing chain,
  a branch **before** the pdf/html branches:
  `elif lw_resolver is not None and post_id_from_url(abstract_url): lw_pending.append((entry_id, abstract_url))`
  (import `post_id_from_url` from `paper_watch.sources.lesswrong` at the top of
  the function, alongside the existing local imports).
- Process `lw_pending` in the same loop that handles pdf/html by extending the
  existing `for pending, resolver in (...)` tuple with `(lw_pending,
  lw_resolver)`, placed first. That loop already calls `_safe_resolve` and
  `rewrite_paper_metadata` with `published_at=meta.get("published_at")`, which
  is the wanted behaviour; no new write path is needed.
- Routing rule, deliberately chosen here: an LW post URL goes to the LW
  resolver whenever one is wired, and a GraphQL miss leaves the entry
  unresolved for that run rather than falling through to the HTML resolver.
  The HTML path cannot read those pages anyway (the bot challenge is the whole
  reason for this milestone), so a fallthrough would only spend a fetch. With
  `lw_resolver=None` the existing `elif` chain sends the URL to the HTML
  branch unchanged, which is what the first fallback test pins. If an
  implementer wants a within-run fallthrough after all, raise it rather than
  adding it as an unremarked change.
- `run_pipeline` gains `lw_resolver=None` and passes it to
  `resolve_paper_metadata`; add it to the `if new_ids and (...)` guard's
  disjunction.
- `_build_metadata_resolvers` returns a fourth element, `LessWrongResolver()`
  (unconditional: it needs no key). Update its docstring and the tuple unpack
  in `run` to `openreview_resolver, pdf_resolver, html_resolver, lw_resolver`.
  Check for other call sites with `grep -rn "_build_metadata_resolvers" src
  tests deploy` and update each.
- `run` passes `lw_resolver=lw_resolver` into `run_pipeline`.

---

## M4: `llm.date_model` (spec Feature 4)

**Commit:** `feat(config): a separate, stronger model for the LLM date fallback`

### Tests first

`tests/test_config.py`:

- `test_llm_date_model_defaults_to_sonnet`: a default `LlmConfig` has
  `date_model == "claude-sonnet-5"` and `model == "claude-haiku-4-5"`.
- `test_llm_date_model_is_overridable`: loading a config that sets
  `llm.date_model` keeps the override and leaves `model` alone.

`tests/test_runtime.py`:

- `test_metadata_resolvers_use_the_date_model_for_date_extraction`: with
  `ANTHROPIC_API_KEY` set through `monkeypatch.setenv` and a config whose
  `llm.model` and `llm.date_model` differ, call `_build_metadata_resolvers` and
  assert the HTML and PDF resolvers' `_date_llm.model` is the date model while
  the PDF OCR client's `model` is `llm.model`. (`ClaudeDateExtractor.__init__`
  constructs an `anthropic.Anthropic()` when no client is passed; if that
  raises without real credentials, set a dummy key value and assert only on the
  attribute, or pass through a monkeypatched `anthropic.Anthropic`.)

### Implementation

- `src/paper_watch/config.py`, `LlmConfig`: add
  `date_model: str = "claude-sonnet-5"` with a two-line comment saying date
  fallback calls are rare (only pages whose metadata and URL state no date), so
  a stronger model costs almost nothing.
- `src/paper_watch/runtime.py`, `_build_metadata_resolvers`: build
  `date_llm = ClaudeDateExtractor(config.llm.date_model)`; leave
  `ClaudePdfOcr(config.llm.model)` unchanged. Update the docstring.
- `config.example.yaml`: add a commented `date_model` line under `llm:` if that
  file lists the other `llm` keys; check before editing.

---

## M5: Attempt tracking and the selection query (spec Feature 2, storage half)

**Commit:** `feat(store): track date-resolution attempts per entry`

### Tests first (`tests/test_store.py`)

- `test_entries_needing_date_lists_undated_entries_newest_first`: three
  entries with different `first_seen_at`, one already dated; the dated one is
  absent and the rest come back newest first.
- `test_entries_needing_date_respects_the_limit`.
- `test_entries_needing_date_skips_entries_at_the_attempt_cap`: an entry with
  `date_attempts` at the cap is excluded; one below it is included.
- `test_entries_needing_date_skips_entries_with_no_http_link`: an entry whose
  `links_json` is `{}` and which has no mention URL is absent from the result.
- `test_entries_needing_date_ignores_link_fields_the_pass_never_reads`: an entry
  whose `links_json` is
  `{"abstract": "arXiv:2608.14825", "code": "https://github.com/x/y"}` and which
  has no http mention URL is **absent**. The http value sits in a field
  `_date_candidate_url` never inspects, so admitting it would hand the entry a
  slot on every run, skip it without charging an attempt, and starve the
  backlog exactly as a linkless entry would. This is the case a
  `links_json LIKE '%"http%'` test would wrongly admit, and the `{}`-based tests
  above would not catch.
- `test_entries_needing_date_accepts_a_pdf_only_link`: `{"pdf": "https://.../p.pdf"}`
  with no abstract link is present, since the pass does resolve from the PDF
  field.
- `test_entries_needing_date_counts_a_mention_url_as_a_candidate`: the same
  entry, given one mention whose `source_item_url` is an http URL, is now
  present. This is why the filter is a query-time `EXISTS` rather than a stored
  flag: a later mention must be able to make an entry eligible again.
- `test_entries_needing_date_does_not_let_linkless_entries_starve_the_budget`:
  the regression test for the starvation bug. Insert more than `limit` recent
  entries with no http link at all, plus one older entry with a resolvable
  link; call `entries_needing_date(limit=2, max_attempts=3)` and assert the
  older resolvable entry is returned. Without the query-time filter the recent
  linkless entries occupy every slot, are skipped by the pass without charging
  an attempt, and therefore occupy every slot again on the next tick, forever.
- `test_record_date_attempt_increments_and_stamps`: two calls leave
  `date_attempts == 2` and `date_attempted_at` at the second timestamp.
- `test_fill_published_at_clears_nothing_else`: sanity check that an entry
  filled by the date path keeps its title and abstract (guards the spec's
  "must not overwrite" requirement at the storage layer).
- `test_date_attempt_columns_migrate_onto_an_existing_db`: open a `Store` on a
  path, close it, reopen, and confirm the columns exist exactly once (the
  `_add_column_if_missing` idempotence guard).

### Implementation (`src/paper_watch/store.py`)

- In `_migrate`, after the `published_at` column, add:
  `self._add_column_if_missing("entries", "date_attempts", "INTEGER NOT NULL DEFAULT 0")`
  and `self._add_column_if_missing("entries", "date_attempted_at", "TEXT")`,
  with a comment explaining that a page that states no date must not be
  refetched on every 4-hourly tick forever.
- `def entries_needing_date(self, *, limit: int, max_attempts: int) -> list[sqlite3.Row]`:

  ```sql
  SELECT id, title, links_json, first_seen_at FROM entries
  WHERE published_at IS NULL
    AND COALESCE(date_attempts, 0) < ?
    AND (
      json_extract(links_json, '$.abstract') LIKE 'http%'
      OR json_extract(links_json, '$.pdf') LIKE 'http%'
      OR EXISTS (
        SELECT 1 FROM mentions m
        WHERE m.entry_id = entries.id AND m.source_item_url LIKE 'http%'
      )
    )
  ORDER BY first_seen_at DESC
  LIMIT ?
  ```

  **The query and `_date_candidate_url` must agree on one defined set of link
  fields, and that set is `("abstract", "pdf")`.** Define it once, in
  `runtime.py`, as `DATE_LINK_FIELDS = ("abstract", "pdf")`; the store method
  builds its `json_extract` clauses from that tuple (or, if you would rather
  not have `store.py` import from `runtime.py`, put the tuple in `store.py` and
  have `runtime` import it, but do not write the field names out twice). A
  looser test such as `links_json LIKE '%"http%'` matches an http value in
  *any* field, including a `code` link that `_date_candidate_url` never reads:
  such an entry passes the filter, is skipped without being charged an attempt,
  and takes a limited slot again on the very next run, which is the same
  starvation the filter exists to prevent.

  `json_extract` needs SQLite's JSON1 extension, which is compiled into the
  `sqlite3` module on every supported Python build; if a target build lacks it,
  fall back to matching each field's value with
  `links_json LIKE '%"abstract": "http%'`-style clauses rather than to a
  whole-document `LIKE`, and note the reason in a comment.

  The link condition is not an optimization, it is what stops the budget from
  starving. Entries with no usable http link cannot be resolved, and the pass
  skips them without charging an attempt (correctly: nothing was tried), so if
  they are selectable, enough recent unresolvable entries fill every slot on
  every tick and the resolvable backlog is never reached. Filtering in SQL also
  keeps the eligibility dynamic: an entry that gains an http mention later
  becomes selectable again with no extra bookkeeping.

  Docstring: newest first, why the attempt cap is there, and why the link
  filter belongs in the query.
- `def record_date_attempt(self, entry_id: int, at: str) -> None`:
  `UPDATE entries SET date_attempts = COALESCE(date_attempts, 0) + 1,
  date_attempted_at = ? WHERE id = ?`, then commit.

---

## M6: Date-only entry points on the HTML and PDF resolvers

**Commit:** `feat(sources): date-only resolution for pages without usable titles`

This milestone is what makes assumption 2 above workable: a bot-blocked or
title-less page can still yield a date, and the caller learns which method
produced it (needed for the backfill's per-method counts).

### Tests first

`tests/test_html_meta.py`:

- `test_parse_html_date_reads_meta_without_a_title`: HTML with
  `article:published_time` and no `<title>`/`og:title` returns the ISO date
  (`parse_html_meta` on the same input still returns `None`; assert both).
- `test_parse_html_date_falls_back_to_json_ld`: no date meta, a JSON-LD
  `datePublished` in the body.
- `test_parse_html_date_returns_none_without_any_date`.
- `test_resolve_date_reports_the_meta_method`: a stub fetch returning dated
  HTML; `resolve_date` returns `{"published_at": ..., "method": "html-meta"}`.
- `test_resolve_date_falls_back_to_the_llm_and_reports_it`: undated HTML plus
  a stub `date_llm` returning "March 11, 2019"; result is the normalized ISO
  date with `"method": "llm"`. Assert the extractor received the visible text,
  not raw markup.
- `test_resolve_date_returns_none_when_the_fetch_fails`: a fetch that raises
  gives `None` and does not propagate.

`tests/test_pdf_meta.py`:

- `test_resolve_date_reads_the_pdf_info_date`: `{"published_at": ...,
  "method": "pdf-meta"}` from a fixture PDF that carries a CreationDate (reuse
  whatever fixture the existing `pdf_info_date` tests use).
- `test_resolve_date_falls_back_to_the_llm_over_first_page_text`.
- `test_resolve_date_returns_none_when_the_pdf_cannot_be_fetched`.

### Implementation

`src/paper_watch/sources/html_meta.py`:

- `def parse_html_date(html: str) -> str | None`: run a `_MetaCollector` over
  the HTML (the same three lines `parse_html_meta` uses, wrapped in the same
  `try/except`) and return `_extract_published_at(html, p.meta)`. Refactor
  `parse_html_meta` to call it rather than calling `_extract_published_at`
  directly, so the two paths cannot drift.
- `HtmlMetaResolver.resolve_date(self, url) -> dict | None`: fetch (returning
  `None` on failure, logging at debug like `resolve` does), try
  `parse_html_date`, and on `None` try `safe_llm_date(self._date_llm,
  html_visible_text(html))`. Return `{"published_at": iso, "method":
  "html-meta" | "llm"}` or `None`.

`src/paper_watch/sources/pdf_meta.py`:

- `PdfMetaResolver.resolve_date(self, url) -> dict | None`: fetch the bytes
  (`None` on failure), try `pdf_info_date(data)`, and on `None` try
  `safe_llm_date(self._date_llm, pdf_first_page_text(data))`. Never runs OCR:
  a scanned page's date is not worth a vision call in a bounded per-tick pass.
  Return the same `{"published_at", "method"}` shape with `"pdf-meta"` or
  `"llm"`.

Both docstrings say the method is date-only and never writes or reads a title.

---

## M7: The date-resolution pass (spec Feature 2, runtime half)

**Commit:** `feat(runtime): fill missing publication dates without redoing metadata`

### Tests first (`tests/test_runtime.py`)

Add a section for `resolve_missing_dates`. Stub resolvers throughout; no
network.

- `test_resolve_missing_dates_prefers_the_url_date_and_never_fetches`: an
  entry whose link carries `/2026-05-08-...`; resolvers that fail the test if
  called; the entry ends with that `published_at` and `date_attempts == 0`.
- `test_resolve_missing_dates_uses_the_lesswrong_resolver_for_lw_posts`.
- `test_resolve_missing_dates_uses_the_html_resolver_for_pages`.
- `test_resolve_missing_dates_uses_the_pdf_resolver_for_pdf_links`.
- `test_resolve_missing_dates_routes_a_pdf_with_a_query_string_to_the_pdf_resolver`:
  a link ending `...paper.pdf?rev=3` reaches the PDF resolver, not the HTML
  one (the M1b helper; real examples sit in the undated backlog).
- `test_resolve_missing_dates_routes_sequence_form_lw_urls_to_the_lw_resolver`.
- `test_resolve_missing_dates_does_not_touch_title_abstract_or_authors`: the
  central guarantee: an entry with a title and abstract keeps both exactly,
  even though the resolvers' stubs return different ones.
- `test_resolve_missing_dates_never_overwrites_an_existing_date`: a dated
  entry is not even selected.
- `test_resolve_missing_dates_counts_an_attempt_only_on_failure`: a resolver
  returning no date leaves `date_attempts == 1` and `date_attempted_at` set.
- `test_resolve_missing_dates_stops_at_the_attempt_cap`: after
  `_MAX_DATE_ATTEMPTS` failures the entry is skipped and the resolver is not
  called again.
- `test_resolve_missing_dates_is_bounded_per_run`: 5 candidates, `limit=2`,
  exactly two resolutions attempted.
- `test_resolve_missing_dates_takes_the_newest_entries_first`.
- `test_resolve_missing_dates_reports_counts_by_method`: the returned result
  carries `url`, `graphql`, `html_meta`, `pdf_meta`, `llm` and `unfilled`
  counts.
- `test_resolve_missing_dates_survives_a_raising_resolver`: one bad entry does
  not abort the rest, and it is charged an attempt.
- `test_resolve_missing_dates_skips_entries_with_no_http_link`: such entries are
  filtered out by `entries_needing_date` (M5), so the pass neither fetches for
  them nor charges them an attempt, and they do not consume budget.
- `test_run_pipeline_fills_missing_dates_when_a_budget_is_set`: end-to-end
  through `run_pipeline` with `max_date_resolve=5` and a stub html resolver.
- `test_run_pipeline_skips_date_resolution_when_the_budget_is_zero`.
- `test_a_url_dated_entry_never_reaches_the_network_for_its_date`: the
  end-to-end case promised in M3. Ingest a no-abstract entry whose URL states a
  date, run `run_pipeline` with `max_date_resolve` set, an `html_resolver` that
  returns a title and abstract but no date, and date-pass resolvers that fail
  the test if they are asked for a date. The entry ends with the URL's date and
  no second fetch happened.

### Implementation (`src/paper_watch/runtime.py`)

- `_MAX_DATE_ATTEMPTS = 3` module constant with the rationale comment.
- `@dataclass class DateFillResult` with `url: int = 0`, `graphql: int = 0`,
  `html_meta: int = 0`, `pdf_meta: int = 0`, `llm: int = 0`, `unfilled: int = 0`,
  and a `total_filled` property. Place it beside the other result dataclasses
  at the top of the module.
- `DATE_LINK_FIELDS = ("abstract", "pdf")`: the single definition of which
  `links_json` fields carry a URL this pass will resolve from. M5's
  `entries_needing_date` builds its SQL from the same tuple. Add a comment
  saying the two must not drift: a field admitted by the query but ignored here
  produces an entry that is selected every run, skipped without an attempt
  charge, and never resolved.
- `def _date_candidate_url(store, row) -> str | None`: walk `DATE_LINK_FIELDS`
  in order, returning the first value that starts with `http`; failing that,
  the first mention `source_item_url` that does. Note the difference from
  `_entry_lookup_url`, which reads only the abstract field: this one also
  accepts a PDF-only entry, which is why the field list is explicit rather than
  reusing that helper.
- ```
  def resolve_missing_dates(
      store,
      *,
      limit: int,
      now_iso: str,
      lw_resolver=None,
      html_resolver=None,
      pdf_resolver=None,
      max_attempts: int = _MAX_DATE_ATTEMPTS,
  ) -> DateFillResult
  ```
  Docstring: explains that this is the pass the abstract gate in
  `resolve_paper_metadata` skips, that it writes only `published_at`, and why
  attempts are capped.

  Body, per row from `store.entries_needing_date(limit=limit,
  max_attempts=max_attempts)`:
  1. `url = _date_candidate_url(store, row)`; no URL means `unfilled += 1` and
     `continue` **without** charging an attempt (nothing was tried, and a later
     mention may supply one). The M5 query already excludes these, so this is
     the belt-and-braces case, not the load-bearing one.
  2. `iso = date_from_url(url)`. On a hit: `store.fill_published_at(row["id"],
     iso)`, `result.url += 1`, continue. No attempt charged.
  3. Pick the resolver: `lw_resolver` when `post_id_from_url(url)`,
     else `pdf_resolver` when `identity.is_pdf_url(url)` (the M1b helper, so a
     `...pdf?rev=3` link routes correctly), else `html_resolver`. A
     `None` resolver for the chosen branch means `unfilled += 1` and continue
     without charging an attempt.
  4. Call it inside `try/except Exception` (log a warning, treat as a miss, the
     way `_safe_resolve` does). The LW resolver's `resolve_date` returns a bare
     string; wrap it as `{"published_at": iso, "method": "graphql"}` so all
     three branches return the same shape.
  5. On a date: `fill_published_at`, then increment the counter named by
     `method` (`"html-meta"` to `html_meta`, `"pdf-meta"` to `pdf_meta`,
     `"llm"` to `llm`, `"graphql"` to `graphql`).
  6. On no date: `store.record_date_attempt(row["id"], now_iso)` and
     `result.unfilled += 1`.

- `run_pipeline` gains `max_date_resolve: int = 0` and, after the
  `recover_titles` block and before `enrich_unenriched`, calls
  `resolve_missing_dates` when `max_date_resolve > 0` and at least one of the
  three resolvers is wired. Dates matter to selection (`old_after_days`), so
  this must run before `select_digest`; running it before enrichment costs
  nothing and keeps the metadata work together. It runs on gated ticks too, the
  same way enrichment does, because it works a backlog rather than fresh
  ingestion.
- `run` passes `max_date_resolve=config.max_date_resolve_per_run` and
  `lw_resolver=lw_resolver`.
- `src/paper_watch/config.py`: `Config.max_date_resolve_per_run: int = 25`,
  placed near `top_n`/`max_new`, with a comment saying it bounds fetches per
  tick, most of which never reach a model. Add a `tests/test_config.py` case
  `test_max_date_resolve_per_run_default` in the tests-first step.

---

## M8: Backfill script (spec Feature 5)

**Commit:** `feat(deploy): backfill publication dates for existing entries`

### Tests first (`tests/test_backfill_dates.py`, new file)

Follow `tests/test_backfill_relevance.py`: load the script by path with
`importlib.util` (its `__main__` guard keeps `main` from running on import) and
exercise the pure helper only.

- `test_summary_lines_report_every_method`: a `DateFillResult`-shaped input
  produces lines naming URL, GraphQL, HTML meta, LLM and unfilled counts.
- `test_summary_lines_handle_an_empty_run`.

Keep `main` untested (it touches the DB and the network), matching the existing
backfill scripts.

### Implementation (`deploy/backfill_pubdates_v2.py`)

Name it `backfill_pubdates_v2.py`; `backfill_pubdates.py` (arXiv) and
`backfill_pubdates_pages.py` (HTML/PDF pages) are taken, and this one
supersedes the latter by adding the URL and GraphQL paths.

Module docstring in the house style: says it re-runs the new date-resolution
pass over every entry with a NULL `published_at`, newest first, writing only
`published_at`; dry-run by default on a throwaway copy, `--apply` to write
(backing up to `{db}.pre-pubdates-v2.{stamp}.bak` first), `--llm` to enable the
Claude date fallback, `--limit N` to cap a trial run. Usage line:
`uv run python deploy/backfill_pubdates_v2.py [--llm] [--limit N] [--apply]`.

`main`:

- Copy the argument handling, temp-copy, and backup block from
  `backfill_pubdates_pages.py` verbatim in structure.
- Build `date_llm` from `config.llm.date_model` (not `config.llm.model`) when
  `--llm` and `ANTHROPIC_API_KEY` are both present, printing the same
  "(--llm given but no ANTHROPIC_API_KEY; deterministic only)" note otherwise.
- Build `LessWrongResolver()`, `HtmlMetaResolver(date_llm=date_llm)`,
  `PdfMetaResolver(date_llm=date_llm)`.
- Call `runtime.resolve_missing_dates(store, limit=limit, now_iso=...,
  lw_resolver=..., html_resolver=..., pdf_resolver=...)` with `limit` defaulting
  to a number well above the table size (for example `10**6`) so the whole
  backlog is covered. Sharing the runtime function is the point: the backfill
  must respect the same attempts logic the spec asks for.
- Print `summary_lines(result)` and the usual `APPLIED`/`DRY RUN` footer with
  the `re-run with --apply to write` hint.
- Add a module-level `def summary_lines(result) -> list[str]` so the tests have
  something pure to call.

Note in the docstring that a per-entry progress line is not printed by
`resolve_missing_dates`; if per-entry output is wanted, pass a small
`on_result` callback rather than duplicating the loop.

---

## M9: `resolve-ties` comma-separated input (spec Side feature)

**Commit:** `feat(feedback): let resolve-ties mark several tied options read`

### Tests first

`tests/test_feedback_votes.py`, in the existing tie-resolution section:

- `test_resolve_tie_list_marks_each_listed_option_read_and_none_picked`: a
  3-option tie resolved with `[1, 3]` records two readings, and
  `store.set_feedback_picked` was not called for either (assert via the
  `feedback` rows' `picked` column staying 0).
- `test_resolve_tie_single_element_list_behaves_like_the_bare_number`: `[2]`
  marks option 2 read *and* picked, matching `resolve_tie(..., 2)`.
- `test_resolve_tie_full_list_matches_zero`: listing every tied option records
  the same readings as `0` and picks nobody.
- `test_resolve_tie_rejects_out_of_range_indices`: `pytest.raises(ValueError)`
  for `[0, 2]`, `[4]` on a 3-option tie, and `[-1]`.
- `test_resolve_tie_rejects_duplicates`: `[1, 1]` raises `ValueError`.
- `test_resolve_tie_rejects_an_empty_list`.
- `test_parse_tie_choice_reads_a_bare_number`: `parse_tie_choice("2", 3) == 2`.
- `test_parse_tie_choice_reads_a_comma_list`: `"1,3"` and `" 1 , 3 "` both
  give `[1, 3]`.
- `test_parse_tie_choice_reads_zero`: `"0"` gives `0`.
- `test_parse_tie_choice_rejects_junk`: `""`, `"x"`, `"1,"`, `"1,x"`, `"4"`,
  `"1,1"`, `"0,1"` all raise `ValueError`.

`tests/test_cli.py`, beside `test_resolve_ties_prompts_and_records`:

- `test_resolve_ties_accepts_a_comma_separated_answer`: `CliRunner` with input
  `"1,3\n"` records two readings and picks nobody.
- `test_resolve_ties_reprompts_after_an_invalid_answer`: input `"9\n1\n"`
  ends up recording option 1, and the output contains the error text.

### Implementation

`src/paper_watch/feedback.py`:

- `def parse_tie_choice(raw: str, n_options: int) -> int | list[int]`: strip;
  reject empty. If there is no comma, parse one int, require `0 <= v <=
  n_options`, return it. Otherwise split on commas, require every part to be a
  non-empty digit run, convert, require `1 <= v <= n_options` for each, reject
  duplicates, and return the list. Every failure raises `ValueError` with a
  short message the CLI can print ("options are 0 or 1-3, or a comma-separated
  list like 1,3").
- `resolve_tie(store, tie, choice: int | Sequence[int], *, recorded_at)`:
  - Normalize: a `Sequence` of length 1 becomes its single int, so `[2]` and `2`
    behave identically (the spec's "a single number keeps today's behavior").
  - For an int: unchanged behaviour (`0` marks all read; `N` marks N read and
    picked).
  - For a list of two or more: validate (in range 1..len, no duplicates, non
    empty) raising `ValueError`, mark each listed option read via
    `_record_reading_for`, pick nobody, return the count.
  - Update the docstring to describe all three forms and say why a list picks
    nobody (several papers were read; the poll's pick is genuinely unknown).

`src/paper_watch/cli.py`, `resolve_ties_cmd`:

- Docstring: replace the "one keypress" sentence with a description of the
  three answers (N read and picked; a comma-separated list read, none picked;
  0 for all/none/don't remember).
- Change the option line to
  `"  0: all / none / don't remember (mark every option read)"` followed by a
  hint line `"  N or a list like 1,3 (a list marks those read, none picked)"`.
- Replace `click.prompt(..., type=click.IntRange(...))` with a loop:
  `raw = click.prompt("Which was read", type=str)`, then
  `parse_tie_choice(raw, len(tie.options))` inside `try/except ValueError as
  exc`, echoing `str(exc)` and re-prompting on failure.
- `refresh.py` is not touched (see assumption 6).

---

## Verification before each commit

- `uv run pytest -q` clean.
- M1b changes routing for existing URLs, so run `tests/test_runtime.py` and
  `tests/test_identity.py` in full and read any diff in behaviour rather than
  adjusting an assertion to match.
- M2's fixtures are captured from the live endpoint; if a capture fails, stop
  and report it rather than hand-writing a fixture, since a hand-written one
  would no longer prove the query shape works.
- M3, M4 and M7 change call signatures used by `run`; after each, re-read
  `runtime.run` and `grep -rn "run_pipeline(\|_build_metadata_resolvers(" src
  tests deploy` to confirm no caller was missed.
- M8's script is only exercised by hand: run it dry (`uv run python
  deploy/backfill_pubdates_v2.py --limit 20`) against the working DB copy and
  read the printed counts before considering the milestone done. Do not run it
  with `--apply` as part of the implementation work; that is the user's call.
