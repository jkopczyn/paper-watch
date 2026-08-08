# Close the feedback loop: weekly vote import, read-paper exclusion, saner ticks

**Date:** 2026-08-08
**Status:** approved design, pre-implementation

## Problem

The feedback loop has never run in production: `feedback` and `feedback_weights` are
empty in the live DB (and every backup), so `feedback_affinity` is 0 for every paper and
group history has had zero effect on prospective ratings. `groundtruth.csv` is stale
(ends 2026-W29). And nothing anywhere models "the group already read this": the paper
read on 2026-08-05 (arXiv 2607.28607) was ingested on 08-06 via Zvi's roundup and ranked
4th in the 08-07 digest. Separately, every 4-hourly tick polls all sources even though
mail goes out twice a week, and a failed send causes the next tick to re-poll and
rebuild fresh content.

## 1. Weekly Thursday feedback refresh (scheduling)

A second scheduled duty inside the existing 4-hourly `run` tick — no new systemd unit.

- Config (new block):

  ```yaml
  feedback_refresh:
    days: [thu]          # same weekday grammar as schedule.deliver_days
    at: '12:00'          # same HH:MM grammar as deliver_at
    workspace: far       # slack.workspaces name whose voting_channels hold the polls
    groundtruth_path: groundtruth.csv
    exclude_read_weeks: 26   # "read in the last six months" exclusion horizon
  ```

- New `meta` watermark `last_feedback_refresh_at`, advanced only on a successful
  refresh. Dueness reuses the `schedule.py` collapse semantics (same as deliveries):
  "has a refresh moment passed that the last successful refresh did not cover?" —
  missed Thursdays (machine off) collapse to one catch-up run, and a failed refresh
  stays owed so later ticks retry it.
- A refresh = export (§2) then import (§3) then notice email (§3). Export failure
  (missing `SLACK_TOKEN_FAR`, Slack error) aborts the refresh but still sends the
  notice email describing the failure; the watermark does not advance. To avoid a
  4-hourly failure-mail drumbeat while a refresh stays owed, at most one failure
  notice is sent per owed refresh point (tracked via a `meta` key); the eventual
  success sends the normal notice.

## 2. Incremental groundtruth export

`export_groundtruth` currently rewrites the whole CSV, which would clobber the
hand-pruned history. Add an append mode (used by the refresh; the manual
`paper-watch groundtruth` CLI keeps its overwrite behavior unless `--append`):

- Read the existing CSV; `oldest` for the Slack fetch = max `message_ts` present
  (fall back to 180d — the existing CLI default — when the file is empty/missing).
- Append only poll messages whose `message_ts` is not already in the file.
- Never touch existing rows: hand-deletions of misdetected polls stick, because
  covered ranges are not re-fetched. (Vote counts of already-captured weeks are
  therefore frozen at capture time — acceptable: polls conclude within days and
  the refresh runs after the reading.)

## 3. Idempotent vote import, ties, notice email

`import_votes` changes:

- **Idempotence.** A `(entry_id, week)` pair already present in the `feedback` table is
  skipped entirely — no re-record, no weight nudge. A week's votes move the EMA weights
  at most once, so re-running the import weekly (or manually) is safe.
- **Ties.** When a poll's top vote count is shared by 2+ options, none of them is
  marked `picked`, none enters the readings ledger (§5), and the tie is reported in
  the notice email for a human call. (Non-tied polls: winner gets `picked` as today.)
- **Unresolved rows.** Options whose URL doesn't match any DB entry are still recorded
  in the readings ledger when they are winners (§5), and each refresh re-attempts
  resolution of previously unresolved winners, backfilling `entry_id`.
- **Notice email** (every refresh, via the existing SMTP sender): weeks imported, rows
  and weight-key counts touched, unresolved URLs, zero-vote skips, ties awaiting a
  call, and any export/import errors. Subject `paper-watch feedback refresh — <date>`.

## 4. Vote→target model (replaces the v≥2 arm of `votes_to_target`)

Requirements from the group's real signal: a lone vote (nominator only) is a negative
signal that deepens with poll size; a nomination that draws at least one *other*
person's vote is always positive, with a base bonus that grows with attendance.

