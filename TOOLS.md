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
| `groundtruth --workspace NAME [--channel ID] [--since 180d]` | Export the reading-group's weekly **poll** messages + emoji-reaction votes to `groundtruth.csv` (for `eval` and `feedback import`). Also captures each poll's turnout (`attendance` = distinct reactors). Defaults to the workspace's config `voting_channels`; `--channel` overrides. Review/prune before using. |
| `eval [--groundtruth f] [--weights-json '{...}'] [--resolve-tweets]` | Score the ranker's top-N against the poll ground truth: recall@N, nDCG, and which poll papers were never even ingested ("ingest misses"). Offline — never changes behavior. |

## The feedback loop (group votes → ranking)

This is the "give feedback based on group votes" tool. It is a **learning** loop
(it changes future rankings), distinct from `eval` (which only measures).
`feedback import` auto-detects which of two CSV shapes it was given:

```
# Path A — hand-filled candidates CSV:
paper-watch feedback export --since 14d --out candidates.csv
#   → CSV of papers the digest SHOWED, with blank `picked` and `group_rating`
# fill in group_rating (1–5 = the group's approval of that paper) and picked
paper-watch feedback import --file candidates.csv        # week label auto = current ISO week

# Path B — real poll votes, straight from the groundtruth export:
paper-watch feedback import --file groundtruth.csv       # all weeks; --week 2026-W28 filters
```

- **Candidates CSV:** a `group_rating` of 1–5 is centered to [−1, +1]
  (`(rating−3)/2`) and blended by EMA (α=0.3) into that paper's
  **author / tag / source** weights (`feedback_weights` table). Blank ratings are
  recorded but move nothing. Operates over papers the digest **showed**
  (`entries_shown_since`), not raw poll candidates.
- **Votes CSV** (a `groundtruth` export, auto-detected): each poll option is
  resolved to a DB entry (reusing `eval.match_entry`); its vote count, given the
  poll's turnout (captured as distinct reactors, else estimated), maps to a
  target in [−1, +1], which is then scaled by **prediction error** against the
  paper's score at poll time (a paper the ranker already rated highly gets a
  smaller boost but a larger penalty). Weeks are processed chronologically so
  each update sees the feedback learned before it. `--week` here is an optional
  filter (default: all weeks); for the candidates CSV it is the week *label*.
- Those weights feed the `feedback` term of the score, `tanh`-squashed, at rank
  time — so a well-rated paper nudges up future papers by the same authors, tags,
  and source. `w.feedback` is **dynamic**: it starts at 2.0 and ramps toward 4.0
  as weeks of feedback accumulate (`score.dynamic_feedback_weight`, half-life
  10 weeks), recomputed once per run in `select_digest` and in `eval`'s ranking.
- **Status:** the candidates-CSV path has never been exercised. The votes-CSV
  path (2026-07-18) was built to feed the exported reading-group polls in; no
  import against the canonical DB is recorded in PLAN.md yet.

> `groundtruth`+`eval` both draw on the reading group, but do opposite things
> with it: `eval` *measures* the ranker against the polls (read-only);
> `feedback` *trains* it — from your hand-filled ratings, or directly from the
> same groundtruth CSV of poll votes.

## deploy/ scripts (`uv run python deploy/<script>.py`)

One-off maintenance; the `backfill_*.py` scripts dry-run on a DB copy by default
and take `--apply` to write (backing up first). Two exceptions:
**`metadata_repair.py` — its `--set`/`--delete` write to the live DB immediately**
(after a backup), no dry-run; and **`backfill_v2.py` — it opens `config.db_path`
directly with no `--apply`, no dry-run copy, and no backup** (its only guard is
exiting when `ANTHROPIC_API_KEY` is missing). `measure_score_distribution.py` is analysis-only —
it always works on a throwaway copy and never takes `--apply`.

| Script | Purpose | Status |
|---|---|---|
| `metadata_repair.py` | List / show / hand-fix / delete entries that are still just a URL. `--set ID [--title ...] [--abstract/--url/--date]` (no `--title` ⇒ keep current title after a y/N confirm — for attaching metadata to an already-correct title), `--delete ID` (prompts). **Writes live, not dry-run.** | in use (manual fixes + deletes run 2026-07-16); the manual companion to the web-search recovery |
| `backfill_webtitles.py` | Recover URL-only entries' titles via Claude web_search (needs `ANTHROPIC_API_KEY`, loaded from `.env`; `--limit N` caps cost) | applied 2026-07-16 (26/33) |
| `backfill_pubdates_pages.py` | Non-arXiv companion to `backfill_pubdates.py`: re-resolve dateless blog/HTML/PDF entries through the live resolvers, writing **only** `published_at`. Deterministic (HTML meta / JSON-LD / PDF CreationDate) by default; `--llm` adds the Claude date fallback (needs `ANTHROPIC_API_KEY`) | applied 2026-07-22 (`--apply --llm`; 107 dates set, 278 left NULL) |
| `backfill_pubdates.py` | Set `entries.published_at` from the arXiv API | applied 2026-07-15 (97) |
| `backfill_relevance_scale.py` | Remap stored relevance from the old 0–4 rubric onto 0–10 (0/1/2/3/4 → 0/3/5/8/10); gate behavior on existing rows unchanged | **not yet applied** (no `pre-relevance-scale` backup on disk; PLAN.md records no run) |
| `backfill_titles.py` | Re-resolve junk titles through the HTML/PDF resolvers | applied 2026-07-14 |
| `backfill_dedup.py` | Merge duplicate entries (identity/dedup fixes) | applied 2026-07-13 |
| `backfill_v2.py` | Enrichment-v2 / scoring migration. **Writes directly to whatever DB config points at — no `--apply`, no copy, no backup** | historical (quality-score branch; already run against the canonical DB at merge) |
| `measure_score_distribution.py` | Analysis: on a DB copy, rescale relevance to 0–10 then recompute every historical `shown` row's score under the base weights × a candidate multiplier (`[MULT]` arg, default 2.0); reports how much of the distribution escapes [0, 10]. Used to validate the 2026-07-18 weight doubling | analysis-only; rerun as needed |
| `systemd/` | The `paper-watch.{service,timer}` units the timer runs from | — |
| `nitter/` | Self-hosted Nitter deploy tooling (session scripts, compose) | — |
