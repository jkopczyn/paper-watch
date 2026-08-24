# paper-watch

Scan AI-safety paper sources and email yourself a ranked digest a few times a day.

## Sources

The specific choices of where to draw are in `config.yaml`.

- arXiv author notices, via the arXiv export API (batched author queries, Atom responses)
- newsletters and blogs (Substack, etc.), via RSS feeds
- blogs without RSS (alignment.anthropic.com, www.apolloresearch.ai/science/), by diffing the index page's links between runs
- LessWrong's AI tag, via the ForumMagnum GraphQL endpoint (karma-filtered; arXiv linkposts adopt their target URL, so they dedup with the paper itself)
- Twitter accounts (largely from JJBalisan's [AGI Safety](https://x.com/i/lists/1185207859728076800) list), via local Nitter feeds
- Slack channels for sharing papers, via app integration

A weak LLM is used to do a relevance check (scored 0–10 against the reader profile in `profile.md`; e.g. discarding tweets that are jokes), to tag subtopics, and to add summaries. ArXiv and some Slack channels are 'trusted' and skip relevance checks; untrusted items need relevance ≥ 4 to enter the digest.

Before enrichment, entries are resolved to real paper metadata: the arXiv and OpenReview APIs, PDF first-page parsing, and Open Graph / `<title>` extraction for ordinary web pages — so a bare link becomes a titled paper, not anchor text. Entries with no usable link are searched on Semantic Scholar / Crossref by title (`url_search`), and entries that are still just a URL fall back to a Claude web search. Publication dates are pulled deterministically from page metadata (date meta tags, JSON-LD `datePublished`, PDF CreationDate), with a Claude fallback that reads the page's visible text when no date metadata exists; the digest shows an exact date chip when one is known, otherwise a ~estimate from the earliest mention.

## Configuration

### `config.yaml`

Lists feeds, channels, and sources, categorized, and schedule/setup info.

Sources:

- `authors`: names to follow on arXiv
- `feeds`: name/url pairs for blog/Substack RSS feeds
- `graphql`: ForumMagnum GraphQL feeds (LessWrong / Alignment Forum); each `{name, endpoint, tag_id, min_karma, limit}`, keeping tag-matched posts at or above the karma bar
- `pages`: name/url pairs for RSS-less blog index pages, diffed between runs (first run seeds a baseline and reports nothing; add `trusted: true` to skip the relevance gate)
- `handles`: Twitter accounts to track
- `slack.workspaces`: `name`/`token_env` pairs to specify workspace; `ingestion_channels` -> {id, name, (trusted: true)} dicts for channels to watch for paper links, and optional `voting_channels` for reading-group poll channels scanned by `groundtruth`
- `slack.paper_link_domains`: domain allowlist for "obviously a paper" links (arxiv.org etc.) — links to these bypass the relevance gate even from untrusted channels

Technical Setup:

- `db_path`: local database of results already seen
- `nitter_instances`: primary location (local) and fallbacks. The local instance is a self-hosted Docker Nitter (`deploy/nitter/`) that needs valid X session tokens in `sessions.jsonl` — see the session tooling there (`get_session.py`, `add_cookie_session.py`, `create_session_browser.py`); generating sessions needs a human (login + captcha)
- `nitter_min_interval` (default 2.0s): minimum spacing between per-handle Nitter fetches, to avoid 429s
- `lookback` (default 7d): baseline fetch window for a run; the systemd path widens it to cover the gap since the last completed run, and `--since` overrides it
- `smtp`: config for sending emails (without this, only `--dry-run` works)
- `llm`: enrichment model + budget; defaults to `claude-haiku-4-5` with `max_enrich_per_run: 50`. Enrichment is actually disabled by a missing `ANTHROPIC_API_KEY`, not by this key — without the key a passthrough enricher marks everything relevance 5 with no TL;DR/tags

Options:

- `top_n`: number of entries to include in digest (highest-quality N)
- `new_window` (default 4d) / `max_new` (default 20) / `max_resurface` (default 5): the digest leads with up to `max_new` never-shown papers; remaining slots up to `top_n` are padded with at most `max_resurface` resurfacing papers that outscore the new picks' average, so a quiet stretch produces a short digest rather than a rerun of old favourites. `new_window` is only the *fallback* freshness bound — once a digest has been delivered, "new" means "first mentioned since the last successful send", so a paper first seen on Wednesday still leads Friday's email
- `old_after_days` (default 90): a paper published longer ago than this is marked `OLDER · YYYY-MM` and treated as padding even the first time it appears — news to you, but not new, so it shares the `max_resurface` budget instead of taking a lead slot. It gets no resurface boost, and unlike a genuine rerun it doesn't have to outscore the fresh crop (nothing else would ever surface it), but being old never exempts an *already-shown* paper from that bar. Age comes from the publication date the digest shows, whose estimate falls back to first-seen — so an undated old paper can slip through unmarked, but a fresh one is never mislabelled
- `recent_window` (default 14d): window for the "surfaced N×" chip on each digest item — it has to span several digests to say anything
- `candidate_window_days`: bounds the candidate pool — how recently a paper must have been mentioned to be considered at all (entering as *new* additionally requires a first mention within the freshness window); also the window over which recent mentions drive the velocity / 'buzz' signal and the surge test
- `tweet_resolution` (default true): resolve bare tweet links via local Nitter (text, expanded links, quoted tweet, same-author thread) so a trailing arXiv link is recovered
- `newsletter_links` (default true): fan a newsletter item out into the papers it links (allowlisted domains / arXiv / DOI / `.pdf`), keeping the newsletter as provenance
- `resurface_window_days`: how far back an already-shown paper can be brought back when attention surges (surge measured within the candidate window)
- `resurface_min_mentions` (default 2): how many distinct (source, day) mention occasions within the candidate window count as a surge — one post linking a paper three ways is one occasion, not three
- `url_search` (default true): fill link-less entries via Semantic Scholar / Crossref title search
- `alert_after_failures` (default 3): how many runs in a row a source must fail before the digest flags it. A watched page that starts 404ing fails *silently* — it yields nothing, which reads exactly like a blog that hasn't posted — so persistent failures get a banner at the top of the email and a line in `paper-watch run`'s output. The streak is what separates a dead URL (fails every run) from a rate-limit blip (clears on the next); at 4-hourly ticks, 3 is about half a day genuinely down
- `schedule`: `deliver_days` + `deliver_at` (local) — when a digest is mailed. `paper-watch run` is meant to tick more often than that (every 4h under systemd): it ingests on every tick and delivers on the first tick at or after `deliver_at` on a delivery day, retrying on later ticks until a send succeeds. Each digest covers everything since the last successful send, so `[tue, fri]` at noon means Friday's email covers Wed–Fri and Tuesday's covers Sat–Tue
- `scoring`: weights for the linear ranking model. `score = relevance·(llm_relevance/10) + source·source_prior + overlap·overlap_norm + velocity·velocity_norm + feedback·feedback_affinity + author·[tracked author]`, plus a flat `resurface_boost` added when a paper is resurfacing. Defaults target a 0–10 raw score: relevance 4.0, source 2.0, overlap 2.0, velocity 1.0, feedback 2.0, author 1.0, resurface_boost 1.0.
  - `relevance`: LLM 0–10 judgment against `profile.md`, made once at enrichment time and cached (the only LLM-derived ranking input)
  - `source`: best per-source base weight among the entry's mentions, from `source_priors`
  - `overlap`: cross-source overlap, `min(distinct_sources, 3)/3`
  - `velocity`: `(citation_growth + new_mentions)` saturated to [0,1) via `raw/(raw+5)`
  - `feedback`: `tanh(sum of learned author/tag/source weights)`, in (−1, 1). The 2.0 weight is a starting point: it ramps toward 4.0 as weeks of reading-group feedback accumulate (half-life 10 weeks)
  - `author`: 1 if any author is on the `authors` whitelist
  - `resurface_boost`: flat additive bonus for resurfaced papers
- `source_priors`: base weight per source label, longest-prefix matched (`slack:alignment:papers` beats `slack`); defaults: arxiv 0.6, slack 0.8, page 0.6, twitter 0.5, rss 0.4, `rss:OpenAI Blog` 0.1, graphql 0.3, `default` 0.5. NB the `source_priors` block in config.yaml *replaces* these defaults wholesale rather than merging
- `llm.max_enrich_per_run`: number of results to apply LLM tagging/enrichment to per run, maximum

### `.env`

For secrets.

- `SMTP_APP_PASSWORD`: Gmail [app password](https://myaccount.google.com/apppasswords) for self-sending emails
- `ANTHROPIC_API_KEY`: Doesn't block digest if missing, but without tags and summaries, and allowing everything through the relevance gate. Also powers the fallbacks that recover titles for bare-URL entries (web search) and extract publication dates from page text when no date metadata exists
- `SLACK_TOKEN_*`: One **user** token (`xoxp-…`) per workspace, named to match `token_env` in config. Scopes: `channels:history` (ingest), `channels:read` (the `slack-channels` helper; add `groups:read` to also list private channels — it falls back to public-only without it)
- `OPENREVIEW_USERNAME` / `OPENREVIEW_PASSWORD`: Optional OpenReview account (your website login). Set both to read login-gated submissions' abstracts; without them only public notes resolve

### Local state (never checked in — ask an existing user for a copy)

A working install accumulates files that live next to the code but are deliberately
kept out of git. If you are setting up fresh, the code runs without them, but the
learned/curated ones are irreplaceable — ask someone with a running instance:

- `paper_watch.db` — the SQLite store: entries, mentions, enrichment, digest history,
  learned feedback weights, and the readings ledger (what the group has already read,
  which gates digest inclusion). Regenerable only in skeleton form by re-running the
  pipeline; the accumulated history and feedback are not.
- `paper_watch.db.*.bak` — point-in-time backups taken before risky migrations/imports.
- `groundtruth.csv` — hand-curated reading-group poll history (options, votes,
  attendance): the feedback loop's input and the eval's ground truth. The weekly
  refresh appends to it; humans prune misdetected polls and correct votes in place.
  Re-exportable from Slack (`paper-watch groundtruth`), but hand-pruning is lost.
- `groundtruth.csv.imported` — machine-written snapshot from the last successful
  import, used to detect hand edits. Never edit; safe to delete (one edit-detection
  cycle is skipped while it regenerates).
- `config.weekly.yaml` — optional local config variant for the weekly sweep (see below).
- `.env` — secrets, above. `deploy/nitter/sessions.jsonl` — Nitter session tokens.
- `out/` — rendered digest HTML from dry runs.

## Running

### Scheduled runs (the passive path)

The normal mode is a systemd **user timer**: units live in `deploy/systemd/` and are symlinked into `~/.config/systemd/user/`, firing `paper-watch run` at **08:00** and **18:00** local time. `Persistent=true` means a run missed while the machine was off fires once on the next boot — and since `run` widens its fetch window back to the last completed run, that one catch-up covers the whole gap. See `deploy/systemd/README.md` for install steps.

```bash
systemctl --user status paper-watch.timer          # is it active?
systemctl --user list-timers paper-watch.timer     # next + last trigger
journalctl --user -u paper-watch -n 50             # logs from the last runs
```

Failure mode to know: the SMTP app password lives in `.env` as `SMTP_APP_PASSWORD`. If Google revokes it, every run fails at the send step with `SMTPAuthenticationError` 535 — regenerate at <https://myaccount.google.com/apppasswords> and update `.env`.

A failed tick is not silent: `OnFailure=` on the service fires `deploy/systemd/alert.sh`, which appends a line to `paper-watch-alerts.log` (repo root, gitignored), raises a desktop notification, and then tries Slack and email via `paper-watch alert` — each channel best-effort, configured under `alerts:` in `config.yaml`. A digest still undelivered 24h after its slot with every tick exiting cleanly is alerted the same way, once per due point. So: an empty alerts log plus a moving `last_sent_at` is the healthy state.

### Manual runs

```bash
uv run paper-watch run                  # fetch, score, email the digest
uv run paper-watch run --dry-run        # render HTML into out/ instead of sending
uv run paper-watch run --since 7d       # override the fetch lookback
```

`--dry-run` writes `out/digest-<timestamp>.html`, does **not** mark items as shown, and does not advance the last-run watermark — safe to repeat.

### Weekly catch-up digest

For a bigger once-a-week sweep without disturbing the daily config: copy `config.yaml` to `config.weekly.yaml` (the repo keeps one, untracked), and in the copy set `top_n: 20`, `max_new: 20`, `new_window: "168h"`. Then:

```bash
uv run paper-watch run --config config.weekly.yaml --since 7d
```

Add `--dry-run` to render it without sending or marking anything shown.

### Other commands

- `paper-watch init`: write an example `config.yaml` to get started (`--force` to overwrite).
- `paper-watch sources`: summarize what's configured (feed/page/handle/channel counts).
- `paper-watch slack-channels --workspace NAME`: list a workspace's channel ids + names to copy into config.
- `paper-watch seed-handles --from-file handles.txt` (or repeated `--handle`): merge Twitter handles into the config.
- `paper-watch feedback export`: write recently-shown papers to a CSV for picks + 1–5 ratings. `feedback import` reads it back and updates the learned author/tag/source weights; it auto-detects a ground-truth *votes* CSV too, feeding real Slack poll votes into the learning loop (votes scaled by turnout and by prediction error against the paper's current score).
- `paper-watch groundtruth --workspace NAME`: export reading-group poll messages + number-emoji votes to a CSV (scans the workspace's `voting_channels` unless `--channel` overrides). Eyeball and prune before eval.
- `paper-watch eval`: score the ranker's top-N against that ground truth, offline — recall@N, nDCG, and ingest misses. `--weights-json` overrides scoring weights to compare rankers.

## Ranking

The digest is composed in two tiers: up to `max_new` genuinely new papers (never shown, first mentioned within `new_window`) lead, ordered by score; the remaining slots are padded with already-shown papers that are *resurfacing* — mentioned again within `resurface_window_days` **and** surging (≥ `resurface_min_mentions` distinct (source, day) mention occasions inside the candidate window) — when they outscore the new picks' average. Extra new papers beyond `max_new` are dropped for that run.

Each item carries a metadata row: a publication-date chip (exact when known, else a ~estimate), a "surfaced N×" chip over `recent_window`, one chip per source, and a TRUSTED badge when any trusted channel carried it.

Weights are tuned offline against reading-group poll ground truth (`groundtruth` → `eval`); the feedback term learns per-author/tag/source affinities from imported votes and gains influence as feedback accumulates.
