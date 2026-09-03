# Publication-date resolution: fix the `~2026-08` estimates

**Date:** 2026-09-02
**Status:** approved direction (conversation 2026-09-02), pre-plan
**Scope note:** the METR translation dedup (locale URL prefixes) is a separate
diagnosed issue, NOT in this spec. Only dates, plus one small CLI side feature.

## Problem

Many digest items show an estimated date (`~2026-08`) when their pages state a
publication date (2009/2017/2018/2021/2023...). `entries.published_at` stays
NULL for four diagnosed reasons:

1. **LessWrong link-resolution is bot-blocked.** lesswrong.com now serves a
   Vercel "verifying your browser" challenge to the HTML metadata resolver
   (`HtmlMetaResolver`), so link-resolved LW/AF posts (e.g. from Zvi's
   newsletter links) get no title, no abstract, and no date. Confirmed live on
   entry 1504 (Value is Fragile). No parser fix can help; the fetch itself
   fails.
2. **Date extraction is gated behind the abstract gate.** `resolve_paper_metadata`
   (runtime.py) skips any entry that already has an abstract, so entries whose
   feed supplied an abstract never get date extraction at all. Their digest
   date then falls back to the earliest mention's date, and feeds re-syndicate
   old posts with current timestamps (entry 1416, Goodhart Taxonomy, 2018 post,
   AF RSS item dated 2026-08-28).
3. **Dates stated only in the URL path are ignored.** Entries 1405/1403
   (Timaeus) link `/research/2026-05-08-...` and `/research/2026-07-21-...`;
   the pages themselves are unreadable to the resolver but the URL states the
   date.
4. The LLM date fallback (`sources/date_llm.py`, wired into HTML and PDF
   resolvers, key-gated, currently `config.llm.model` = claude-haiku-4-5) only
   runs inside those resolvers, so cases 1-3 never reach it. Where it does run
   it mostly works; model strength is a minor factor, but calls are rare so a
   bump is nearly free.

## Feature 1: LW/AF metadata via ForumMagnum GraphQL

A new resolver for lesswrong.com / alignmentforum.org / mirror-host post URLs
(path `/posts/<id>/<slug>`, id extractable from the canonical URL; mirrors
already collapse to www.lesswrong.com via `canonicalize_url`). It queries the
ForumMagnum GraphQL endpoint (https://www.lesswrong.com/graphql — the same
backend the existing `graphql:` source uses; reuse its HTTP plumbing and
conventions in `sources/graphql.py`) for title, author display names,
`postedAt`, and a plaintext excerpt to serve as the abstract.

Wiring: in `resolve_paper_metadata`, LW post URLs route to this resolver
*before* the generic HTML path (which cannot work anyway). It fills title,
authors, abstract, and published_at via `rewrite_paper_metadata`, same as the
other resolvers.

Effects: link-resolved LW classics get true titles/abstracts and their real
dates (2009 etc.); the bot block stops mattering for LW. Comments links
(`?commentId=`) may resolve to the parent post; acceptable.

## Feature 2: date extraction decoupled from the abstract gate

Entries with an abstract but NULL `published_at` currently never get a date.
Add a date-only resolution pass: for such entries (with an http link), attempt,
in order: URL-date parse (feature 3, free), then the appropriate resolver's
date extraction (GraphQL for LW, HTML meta/JSON-LD, LLM fallback). It must NOT
overwrite existing title/abstract/authors; it only fills `published_at`.

Constraints:
- Must not refetch the same failing page on every 4-hourly tick forever. Track
  attempts (a small schema addition, e.g. an attempts counter or attempted-at
  marker) and stop after a small number of failures.
- Bounded per run (a cap similar in spirit to `max_enrich_per_run`).
- Newest entries first.

## Feature 3: URL-path date parser

A pure function: given a URL, return an ISO date if the path states one.
Patterns: `/YYYY-MM-DD-...`, `/YYYY/MM/DD/`, `/YYYY/MM/` (year-month is
acceptable; `dates.parse_to_iso_date` already validates plausibility). Reject
implausible years (before 1990, more than a month in the future). Runs first in
every date-resolution path (it is free and beats a fetch). Must not misread
arXiv ids (`/2608.14825`) or other number-bearing paths as dates.

## Feature 4: stronger model for the LLM date fallback

`config.llm` gains an optional `date_model` (default: `claude-sonnet-5`),
used by `ClaudeDateExtractor` instead of `config.llm.model`. Date-fallback
calls are rare (only pages whose metadata and URL state no date), so cost is
negligible. Everything else (enrichment, OCR) stays on `config.llm.model`.

## Feature 5: backfill

A `deploy/` script (matching the existing backfill script conventions) that
runs the new date-resolution pass over existing entries with NULL
`published_at` (all of them, newest first, respecting the attempts logic), so
already-ingested entries stop showing `~2026-08`. Report counts: filled via
URL, via GraphQL, via HTML meta, via LLM, unfilled.

## Side feature: resolve-ties comma-separated input

`paper-watch resolve-ties` currently accepts one keypress: N (that option read
and picked) or 0 (all tied options read, none picked). New: a comma-separated
list (e.g. `1,3`) marks exactly those options read and none picked, for polls
where several but not all tied options were read. Rules:

- A single number keeps today's behavior (read + picked).
- A list of two or more: each listed option marked read, nobody picked.
- `0` keeps today's behavior (all tied options read, none picked).
- Validate: indices in range, no duplicates; a list naming every tied option is
  equivalent to `0`.
- `feedback.resolve_tie` grows to accept the list; the interactive prompt moves
  from single-keypress to a short line read. Update the command's docstring and
  the weekly-refresh notice text if it describes the keypress interface.

## Non-goals

- METR locale dedup (separate, unapproved).
- Any scoring/ranking change. (Note one indirect effect: entries that gain a
  real old date will correctly pick up the OLDER marking per `old_after_days`.)
- Re-dating entries whose `published_at` is already set.

## Testing expectations (TDD per repo convention)

Tests precede functionality per milestone. Highest-value cases: URL-date parser
(incl. arXiv-id non-match), the abstract-gate decoupling selection logic and
its attempts cap, GraphQL resolver response parsing (fixture JSON, no network),
resolve_tie list semantics, config default for `date_model`.
