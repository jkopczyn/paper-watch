# paper-watch

Scan AI-safety paper sources and email yourself a ranked digest a few times a day.

## Sources
The specific choices of where to draw are in config.yaml

- arXiv author notices, via RSS feeds
- newsletters and blogs (Substack, etc.), via RSS feeds
- Twitter accounts (largely from JJBalisan's [AGI Safety](https://x.com/i/lists/1185207859728076800)  list), via local Nitter feeds
- Slack channels for sharing papers, via app integration (WIP)

A weak LLM is used to do a relevance check (e.g. discarding tweets that are jokes), to tag subtopics, and to add summaries. Some sources are 'trusted' and bypass the relevance check and ranking signals. Trusted sources include certain Slack channels, feeds that are major-lab technical teams, etc.

## Configuration
### `config.yaml`
Lists feeds, channels, and sources, categorized, and schedule/setup info

Sources:
- authors: names to follow on arXiv
- feeds: name/url pairs for blog/Substack RSS feeds 
- handles: Twitter accounts to track
- slack.workspaces: `name`/`token_env` pairs to specify workspace, channels -> {id, name, (trusted: true)} dicts for channels to watch

Technical Setup:
- `db_path`: local database of results already seen
- `nitter_instances`: primary location (local) and fallbacks
- smtp: config for sending emails (without this, only `--dry-run` works)
- llm: choice for tagging/summarizing and relevance filter; defaults to none, adding nothing and considering everything relevant

Options:
- top_n: number of entries to include in digest (highest-quality N)
- resurface_window_days: ?maximum? days after first appearance to re-display a previous result if it's 'buzz'-ing
- schedule: daily times (cron) to run the digest
- scoring: params for scoring algorithm
- llm.max_enrich_per_run: number of results to apply LLM tagging/enrichment to per run, maximum


### `.env`
For secrets.
- `SMTP_APP_PASSWORD`: Gmail [app password](https://myaccount.google.com/apppasswords) for self-sending emails
- `ANTHROPIC_API_KEY`: Doesn't block digest if missing, but without tags and summaries, and allowing everything through the relevance gate
- `SLACK_TOKEN_*`: One token per workspace; a bot token allowing `channels:read`,   `channels:history`, `channels:join`


## Ranking

WIP. Not well-functioning currently.