- **v ≥ 2:**

  ```
  B(a)        = _VT_NOM_BASE + _VT_NOM_SLOPE·a      # 0.125 + 0.0375·a
  target(v,a) = B(a) + (1 − B(a)) · (v − 2) / (a − 2)   # a := max(a, v) as today
  ```

  giving B(2)=+0.2, B(10)=+0.5; v=2 → B(a); a full sweep (v=a) → +1.0; monotone in
  both v and a. Degenerate a≤2 with v=2: target = B(a) (no division). Values are
  clamped to [−1, 1] as today.
- **v = 1:** unchanged — `_VT_LONE_BASE + _VT_LONE_SLOPE·(a−3)` (−0.5 at attendance 3,
  −0.125 per further attendee, ≈−1.0 by attendance 7), clamped.
- **v = 0:** unchanged — `None` (treated as a detection error, not a signal).
- `_VT_AMP`, `_VT_V0_SLOPE`, `_VT_V0_BASE` are deleted. Downstream `_score_scale`
  (prediction-error damping) is unchanged.

## 5. Readings ledger + digest exclusion

New table `readings`: `(id, week, message_ts, url, arxiv_id?, title_norm, entry_id?,
recorded_at)`, unique on `(message_ts, url)`. Populated at import time from each
non-tied poll winner — **even when the URL resolves to no DB entry yet** (the
2607.28607 case: read before first ingest). `arxiv_id`/`title_norm` are derived from
the URL and the poll option's context line via the existing normalize/identity helpers.

Digest candidate selection (`select_digest`) drops any entry matching a reading from
the last `exclude_read_weeks` (26) weeks, matched by `entry_id`, else `arxiv_id`, else
`title_norm`, else exact URL among the entry's URLs. Exclusion is display-only: read
papers still contribute their vote-based weight nudges, and their `feedback` rows
remain.

## 6. Daily poll gate + send-retry semantics

- New `meta` watermark `last_polled_at`, advanced whenever ingest runs. Any tick skips
  ingest when the last poll is <24h old — **except** that a delivery-due tick polls
  first if no poll has happened at/after the owed delivery point.
- **No snapshot logic for failed sends.** A failed send leaves the delivery owed
  (as today); the next tick skips ingest (gate), rebuilds the digest from the unchanged
  DB, and retries. In practice the retry email is "content as of the delivery-point
  poll". If failures persist past the gate (>24h), the tick re-polls and sends fresher
  content. `last_sent_at` records the actual send time, truthfully.
- The systemd timer stays 4-hourly — that is what provides same-day send retries;
  polling frequency is now governed by the gate, roughly once daily off-schedule.

## Rollout (after implementation)

1. Run a fresh append-mode export — fills the stale W30+ gap, capturing the 08-05
   reading's poll.
2. Eyeball the appended rows once (last manual pass before automation takes over).
3. Run one full import over the CSV history: seeds `feedback` + `feedback_weights`
   for the first time, populates the readings ledger, retroactively excludes
   2607.28607 (and other recent readings) from future digests.
4. Confirm the next Thursday tick performs the refresh unattended.

## Testing (TDD; tests before each piece)

- `votes_to_target`: table-driven values incl. B(2)=0.2, B(10)=0.5, sweep→1.0,
  monotonicity, v=1 unchanged, v=0→None, a<v handling, clamps.
- Export append mode: no rewrite of existing rows, dedup by `message_ts`,
  empty/missing-file fallback.
- Import: idempotence (second run is a no-op on weights), tie → no pick/no ledger
  row/reported, unresolved winner → ledger row without `entry_id`, later resolution
  backfill.
- Ledger exclusion: match by each key tier; the late-ingestion case (entry created
  after the reading is recorded); 26-week horizon boundary.
- Scheduling: Thursday dueness + collapse of missed weeks; failed refresh stays owed.
- Poll gate: off-schedule skip within 24h, delivery-due tick forces poll, failed-send
  retry does not re-poll within the gate, >24h failure re-polls.
- One integration test driving fake ticks through refresh + digest end-to-end.
