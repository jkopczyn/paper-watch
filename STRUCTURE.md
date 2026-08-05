# paper-watch

Scan AI-safety paper sources and email yourself a ranked digest a few times a day.

Sources: **arXiv author feeds** (replaces Google Scholar alerts), **RSS newsletters/blogs**,
**RSS-less blogs** watched by diffing their index page's links (alignment.anthropic.com,
www.apolloresearch.ai/science/), **Twitter via Nitter** per-user RSS,
**LessWrong/Alignment Forum via ForumMagnum GraphQL** (tag-filtered posts over a karma bar), and
**Slack** `#papers`-style channels (MATS, FAR, and the
alignment Slack where Aaron Scher collects papers). Papers are deduplicated across sources and ranked by
cross-source overlap, citation/social velocity, and learned reading-group feedback. Each item gets
an LLM-generated TL;DR, topic tags, and links. Previously-shown papers can "resurface" if their
attention surges within a rolling 2–4 week window.

The LLM (Claude) is used **at enrichment time, never per-run at ranking time** — TL;DR, tags, and
a 0-10 safety-relevance score (cached per entry) that gates newsletter/Twitter noise and feeds one
scoring term — plus best-effort metadata recovery for entries the deterministic resolvers can't
crack: vision OCR on scanned-PDF cover pages, web-search title recovery for bare-URL entries, and
a publication-date fallback when a page carries no date metadata. arXiv author-feed items bypass the
gate (the author list is a trusted whitelist) but still get tagged. Slack items bypass the gate when
their channel is marked `trusted` or the link is "obviously a paper" (arXiv / LessWrong / Alignment
Forum / a major-lab safety blog); other Slack links go through the gate like Twitter.

See `method-rec.md` for the source list this is built from.

## Setup

```bash
uv sync                              # install deps
cp .env.example .env                 # add SMTP app password + ANTHROPIC_API_KEY
cp config.example.yaml config.yaml   # seeded with the authors + feeds from method-rec.md
```

`config.example.yaml` already lists the ~50 arXiv authors and the high-confidence newsletter feeds.
Edit `config.yaml` to taste (e.g. set `smtp.to_addr`, tune `scoring` weights, `top_n`).

`paper-watch init` writes a minimal empty config instead, if you'd rather start from scratch.

### Secrets (.env)

- `SMTP_APP_PASSWORD` — a Gmail [app password](https://myaccount.google.com/apppasswords).
- `ANTHROPIC_API_KEY` — used for enrichment. Without it, the digest still runs but papers have no
  TL;DR/tags and the relevance gate is skipped (everything passes).
- `SLACK_TOKEN_*` — one Slack user token (`xoxp-…`) per workspace; see below.
- `OPENREVIEW_USERNAME` / `OPENREVIEW_PASSWORD` — optional; lets the OpenReview resolver log in
  and read login-gated submissions. Without them, gated notes just resolve anonymously (or not at all).

### Slack channels

paper-watch reads `#papers`-style channels via the Slack Web API. For each workspace, create a
user token with the `channels:history`, `groups:history`, and `channels:read` scopes (a Slack app
with a user token, installed to that workspace), and put it in `.env` under the env-var name you
reference from `config.yaml`:

> **Heads up — workspace approval.** A token (bot *or* user) requires creating a Slack app and
> **installing it to the workspace**, which many community Slacks gate behind admin approval. This
> is the real hurdle, not the token type: a user token avoids the per-channel bot-invite step but
> still needs the app installed. Check each workspace's app-management policy — members can
> self-install in some, while others require an admin. To try paper-watch out first, install the
> app in a workspace you control and post a test message with a paper link.

```yaml
slack:
  workspaces:
    - name: mats
      token_env: SLACK_TOKEN_MATS
      ingestion_channels:
        - {id: C0123ABCD, name: papers}
    - name: alignment
      token_env: SLACK_TOKEN_ALIGNMENT
      ingestion_channels:
        - {id: C0789WXYZ, name: aaron-papers, trusted: true}   # bypasses the gate wholesale
      voting_channels:                                          # reading-group poll channels,
        - {id: C0456QRST, name: paper-reading-group}            # scanned by `groundtruth`, not ingested
```

Find channel ids with:

```bash
uv run paper-watch slack-channels --workspace mats   # prints "<id>\t<name>" for each channel
```

Mark a curated channel `trusted: true` to let all its items skip the relevance gate; otherwise only
links on `slack.paper_link_domains` bypass and the rest are gated. The token only needs read scopes
and is never written back to the config.

### Twitter handles (Nitter)

The AGI Safety Core list members page requires a logged-in browser, so handle-seeding is a one-time
assisted step: extract handles into a newline-separated file, then merge them into the config.

```bash
uv run paper-watch seed-handles --from-file handles.txt
uv run paper-watch seed-handles --handle NeelNanda5 --handle EthanJPerez   # or one at a time
```

## Usage

```bash
uv run paper-watch sources           # show how many authors/feeds/handles/slack channels are configured
uv run paper-watch run --dry-run     # fetch + render to out/, don't send
uv run paper-watch run               # fetch, score, email the digest
uv run paper-watch run --since 7d    # override the lookback window

# Weekly reading-group feedback loop:
uv run paper-watch feedback export   # writes candidates.csv of recently-shown papers
#   ...fill in `picked` and a 1-5 `group_rating` (the group's approval) ...
uv run paper-watch feedback import   # records it and tunes per-author/tag/source weights

# Offline eval against reading-group poll votes:
uv run paper-watch groundtruth --workspace alignment   # export poll messages + emoji votes to groundtruth.csv
uv run paper-watch eval                                # recall@N / nDCG of the ranker vs the votes
```

## Scheduling (systemd user timer)

The shipped deployment is the systemd **user** timer in `deploy/systemd/` —
`paper-watch.service` + `paper-watch.timer`, ticking every 4 hours (00, 04, 08, 12, 16, 20
local). Install per `deploy/systemd/README.md`: symlink both units into
`~/.config/systemd/user/`, then
`systemctl --user daemon-reload && systemctl --user enable --now paper-watch.timer`
(and `loginctl enable-linger` so it survives logout). `Persistent=true` fires a missed tick
once on the next boot, and `run` widens its window back to the last completed run, so one
catch-up covers the whole gap.

**Ticking is not delivering.** Every tick ingests; a digest is mailed only when the
`schedule:` key in `config.yaml` says one is due — `deliver_days` + `deliver_at`, by default
local noon on Tuesday and Friday, so Friday's digest covers Wed–Fri and Tuesday's covers
Sat–Tue. A failed send leaves the digest owed, so the following ticks retry it at 16:00,
20:00, and onward until one lands. Needing those retries is why the timer ticks more often
than it mails.

```bash
uv run paper-watch run --force-send   # deliver now, off-schedule
```

A plain crontab also works if systemd isn't an option (use absolute paths; cron has a
minimal environment; `.env` is loaded from the working directory):

