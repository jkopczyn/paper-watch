# Implementation plan: close the feedback loop

**Spec:** `docs/superpowers/specs/2026-08-08-feedback-loop-design.md`
**Date:** 2026-08-08

Six milestones, one commit each, in order. TDD throughout: each milestone's
tests are written and seen failing before the implementation. Test commands
while iterating: `uv run pytest tests/test_feedback_votes.py -q` etc.; before
each commit, run the whole files touched (`tests/test_feedback_votes.py
tests/test_groundtruth.py tests/test_store.py tests/test_runtime.py
tests/test_schedule.py tests/test_config.py tests/test_cli.py` as relevant).

---

## M1 — `votes_to_target` revision (spec §4)

**Commit:** `feat(feedback): attendance-scaled nomination bonus in votes_to_target`

### Files / functions

- `src/paper_watch/feedback.py`
  - Delete `_VT_AMP`, `_VT_V0_SLOPE`, `_VT_V0_BASE`.
  - Add `_VT_NOM_BASE = 0.125`, `_VT_NOM_SLOPE = 0.0375` next to the surviving
    `_VT_LONE_*` constants, with a comment giving the anchor points
    (B(2)=+0.2, B(10)=+0.5).
  - Rewrite `votes_to_target(votes: int, attendance: float) -> float | None`:
    - `votes <= 0` → `None` (unchanged).
    - `a = max(float(attendance), float(votes))` (unchanged).
    - `votes == 1` → `_VT_LONE_BASE + _VT_LONE_SLOPE * (a - 3)`, clamped
      (unchanged — note: the current code takes `min(linear_target, lone)`;
      the new v=1 arm is the lone formula *alone*, since the linear arm is
      gone. See "conflicts" below.)
    - `votes >= 2`: `b = _VT_NOM_BASE + _VT_NOM_SLOPE * a`; if `a <= 2` (which
      with the max() clamp means `votes == 2 and a == 2`) → `b`; else
      `b + (1 - b) * (votes - 2) / (a - 2)`. Clamp to [-1, 1].
  - Update the module-level comment block above the constants to describe the
    new model (base bonus growing with attendance; sweep → +1).
  - `poll_attendance` and `_score_scale` unchanged.

### Tests first (`tests/test_feedback_votes.py`)

Replace `test_votes_to_target_matches_brisk_week_sketch` and
`test_votes_to_target_matches_slow_week_sketch` (they encode the deleted
model) with a table-driven set:

- `test_votes_to_target_zero_is_none` — keep as-is (still passes).
- `test_votes_to_target_nomination_base_anchors` — `votes_to_target(2, 2) ==
  pytest.approx(0.2)`; `votes_to_target(2, 10) == pytest.approx(0.5)`;
  degenerate `votes_to_target(2, 1)` (a clamped to 2) `== approx(0.2)` with no
  ZeroDivisionError.
- `test_votes_to_target_full_sweep_is_plus_one` — `votes_to_target(7, 7) ==
  approx(1.0)`; also `votes_to_target(10, 4)` (a := 10 via the max clamp) `==
  approx(1.0)`.
- `test_votes_to_target_interpolates_between_base_and_sweep` — e.g. v=6, a=10:
  `B(10) + 0.5*(1-B(10)) = 0.75`.
- `test_votes_to_target_monotonic_in_votes` — keep/extend: for a=8, targets
  strictly increase over v=2..8; and lone (v=1) below v=2.
- `test_votes_to_target_monotonic_in_attendance_for_v2` — B(a) increasing:
  v=2 targets increase over a=2..12.
- `test_votes_to_target_lone_vote_unchanged` — v=1: −0.5 at a=3, −0.625 at
  a=4, clamped at −1.0 by a≥7.
- `test_votes_to_target_clamped_to_unit_interval` — extreme inputs never leave
  [−1, 1].

