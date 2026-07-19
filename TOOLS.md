# paper-watch — tools beyond the core loop

The core loop is `paper-watch run` (fetch → enrich → score → select → email),
fired twice daily by the systemd timer. Everything below is run by hand.

## CLI subcommands (`paper-watch <cmd>`)

| Command | What it does |
|---|---|
| `run [--dry-run] [--since 7d]` | The core loop. `--dry-run` renders the digest to `out/` without emailing; `--since` overrides the lookback window. |
| `init [--path] [--force]` | Write an example `config.yaml`. |
| `sources` | Print counts of configured sources (authors / feeds / pages / handles / Slack). |
| `slack-channels --workspace NAME` | List a Slack workspace's channel ids + names, to paste into config. Needs the workspace token in `.env`. |
| `seed-handles [--from-file f] [--handle h]` | Merge Twitter handles into the config. |
| **`feedback export` / `feedback import`** | **The group-votes learning loop — see below.** |
| `groundtruth --workspace NAME [--channel ID] [--since 180d]` | Export the reading-group's weekly **poll** messages + emoji-reaction votes to `groundtruth.csv` (for `eval`). Defaults to the workspace's config `voting_channels`; `--channel` overrides. Review/prune before using. |
| `eval [--groundtruth f] [--weights-json '{...}'] [--resolve-tweets]` | Score the ranker's top-N against the poll ground truth: recall@N, nDCG, and which poll papers were never even ingested ("ingest misses"). Offline — never changes behavior. |

## The feedback loop (group votes → ranking) — not yet used

This is the "give feedback based on group votes" tool. It is a **learning** loop
(it changes future rankings), distinct from `eval` (which only measures).

```
paper-watch feedback export --since 14d --out candidates.csv
#   → CSV of papers the digest SHOWED, with blank `picked` and `group_rating`
# fill in group_rating (1–5 = the group's approval of that paper) and picked
paper-watch feedback import --file candidates.csv        # week label auto = current ISO week
```

- A `group_rating` of 1–5 is centered to [−1, +1] (`(rating−3)/2`) and blended by
  EMA (α=0.3) into that paper's **author / tag / source** weights
  (`feedback_weights` table). Blank ratings are recorded but move nothing.
- Those weights feed the `feedback` term of the score (`w.feedback=1.0`,
  `tanh`-squashed) at rank time — so rating a paper highly nudges up future papers
  by the same authors, tags, and source.
- It operates over papers the digest **showed** (`entries_shown_since`), not raw
  poll candidates — so it teaches the ranker from what it actually surfaced.
- **Status: never exercised (0 feedback rows).** Running one week's ratings is the
  first real use.

> `groundtruth`+`eval` and `feedback` both draw on the reading group, but do
> opposite things: `eval` *measures* the ranker against the polls (read-only);
> `feedback` *trains* it from your ratings. Neither depends on the other.

## deploy/ scripts (`uv run python deploy/<script>.py`)

One-off maintenance; the `backfill_*.py` scripts dry-run on a DB copy by default
and take `--apply` to write (backing up first). **`metadata_repair.py` is the
exception — its `--set`/`--delete` write to the live DB immediately** (after a
backup); it has no dry-run.

| Script | Purpose | Status |
|---|---|---|
| `metadata_repair.py` | List / show / hand-fix / delete entries that are still just a URL. `--set ID [--title ...] [--abstract/--url/--date]` (no `--title` ⇒ keep current title after a y/N confirm — for attaching metadata to an already-correct title), `--delete ID` (prompts). **Writes live, not dry-run.** | new; the manual companion to the web-search recovery |
| `backfill_webtitles.py` | Recover URL-only entries' titles via Claude web_search | applied 2026-07-16 (26/33) |
| `backfill_pubdates.py` | Set `entries.published_at` from the arXiv API | applied 2026-07-15 (97) |
| `backfill_titles.py` | Re-resolve junk titles through the HTML/PDF resolvers | applied 2026-07-14 |
| `backfill_dedup.py` | Merge duplicate entries (identity/dedup fixes) | applied 2026-07-13 |
| `backfill_v2.py` | Enrichment-v2 / scoring migration | historical (quality-score branch) |
| `systemd/` | The `paper-watch.{service,timer}` units the timer runs from | — |
| `nitter/` | Self-hosted Nitter deploy tooling (session scripts, compose) | — |
