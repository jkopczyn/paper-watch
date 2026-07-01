# paper-watch

Scan AI-safety paper sources and email yourself a ranked digest a few times a day.

## Sources

The specific choices of where to draw are in `config.yaml`.

- arXiv author notices, via RSS feeds
- newsletters and blogs (Substack, etc.), via RSS feeds
- Twitter accounts (largely from JJBalisan's [AGI Safety](https://x.com/i/lists/1185207859728076800) list), via local Nitter feeds
- Slack channels for sharing papers, via app integration (WIP)

A weak LLM is used to do a relevance check (e.g. discarding tweets that are jokes), to tag subtopics, and to add summaries. ArXiv and some Slack channels are 'trusted' and skip relevance checks.

## Configuration

### `config.yaml`

Lists feeds, channels, and sources, categorized, and schedule/setup info.

Sources:

- `authors`: names to follow on arXiv
- `feeds`: name/url pairs for blog/Substack RSS feeds
- `handles`: Twitter accounts to track
- `slack.workspaces`: `name`/`token_env` pairs to specify workspace, channels -> {id, name, (trusted: true)} dicts for channels to watch

Technical Setup:

- `db_path`: local database of results already seen
- `nitter_instances`: primary location (local) and fallbacks
- `smtp`: config for sending emails (without this, only `--dry-run` works)
- `llm`: choice for tagging/summarizing and relevance filter; defaults to none, adding nothing and considering everything relevant

Options:

- `top_n`: number of entries to include in digest (highest-quality N)
- `candidate_window_days`: how recently a paper must be seen to enter the digest as new; also the window over which recent mentions drive the velocity / 'buzz' signal
- `resurface_window_days`: how far back an already-shown paper can be brought back when attention surges (surge measured within the candidate window)
- `schedule`: daily times (cron) to run the digest
- `scoring`: weights for the linear ranking model (the LLM is not a ranking signal). `score = overlap·overlap_norm + velocity·velocity_norm + feedback·feedback_affinity`, plus a flat `resurface_boost` added when a paper is resurfacing.
  - `overlap`: cross-source overlap, `min(distinct_sources, 3)/3`
  - `velocity`: `(citation_growth + new_mentions)` saturated to [0,1) via `raw/(raw+5)`
  - `feedback`: `tanh(sum of learned author/tag/source weights)`, in (−1, 1)
  - `resurface_boost`: flat additive bonus for resurfaced papers
- `llm.max_enrich_per_run`: number of results to apply LLM tagging/enrichment to per run, maximum

### `.env`

For secrets.

- `SMTP_APP_PASSWORD`: Gmail [app password](https://myaccount.google.com/apppasswords) for self-sending emails
- `ANTHROPIC_API_KEY`: Doesn't block digest if missing, but without tags and summaries, and allowing everything through the relevance gate
- `SLACK_TOKEN_*`: One token per workspace; a bot token allowing `channels:read`, `channels:history`, `channels:join`

## Ranking

WIP. Not well-functioning currently.