```cron
0 */4 * * *  cd /home/jkop/Code/paper-watch && /usr/bin/uv run paper-watch run >> ~/paper-watch.log 2>&1
```

## Development

```bash
uv run pytest
```

Source adapters are tested against recorded fixtures (no live network), and the LLM is mocked.

### Layout

- `sources/` — one adapter per upstream, each yielding `RawItem`s:
  - `arxiv.py` — arXiv export API queried by whitelisted author (batched to dodge rate limits)
  - `rss.py` — RSS/Atom newsletter + blog feeds
  - `page_watch.py` — RSS-less blog index pages, new posts found by diffing the page's link set
  - `twitter_nitter.py` — per-handle Nitter RSS; only yields tweets with a recoverable paper id
  - `graphql.py` — ForumMagnum GraphQL (LessWrong/AF) posts carrying a tag id, over a karma bar
  - `slack.py` — paper links posted in `#papers`-style channels via the Slack Web API
  - `newsletter_links.py` — fans a newsletter body out into the papers it links (one `RawItem` each)
- `sources/` resolvers — fill in metadata for existing entries rather than yield items:
  - `tweet_resolver.py` — expands a bare tweet link via local Nitter (text/links/thread; SQLite-cached)
  - `openreview.py` — OpenReview API title/abstract; optional login for gated notes
  - `pdf_meta.py` — title/abstract from page 1 of a raw PDF (vision OCR only for scanned pages)
  - `html_meta.py` — title/blurb from an HTML landing page's Open Graph / `<title>` metadata
  - `paper_search.py` — S2/Crossref title search to give link-less entries a canonical URL + date
  - `web_search.py` — last-resort Claude web_search recovery for bare-URL entries (key-gated)
  - `date_llm.py` — last-resort LLM read of a page's stated publication date (key-gated)
  - `html_links.py` — shared HTML anchor extraction; `semantic_scholar.py` — citation counts for velocity
- `normalize.py` / `identity.py` — `RawItem` → entry fields; arXiv-ID/DOI extraction and dedup
- `dates.py` — publication-date parsing/normalization to ISO-8601 UTC strings
- `enrich.py` — Claude TL;DR / tags / 0-10 relevance vs the reader profile (cached + versioned per entry)
- `score.py` — relevance + source prior + overlap + velocity + feedback + tracked-author + resurface (pure functions)
- `digest.py` / `delivery/email.py` — HTML render + Gmail SMTP
- `feedback.py` — weekly CSV export/import → EMA feedback weights (also ingests groundtruth votes CSVs)
- `groundtruth.py` — export Slack reading-group poll messages + emoji votes to a CSV
- `eval.py` — offline ranker eval vs poll ground truth (recall@N, nDCG, ingest misses)
- `runtime.py` — the `run` pipeline wiring it all together; `resolve_paper_metadata` routes entries to resolvers
- `nitter_local.py` — ensures the local Nitter instance is up before a real run
- `handles.py` — merges Twitter handles into config.yaml (`seed-handles`)
- `http.py` / `models.py` / `config.py` / `cli.py` — HTTP helper (injectable fetcher), shared dataclasses, YAML config schema, CLI
- `store.py` — SQLite state (entries, entry_urls, mentions, metrics, shown, feedback, feedback_weights, source_state, tweet_cache, meta)
```
