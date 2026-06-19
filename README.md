# paper-watch

Scan AI-safety paper sources and email yourself a ranked digest a few times a day.

Sources (v1): **arXiv author feeds** (replaces Google Scholar alerts), **RSS newsletters/blogs**,
and **Twitter via Nitter** per-user RSS. Papers are deduplicated across sources and ranked by
cross-source overlap, citation/social velocity, and learned reading-group feedback. Each item gets
an LLM-generated TL;DR, topic tags, and links. Previously-shown papers can "resurface" if their
attention surges within a rolling 2–4 week window.

The LLM (Claude) is used **only for enrichment** — TL;DR, tags, and a safety-relevance gate that
filters newsletter/Twitter noise — never as a ranking signal. arXiv author-feed items bypass the
gate (the author list is a trusted whitelist) but still get tagged.

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

### Twitter handles (Nitter)

The AGI Safety Core list members page requires a logged-in browser, so handle-seeding is a one-time
assisted step: extract handles into a newline-separated file, then merge them into the config.

```bash
uv run paper-watch seed-handles --from-file handles.txt
uv run paper-watch seed-handles --handle NeelNanda5 --handle EthanJPerez   # or one at a time
```

## Usage

```bash
uv run paper-watch sources           # show how many authors/feeds/handles are configured
uv run paper-watch run --dry-run     # fetch + render to out/, don't send
uv run paper-watch run               # fetch, score, email the digest
uv run paper-watch run --since 7d    # override the lookback window

# Weekly reading-group feedback loop:
uv run paper-watch feedback export   # writes candidates.csv of recently-shown papers
#   ...fill in `picked` and a 1-5 `group_rating` (the group's approval) ...
uv run paper-watch feedback import   # records it and tunes per-author/tag/source weights
```

## Scheduling (cron)

Run it 1–3×/day. Example crontab (08:00 and 16:00, matching the `schedule:` in config):

```cron
0 8,16 * * *  cd /home/jkop/Code/paper-watch && /usr/bin/uv run paper-watch run >> ~/paper-watch.log 2>&1
```

`crontab -e` to install. Use absolute paths; cron has a minimal environment. The `.env` is loaded
automatically from the working directory.

## Development

```bash
uv run pytest
```

Source adapters are tested against recorded fixtures (no live network), and the LLM is mocked.

### Layout

- `sources/` — arXiv, RSS, Nitter adapters + Semantic Scholar client (each yields `RawItem`s)
- `normalize.py` / `identity.py` — `RawItem` → entry fields; arXiv-ID/DOI extraction and dedup
- `enrich.py` — Claude TL;DR / tags / relevance gate (cached per entry)
- `score.py` — overlap + velocity + feedback + resurface (pure functions)
- `digest.py` / `delivery/email.py` — HTML render + Gmail SMTP
- `feedback.py` — weekly CSV export/import → EMA feedback weights
- `runtime.py` — the `run` pipeline wiring it all together
- `store.py` — SQLite state (entries, mentions, metrics, shown, feedback, weights, cursors)
```