`test_import_votes_counts_and_weight_directions` asserts weight *directions*
(winner positive, lone vote negative) — expected to survive; verify, don't
silently loosen.

### Risks / notes

- Downstream `import_votes` calls are numerically affected (targets differ),
  but no test pins exact weight values, only signs. Re-run
  `tests/test_feedback_votes.py` in full.

---

## M2 — groundtruth export append mode + CLI `--append` (spec §2)

**Commit:** `feat(groundtruth): append-mode export that never rewrites captured rows`

### Files / functions

- `src/paper_watch/groundtruth.py`
  - New helper `_existing_rows(path: Path) -> tuple[list[dict], set[str]]` —
    reads the CSV if present, returns (raw rows as dicts, set of
    `message_ts`). Missing/empty file → `([], set())`.
  - Change `export_groundtruth(token, channel_ids, *, oldest, path,
    fetch=slack_history, min_options=2, append: bool = False) -> int`:
    - When `append` and the file has rows: override `oldest` with
      `max(message_ts present)` (as a raw ts string — Slack's `oldest` param
      takes epoch-seconds strings, which these are). When the file is
      empty/missing, keep the caller-supplied `oldest` (the CLI's 180d
      default).
    - Fetch as today; drop any parsed option whose `message_ts` is already in
      the existing set (belt-and-braces: Slack's `oldest` is exclusive by
      default, but dedup makes the behavior independent of that).
    - `append=True`: open with `"a"`, write header only when the file was
      missing/empty, write only the new rows, sorted by (ts, option).
      Existing rows are never re-read-and-rewritten. Return count of *new*
      rows.
    - `append=False`: byte-for-byte today's behavior.
- `src/paper_watch/cli.py`, `groundtruth_cmd`
  - Add `--append` flag (`is_flag=True`): pass `append=True` through; keep
    the `--since`-derived `oldest` as the fallback. Echo line distinguishes
    "Appended N new poll option(s)" from the overwrite message.

### Tests first (`tests/test_groundtruth.py`)

- `test_export_append_only_adds_new_polls(tmp_path)` — write a CSV with one
  poll (ts=T1), fake `fetch` returning polls T1 and T2; append; file still has
  exactly one T1 row block (byte-identical original lines) plus T2's rows;
  return value counts only T2's options.
- `test_export_append_dedups_by_message_ts(tmp_path)` — fetch returns only an
  already-present ts; append writes nothing, returns 0, file unchanged.
- `test_export_append_uses_max_ts_as_oldest(tmp_path)` — capture the `oldest`
  the fake fetch receives; equals the max ts in the file.
- `test_export_append_falls_back_when_file_missing(tmp_path)` — no file:
  caller's `oldest` is passed through, header is written, rows land.
- `test_export_append_preserves_hand_deletions(tmp_path)` — file contains
  poll T2 but *not* T1 (T1 < T2, hand-pruned); oldest = T2's ts, so T1 is
  never re-fetched (assert the fake fetch got T2's ts) and stays absent.
- CLI: in `tests/test_cli.py`, `test_groundtruth_append_flag_passes_through`
  — invoke with `--append` and a monkeypatched `export_groundtruth`, assert
  `append=True` and the existing `oldest` fallback still computed from
  `--since`.

### Risks / notes

- Column order of appended rows must match the existing header exactly;
  reuse the same `fieldnames` list for both modes.
- A hand-deleted *option row* of a still-present poll ts stays deleted
  (dedup is by ts, and covered ranges aren't re-fetched) — matches spec.

---

## M3 — readings ledger + store methods + import idempotence/ties/ledger (spec §3, §5 import side)

**Commit:** `feat(feedback): idempotent vote import with tie handling and a readings ledger`

### Store (`src/paper_watch/store.py`)

- SCHEMA: new table (with a design-rationale comment in the docstring style of
  `entry_urls`/`source_health` — the 2607.28607 read-before-ingest case):

  ```sql
  CREATE TABLE IF NOT EXISTS readings (
      id          INTEGER PRIMARY KEY,
      week        TEXT NOT NULL,
      message_ts  TEXT NOT NULL,
      url         TEXT NOT NULL,
      arxiv_id    TEXT,
      title_norm  TEXT,
      entry_id    INTEGER REFERENCES entries(id) ON DELETE SET NULL,
      recorded_at TEXT NOT NULL,
      UNIQUE(message_ts, url)
  )
  ```

  Note: `entry_id` is nullable and *not* CASCADE — a reading is a historical
  fact that must survive an entry merge/delete. But **do** add `readings` to
  the repoint list in `merge_entries` so a merge keeps the ledger pointing at
  the survivor (see conflicts).
- New methods:
  - `has_feedback(self, entry_id: int, week: str) -> bool`
  - `record_reading(self, *, week, message_ts, url, arxiv_id, title_norm,
    entry_id, recorded_at) -> None` — `INSERT ... ON CONFLICT(message_ts, url)
    DO UPDATE SET entry_id = COALESCE(excluded.entry_id, readings.entry_id)`
    so re-import is idempotent and a later resolution backfills.
  - `unresolved_readings(self) -> list[sqlite3.Row]` — `entry_id IS NULL`.
  - `set_reading_entry(self, reading_id: int, entry_id: int) -> None`.
  - `readings_since(self, message_ts_min: str) -> list[sqlite3.Row]` —
    readings whose poll time is at/after the cutoff. `message_ts` is an
    epoch-seconds string; compare numerically (`CAST(message_ts AS REAL) >= ?`)
    — lexicographic compare is wrong for epoch strings of differing length.

### Feedback (`src/paper_watch/feedback.py`)

- Extend `VoteImportResult`:

  ```python
  @dataclass
  class VoteImportResult:
      imported: int = 0
      skipped_zero: int = 0
      skipped_existing: int = 0
      unresolved: int = 0
      weeks: list[str] = field(default_factory=list)
      weight_keys_touched: int = 0
      unresolved_urls: list[str] = field(default_factory=list)
      ties: list[str] = field(default_factory=list)   # week labels awaiting a call
      readings_recorded: int = 0
      resolutions_backfilled: int = 0
  ```

  (M5's notice email consumes this; adding it now keeps M5 mechanical.)
- `import_votes` changes:
  - **Ties:** per poll, compute the top vote count; if 2+ options share it,
    mark the poll tied — no option gets `picked`, no ledger row, week appended
    to `result.ties`. Non-tied: winner logic as today.
  - **Ledger:** for each non-tied poll winner row (by CSV row, *before*
    entry resolution), derive `arxiv_id = extract_arxiv_id(f"{url} {context}")`
    and `title_norm = normalize_title(context)` (only when
    `is_distinctive_title` passes, else NULL — a context line like a bare URL
    must not become an exclusion key), and `record_reading(...)` with
    `entry_id` = the resolved id or None. Count into `readings_recorded`.
  - **Idempotence:** after `match_entry`, skip a row whose `(entry_id, week)`
    already has a `feedback` row (`store.has_feedback`) — no `record_feedback`,
    no `_apply_target`; count into `skipped_existing`. (The ledger upsert
    still runs — it's a no-op on conflict.)
  - **Resolution backfill:** at the start of `import_votes`, walk
    `store.unresolved_readings()`, re-attempt `match_entry` on a synthetic
    `GroundTruthRow(url=r["url"], context=..., ...)` (or factor a small
    `_resolve_reading(store, url, arxiv_id, title_norm)` that tries
    `get_entry_by_arxiv_id` / `get_entry_id_by_mention_url(canonicalize_url(url))`
    / `get_entry_by_source_url` / `get_entry_by_title_norm`), and
    `set_reading_entry` on a hit; count into `resolutions_backfilled`.
  - Track `weeks` (distinct, sorted) and `weight_keys_touched` (len of the set
    of `(key_type, key_value)` passed to `set_feedback_weight` — have
    `_apply_target` return the keys it touched).
  - Populate `unresolved_urls` alongside the `unresolved` counter.
- `import_file`'s votes summary line extends to mention ties/skips.

### Tests first

`tests/test_store.py`:

- `test_record_reading_upsert_backfills_entry_id(tmp_path)` — insert with
  entry_id=None, re-insert same (ts, url) with an id → single row, id set;
  re-insert again with None → id kept.
- `test_readings_since_compares_ts_numerically(tmp_path)` — a 9-digit and a
  10-digit epoch ts sort correctly.
- `test_merge_entries_repoints_readings(tmp_path)`.
- `test_has_feedback(tmp_path)`.

`tests/test_feedback_votes.py`:

- `test_import_votes_is_idempotent(tmp_path)` — run twice; second run:
  `imported == 0`, `skipped_existing > 0`, and every feedback weight
  byte-identical to after the first run.
- `test_import_votes_tie_no_pick_no_ledger(tmp_path)` — poll with two options
  at the top count: neither feedback row has `picked=1`, no readings row for
  that poll, week in `result.ties`. (Votes still recorded and weights still
  nudged for tied options — spec only removes the pick/ledger, not the
  signal.)
- `test_import_votes_winner_enters_ledger(tmp_path)` — non-tied poll: exactly
  one readings row, with entry_id, arxiv_id, week.
- `test_import_votes_unresolved_winner_still_ledgered(tmp_path)` — winner URL
  matching nothing: readings row exists with `entry_id IS NULL`, url in
  `result.unresolved_urls`.
- `test_import_votes_backfills_resolution_on_next_run(tmp_path)` — import with
  unresolved winner; then seed the entry (as ingest would); import again →
  ledger row's entry_id filled, `resolutions_backfilled == 1`.
- `test_import_votes_reports_weeks_and_weight_keys(tmp_path)`.

### Risks / notes

- Ordering: the backfill walk must run against the *current* DB before new
  rows are processed, but the order doesn't otherwise matter.
- `record_feedback` keeps its upsert (the manual candidates path still uses
  it); idempotence lives in `import_votes`, per spec.

---

## M4 — digest exclusion of read papers (spec §5, digest side)

**Commit:** `feat(digest): exclude recently-read papers from selection`

### Files / functions

- `src/paper_watch/store.py`: `get_entry_urls(self, entry_id: int) ->
  list[str]` (select from `entry_urls`).
- `src/paper_watch/runtime.py`:
  - New module-level helper:

    ```python
    def _read_exclusion_index(store, message_ts_min: str) -> tuple[
        set[int], set[str], set[str], set[str]]:
        """(entry_ids, arxiv_ids, title_norms, urls) read within the horizon."""
    ```

    Built from `store.readings_since(...)`; urls canonicalized via
    `canonicalize_url`.
  - `select_digest(...)` gains `exclude_read_since: str | None = None`
    (an ISO instant; converted to an epoch-seconds string for
    `readings_since` — add a tiny `_iso_to_epoch_str` or reuse
    `paper_watch.sources.slack.iso_to_ts`). When set, build the index once and
    drop a candidate when, in tier order: `entry_id` in ids; `row["arxiv_id"]`
    in arxiv_ids; `row["title_norm"]` in title_norms; any of
    `store.get_entry_urls(entry_id)` (canonicalized) in urls. Placed right
    after the gate check, before scoring.
  - `run_pipeline(...)` gains `exclude_read_weeks: int | None = None`;
    computes `exclude_read_since = (now - timedelta(weeks=exclude_read_weeks))`
    when set, passes through to `select_digest`.
  - `run(...)` passes `config.feedback_refresh.exclude_read_weeks` when the
    block is configured — **but the config block arrives in M5**. To keep the
    milestone order, M4 wires `run_pipeline`/`select_digest` only and defaults
    to `None` (feature off in the live path until M5 lands the config). Note
    this explicitly in the commit message.

### Tests first (`tests/test_runtime.py`)

Helper `_record_reading(store, **kw)` local to the test file.

- `test_a_read_paper_is_excluded_by_entry_id(tmp_path)` — seed + reading with
  entry_id; `select_digest(..., exclude_read_since=...)` omits it; without the
  kwarg it is selected (exclusion is opt-in).
- `test_a_read_paper_is_excluded_by_arxiv_id(tmp_path)` — reading has
  entry_id NULL but arxiv_id matching the entry (the late-ingestion
  2607.28607 case: entry created *after* the reading).
- `test_a_read_paper_is_excluded_by_title_norm(tmp_path)` — same, arxiv_id
  NULL, title_norm matches.
- `test_a_read_paper_is_excluded_by_url(tmp_path)` — match only via
  `entry_urls`.
- `test_a_reading_older_than_the_horizon_does_not_exclude(tmp_path)` —
  reading message_ts 27 weeks back, horizon 26: entry selected. Boundary:
  exactly at the cutoff → excluded (>= semantics).
- `test_exclusion_is_display_only(tmp_path)` — the excluded entry's feedback
  rows / weights are untouched by selection (trivial but pins the spec line).

### Risks / notes

- `select_digest` iterates every active entry; `get_entry_urls` per candidate
  is only hit when the cheaper tiers miss and the url set is non-empty —
  guard with `if urls:` to keep the common path free of extra queries.

---

## M5 — weekly feedback refresh scheduling + notice email + config (spec §1, §3 email)

**Commit:** `feat(refresh): weekly Thursday feedback refresh with notice email`

### Config (`src/paper_watch/config.py`)

```python
class FeedbackRefreshConfig(BaseModel):
    """Weekly export→import of reading-group poll votes, run inside the
    ordinary tick (no extra systemd unit). <rationale docstring>"""
    days: list[str] = Field(default_factory=lambda: ["thu"])
    at: str = "12:00"
    workspace: str = "far"
    groundtruth_path: str = "groundtruth.csv"
    exclude_read_weeks: int = 26

    @property
    def weekdays(self) -> set[int]: ...   # parse_weekdays(self.days)
    @property
    def at_time(self) -> time: ...        # parse_deliver_at(self.at)
    # field_validators mirroring ScheduleConfig
```

`Config` gains `feedback_refresh: FeedbackRefreshConfig | None = None`
(absent ⇒ feature off; also add the block, commented out, to
`cli.EXAMPLE_CONFIG`).

### Store (`src/paper_watch/store.py`)

- Constants + accessors in the LAST_RUN/LAST_SENT style:
  - `LAST_FEEDBACK_REFRESH_KEY = "last_feedback_refresh_at"` /
    `get_last_feedback_refresh_at` / `set_last_feedback_refresh_at`.
  - `FEEDBACK_FAILURE_NOTICED_KEY = "feedback_failure_noticed_for"` — stores
    the ISO of the owed refresh point whose failure has already been mailed;
    plain `get_meta`/`set_meta` is fine, no dedicated accessors needed.

### New module `src/paper_watch/refresh.py`

Rationale docstring; contents:

- `@dataclass RefreshResult: performed: bool; ok: bool; summary: str;
  notice_sent: bool` (whatever the tests need — keep minimal).
- `is_refresh_due(now, last_refresh_at, *, days, at) -> bool` — thin wrapper
  over `schedule.is_delivery_due` (same collapse semantics, different
  watermark). Owed point via `schedule.last_delivery_at_or_before`.
- `run_feedback_refresh(store, config, sender, *, now, export=..., importer=...)
  -> RefreshResult`:
  1. Resolve workspace/token exactly as `cli.groundtruth_cmd` does (workspace
     by name, `voting_channels`, token from env). Missing token / no
     channels / Slack error ⇒ export failure.
  2. Export: `export_groundtruth(token, channel_ids, oldest=<180d fallback>,
     path=cfg.groundtruth_path, append=True)`.
  3. Import: `import_votes(store, path=..., config=config)` →
     `VoteImportResult`.
  4. Compose + send the notice email via `sender.send(subject=f"paper-watch
     feedback refresh — {now:%Y-%m-%d}", html=...)`: weeks imported, rows /
     weight-key counts, unresolved URLs, zero-vote skips, ties, any error. A
     small `render_notice(result | error) -> str` helper, plain HTML.
  5. Success (export+import ran): `store.set_last_feedback_refresh_at(now_iso)`
     and clear/ignore the failure-notice key. Failure: watermark untouched;
     send the failure notice only if `get_meta(FEEDBACK_FAILURE_NOTICED_KEY)`
     differs from this owed point's ISO, then set it.
  - The notice email failing to send on an otherwise-successful refresh:
    advance the watermark anyway (the refresh succeeded; spec ties the
    watermark to the refresh, not the mail) — but log. Flagged below.

### Wiring (`src/paper_watch/runtime.py::run`)

After the pipeline (so a refresh failure can never block the digest), when
`config.feedback_refresh` is set and not `dry_run`:

```python
if is_refresh_due(now, store.get_meta(LAST_FEEDBACK_REFRESH_KEY), days=..., at=...):
    run_feedback_refresh(store, config, sender, now=now)
```

Also: `run` now passes `exclude_read_weeks=config.feedback_refresh.
exclude_read_weeks if config.feedback_refresh else None` into `run_pipeline`
(completing M4's wiring). `RunResult` gains `refreshed: bool = False` and the
CLI `run` command echoes a one-liner when a refresh ran.

### Tests first

`tests/test_config.py`:

- `test_feedback_refresh_block_parses` / `..._defaults` /
  `..._rejects_bad_day`.

New `tests/test_refresh.py`:

- `test_refresh_due_thursday_after_noon` / `test_refresh_not_due_before` /
  `test_missed_thursdays_collapse_to_one` — mirror `test_schedule.py` style
  (tz fixture) but through `is_refresh_due` with the refresh watermark.
- `test_refresh_runs_export_then_import_and_mails(tmp_path)` — fake export /
  importer / sender; order asserted; watermark advanced; notice subject/body
  mentions weeks + counts.
- `test_refresh_export_failure_mails_once_and_stays_owed(tmp_path)` — export
  raises: watermark not advanced, one failure mail; a second call for the
  same owed point sends nothing; a later successful call sends the normal
  notice and advances the watermark.
- `test_refresh_notice_lists_ties_and_unresolved(tmp_path)`.

`tests/test_runtime.py`:

- `test_run_wires_feedback_refresh_on_a_due_tick` — likely via monkeypatching
  `refresh.run_feedback_refresh`; skip if `run()`-level testing proves too
  heavy, and cover in the M6 integration test instead.

### Risks / notes

- `run_feedback_refresh` needs the SMTP sender; `run` already builds one.
- Export's `oldest` fallback when the CSV is empty: reuse
  `iso_to_ts(since_to_iso("180d", now=now))` — same as the CLI default.

---

## M6 — daily poll gate + send-retry semantics (spec §6)

**Commit:** `feat(schedule): daily poll gate; failed sends retry without re-polling`

### Files / functions

- `src/paper_watch/store.py`: `LAST_POLLED_KEY = "last_polled_at"` +
  `get_last_polled_at` / `set_last_polled_at` accessors, with a comment
  explaining the gate.
- `src/paper_watch/schedule.py`:

  ```python
  def is_poll_due(
      now: datetime,
      last_polled_at: str | None,
      *,
      delivery_due: bool,
      days: set[int],
      at: time,
  ) -> bool:
  ```

  True when: no poll on record; or last poll ≥ 24h old; or `delivery_due` and
  the last poll predates `last_delivery_at_or_before(now, ...)` (the owed
  point) — the "delivery-due tick polls first" exception. Module docstring
  updated to describe the second question a tick now asks.
- `src/paper_watch/runtime.py`:
  - `run_pipeline(...)` gains `do_ingest: bool = True`. When False: skip
    `ingest`/`resolve_paper_metadata`/`recover_titles` (there are no
    `new_ids`), still run `enrich_unenriched` (cheap, and a previously-polled
    backlog may exist), then proceed to selection/delivery as today.
  - `run(...)`: compute `deliver` as today, then
    `do_ingest = dry_run or since is not None or is_poll_due(now,
    store.get_last_polled_at(), delivery_due=deliver and not force_send, ...)`
    (an explicit `--since` is a manual ask to poll; `force_send` is a manual
    send, not a scheduled owed point, so it does not force a poll — flag if
    disagreeing). After a pipeline where ingest ran and not `dry_run`:
    `store.set_last_polled_at(now_iso)`.
  - **`effective_since` fix (see conflicts):** the gap-widening watermark must
    track *polling*, not ticks. Change `run` to pass
    `store.get_last_polled_at() or store.get_last_run_at()` into the widening
    logic (migration fallback for the first gated run), or change
    `effective_since` to take the watermark value as a parameter. Prefer the
    parameter: `effective_since(store, since, lookback, now)` becomes
    `effective_since(last_polled: str | None, since, lookback, now)` — update
    its four existing tests mechanically.
  - `RunResult` gains `polled: bool = False`; CLI echoes "poll skipped
    (last < 24h ago)" on gated ticks.

### Tests first

`tests/test_schedule.py`:

- `test_poll_due_when_never_polled`
- `test_poll_skipped_within_24h_off_schedule`
- `test_poll_due_after_24h`
- `test_delivery_due_tick_forces_poll_when_owed_point_uncovered` — last poll
  23h ago but *before* the owed noon point → due.
- `test_delivery_due_tick_does_not_repoll_when_point_covered` — poll at 12:05
  covered the noon point; 16:00 retry tick (send failed) → not due.
- `test_persistent_failure_past_24h_repolls`.

`tests/test_runtime.py`:

- `test_gated_tick_skips_ingest_but_still_delivers(tmp_path)` —
  `run_pipeline(do_ingest=False, deliver=True)` with a fetch-counting source:
  fetch not called, digest built from the DB and sent.
- `test_failed_send_retry_reuses_db_content(tmp_path)` — extend
  `test_a_failed_send_leaves_the_delivery_owed`: second pipeline call with
  `do_ingest=False` sends the same chosen ids.
- `test_effective_since_widens_from_last_poll_not_last_tick` — reworked
  `effective_since` tests.

Integration (spec's last bullet), in `tests/test_runtime.py` (or a new
`tests/test_ticks.py` if it grows): `test_fake_week_of_ticks_end_to_end
(tmp_path)` — drive a sequence of `now` values through a thin harness calling
the same pieces `run()` composes (poll gate → pipeline → refresh) with fake
sources/sender/export: asserts polls happen ~daily, Tue/Fri digests send,
Thursday refresh runs once, a read winner is excluded from the next digest.
This test is written in M6 (it needs the gate) but exercises M3–M5.

### Risks / notes

- Keep `run()`'s existing behavior when the gate says poll: identical to
  today. The gate must never skip enrichment of an existing backlog.
- Page-watch diffs and Slack history are lossy over long gaps — the old
  comment in `run_pipeline` justified polling every tick with exactly that.
  Daily polling is the spec's explicit call; the lookback (7d) and
  `effective_since` widening still cover Slack/RSS. Page-watch link-set diffs
  only miss a post that appears *and* scrolls off the index within a day —
  acceptable, but noted.

---

## Spec-vs-code conflicts and ambiguities (flagged, not silently resolved)

1. **v=1 arm is not literally "unchanged".** Today the lone-vote value is
   `min(linear_target, lone_formula)` — the linear arm can drag it *below*
   the lone formula for tiny polls. With the linear arm deleted, v=1 becomes
   the lone formula alone. Spec's stated values (−0.5 at a=3, −1.0 by a=7)
   match the lone formula, so this reading is taken — but exact v=1 outputs
   can differ from today's for some attendances.
2. **Existing sketch tests encode the deleted model.**
   `test_votes_to_target_matches_brisk_week_sketch` /
   `..._slow_week_sketch` were written against Jacob's original sketch;
   M1 deletes them. That is removing user-vetted expected values, per spec.
3. **Idempotence vs. corrections.** "(entry_id, week) present ⇒ skipped
   entirely" means a hand-corrected vote count in the CSV will never
   re-import without deleting the feedback row first. Also, a manual
   *candidates*-CSV import for a week blocks the later votes import for the
   same (entry, week). Accepted per spec; worth remembering operationally.
4. **Ties still nudge weights.** The spec removes pick + ledger + adds
   reporting for ties, but says nothing about suppressing the tied options'
   vote signal. Plan keeps the weight nudges (votes are real signal). If the
   intent was to withhold *all* effect until the human call, M3's tie test
   changes.
5. **`feedback.picked` vs. ledger for ties:** the tied options' feedback rows
   are recorded with `picked=0`. If the human later "makes the call", there
   is no importer path to set `picked`/ledger for that week — manual SQL or a
   future command. Spec is silent; flagged.
6. **`last_run_at` gap-widening breaks under the poll gate** (spec silent). A
   gated tick advances `last_run_at` without fetching, so
   `effective_since`'s widening would trust coverage that never happened.
   M6 rebases widening on `last_polled_at` (falling back to `last_run_at`
   for migration). `last_run_at` keeps advancing every real tick for its
   other consumers (none currently — it's only read by `effective_since` —
   so alternatively it could move only on polls; plan keeps it advancing and
   switches the reader, the smaller change).
7. **Refresh notice email on refresh success but SMTP failure:** spec ties
   "at most one failure notice" to export/import failures; it doesn't say
   whether a refresh whose *notice* fails to send should count as failed.
   Plan: the refresh succeeded — advance the watermark, log the mail error.
   The alternative (stay owed to retry the mail) risks re-running a
   successful import's side effects — safe now that import is idempotent, but
   noisier.
8. **`--force-send` and the poll-first exception:** spec's exception is keyed
   to "the owed delivery point"; a `--force-send` has no owed point. Plan
   treats force-send as not forcing a poll (the gate may still allow one).
9. **Exclusion horizon clock:** "read in the last 26 weeks" is measured from
   the poll's `message_ts` (when it was read), not `recorded_at` (when
   imported) — otherwise the initial 6-month backfill would stamp everything
   "read today". Spec doesn't say; this reading is the only sensible one but
   is an interpretation.
10. **Spec §2 "never touch existing rows" vs. CSV append:** appending can
    only guarantee this if the existing file ends with a newline; the append
    path must check and prepend `\n` if the last byte isn't one (hand edits
    in some editors strip it). Small implementation detail; covered by the
    append tests using a file without a trailing newline in at least one case.

## Ordering constraints

- M1, M2 are independent of everything (either could go first; spec order kept).
- M3 must precede M4 (table + ledger rows) and M5 (import result fields).
- M4 wires `select_digest`/`run_pipeline` only; the live `run()` wiring of
  `exclude_read_weeks` lands with the config block in M5.
- M5 must precede nothing but benefits M6's integration test; M6's
  integration test is the end-to-end check for M3–M6 together.
- Rollout steps (spec end) happen after M6, outside these commits, on the
  live DB/CSV — not part of this plan's file changes. Note the untracked
  root `groundtruth.csv` / `config.weekly.yaml` must not be touched by tests
  (all tests use tmp_path).
