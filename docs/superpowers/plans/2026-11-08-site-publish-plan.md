# Implementation plan: public site for digest history and ratings

**Spec:** `docs/superpowers/specs/2026-11-08-site-publish-design.md`
**Date:** 2026-11-08
**Branch / worktree:** `feat/site-publish` at `.worktrees/feat-site-publish`

Eight tasks. Tasks 1 to 7 are for implementation agents and each ends in one commit. Task 8 is run by the primary session, because it creates a GitHub repo, a deploy key, and a DNS record. TDD throughout: in every task the listed tests are written first and seen failing, then the implementation is written. While iterating, run the touched test files (`uv run pytest tests/test_site.py -q` and so on). Before each commit run the full suite (`uv run pytest -q`; 654 tests pass on the branch base). Commit subjects are given per task; each commit message ends with the attribution line the implementing session is told to use.

The spec's "Not published" list and "Out of scope" section are binding. Nothing here changes scoring, selection, the send path's watermarks, or how ratings are imported. Where a Decision below differs from the spec, the Decision wins.

---

## Decisions

These were settled on 2026-11-08, first from the plan's open questions and then from two plan reviews. Each item states the decision and the tasks it affects. Numbers 5 and 10 are retired and kept only so that earlier references stay unambiguous.

1. **No `generated_at`; use `latest_digest_at`.** A wall-clock `generated_at` would change on every publish, so the tree would never be clean. `papers.json` has no `generated_at`. It has `latest_digest_at`, the newest `shown.digest_at`, or JSON `null` when nothing has been shown. The footer of each HTML page says `Latest digest: <YYYY-MM-DD>`, or shows no such line when the store is empty. Change detection is `git status --porcelain` plus the ahead-of-remote check in Decision 3. The commit message keeps the wall-clock timestamp, as the spec says. (Tasks 3 and 4.)

2. **Dry-run never writes to `repo_path`.** It renders into a temporary directory, compares each managed file with the copy in `repo_path`, and reports a per-file list of the files that would be added or changed. (Task 4.)

3. **A clean tree still pushes when there is something to push.** When `git status --porcelain` is empty, publish pushes if the local branch is ahead of `<remote>/<branch>` or that remote-tracking ref does not exist yet. It reports "site unchanged" only when there is nothing to push either. (Task 4.)

4. **Publish writes the site repo's `README.md`.** It comes from a module string in `site.py`, like `style.css`. The managed set is the six files `index.html`, `all.html`, `papers.json`, `style.css`, `CNAME`, `README.md`. (Tasks 3 and 4.)

5. *Retired.* The earlier decision to show site failures in the digest's warning block is replaced by Decision 13.

6. **Rating markers.** Show `group rating N/5` when there is a rating (Decision 21), add `read` when the paper was read, and show `picked` only when the paper is picked, not read, and unrated. A paper can show `group rating 4/5` and `read` together. (Task 3.)

7. **"Times shown" counts every appearance.** The table column is "times shown" and includes the first digest the paper appeared in. (Task 2.)

8. **The latest non-NULL rating wins.** `group_rating` is the rating from the latest week whose `group_rating` is not NULL. Ratings imported from Slack polls are derived from vote share (`feedback.import_votes` maps it onto 1 to 5), so the rating on the site is that derived number. (Task 2.)

9. **Site name and description are placeholders.** Title "paper-watch", description "Papers suggested by paper-watch, a scan of new AI safety research, with the reading group's ratings where they exist." This is a placeholder for the user to confirm, kept in the constants `SITE_TITLE` and `SITE_DESCRIPTION`. (Task 3.)

10. *Retired.* Two publishes on one tick no longer happen; see Decision 14.

11. **The project README is Claude-maintained.** Task 7 edits `README.md` and commits the changes directly. (Task 7.)

12. **The domain is a config key.** `SiteConfig` gains `domain: str = "papers.cupchinski.com"`. The `CNAME` file and any absolute links in the rendered pages use it. `site.py` has no hardcoded domain. (Tasks 1, 3, and 4.)

13. **Publish failures never reach the digest.** The digest goes to the reading group, and commit 053c483 moved refresh notices away from those recipients for the same reason. On a failed publish the tick still calls `store.record_source_failure("site", label="site publish", ...)`, so the health row exists for diagnosis, and success still calls `record_source_ok`. `source_warnings` excludes the `site` key entirely, so it never appears in the digest's warning block, whatever its streak. The operator is told through the same route `run_feedback_refresh` uses for failure notices: `refresh._default_alert_send`, which fans out through `alerts.send_alert` to the alerts log, desktop, operator email (`smtp.from_addr`), and the operator's Slack target. The notice body contains only the first line of the error and never contains the deploy key path; the same sanitized text goes into the health row's `last_error`. Every failed publish sends one notice. There is no once-per-point suppression, because publishes happen only a few times a week. The digest template is not changed. (Task 6.)

14. **One publish per tick.** There is a single call site, in `runtime.run()`, after the send (inside `run_pipeline`) and the feedback refresh have both had their chance. It runs when the tick is not a dry run, `config.site` is set, and either `result.sent` is true or the feedback refresh was performed and returned `ok=True`. `run_pipeline` does not change. This removes the problem the review found where two publishes in one tick could interfere with each other. (Task 6.)

15. **Git subprocess hardening.** Every git call passes `timeout=GIT_TIMEOUT_SECONDS` (a module constant, 120). A `subprocess.TimeoutExpired` is turned into `SitePublishError` and handled like any other failure. `GIT_SSH_COMMAND` is always set for git calls made by publish: it is `ssh -o BatchMode=yes` when `SITE_DEPLOY_KEY` is unset or empty, and `ssh -o BatchMode=yes -i <quoted key path> -o IdentitiesOnly=yes` when it is set. Publish also sets `GIT_TERMINAL_PROMPT=0` so an HTTPS remote cannot prompt for credentials either. An unset key can therefore never block on a keyring or passphrase prompt; it fails. This replaces the spec's "if `SITE_DEPLOY_KEY` is unset, publish uses the ambient git configuration": the ambient SSH agent and default keys still work, but only without prompting. (Task 4.)

16. **Deploy key location.** Task 8 generates the key at `~/.ssh/paper-watch-site_deploy_key`, outside any repo, so no `.gitignore` entry is needed. `.env` is appended to with a small Python snippet that adds a newline first if the file does not end with one. (Tasks 7 and 8.)

17. **The `site:` block is committed to the tracked `config.yaml`.** `config.yaml` is tracked and earlier config additions (`feedback_refresh`, `alerts`) were committed to it, so Task 7 adds the `site:` block to `config.yaml` on the branch with `repo_path: ../paper-watch-site`, `remote: origin`, `branch: main`, and commits it together with the example files. The primary session verified this on 2026-11-08; an earlier reviewer claim that the file is gitignored was wrong. (Tasks 7 and 8.)

18. **Only http and https URLs are published.** When a record is built, its `url` is kept only if `urllib.parse.urlsplit` gives a scheme of `http` or `https` (case-insensitive) and a non-empty host, and the string has no whitespace or control characters. Anything else gives `url = None`: `javascript:`, `data:`, `slack://` (which also covers the mention fallback when a mention URL is a Slack permalink in app form), protocol-relative `//host/path`, `http://` with no host, and malformed strings. The renderers show the title without a link when `url` is None. The check applies to the first displayed link only; publish does not fall back to a later link. (Tasks 2 and 3.)

19. **Commit identity comes from config.** `SiteConfig` gains `committer_name: str = "paper-watch"` and `committer_email: str | None = None`. Publish runs `git -c user.name=<name> -c user.email=<email> commit ...`. When `committer_email` is None, publish reads `git config user.email` in the site checkout at publish time; if that is empty or unset, publish fails with a clear `SitePublishError` before writing any file. Tests set both fields. (Tasks 1 and 4.)

20. **Test placement.** Every test that drives `runtime.run()` lives in `tests/test_runtime.py`. After Decision 13 there is no `tests/test_digest.py` change at all. (Task 6.)

21. **Labels on derived content.** The tldr's `<details>` element has the summary text "AI summary". The rating marker reads `group rating N/5`. The index footer has one sentence saying ratings come from the reading group's weekly poll. These are constants next to `SITE_TITLE`: `TLDR_LABEL = "AI summary"`, `RATING_FORMAT = "group rating {n}/5"`, and `RATINGS_NOTE = "Ratings come from the reading group's weekly poll."`, so the user can change the wording in one place. (Task 3.)

---

## Dependencies and parallelism

- Task 1 (config) and Task 2 (record query) are independent and can run in parallel.
- Task 3 (renderers) needs Task 2. Task 4 (publish) needs Tasks 1 and 3. Tasks 2, 3, and 4 all edit `src/paper_watch/site.py` and `tests/test_site.py`, so they run in sequence.
- Task 5 (CLI command) and Task 6 (tick wiring) both need Task 4 and are independent of each other. They touch different files (`cli.py` and `test_cli.py` versus `runtime.py` and `test_runtime.py`) and can run in parallel.
- Task 7 (docs and example config) needs only Task 1's key names and can run in parallel with anything after Task 1. It edits `cli.py` only inside the `EXAMPLE_CONFIG` string, so if it runs alongside Task 5 the two changes merge without overlap.
- Task 8 (out-of-repo setup) runs last, in the primary session, after Task 5 is on the branch.

Suggested order: {1, 2} in parallel, then 3, then 4, then {5, 6, 7} in parallel, then 8.

---

## Conventions the tasks follow

- `site.py` imports `_display_links` and `_pub_display` from `paper_watch.runtime`. `runtime.py` must import `paper_watch.site` lazily inside `run()` (the same way it imports `refresh`) to avoid an import cycle.
- Rendering uses `jinja2.Environment(autoescape=True)` and module-level template strings, as `digest.py` does. If a template grows past about 80 lines the implementer may move it to `src/paper_watch/templates/` instead; the spec allows either.
- A relative `repo_path` resolves against the working directory, like `db_path` and `groundtruth_path`. The systemd unit's `WorkingDirectory` is `~/Code/paper-watch`, so `../paper-watch-site` means `~/Code/paper-watch-site`.
- Git is driven with `subprocess.run(["git", "-C", str(repo), ...], capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS, env=...)`. No `shell=True`. A non-zero exit raises `SitePublishError("git <subcommand> failed (exit <code>): <first stderr line>")`; the full stderr is kept on the exception's `stderr` attribute for logging, not in its message.
- Tests that run real git set `GIT_CONFIG_GLOBAL=/dev/null` and `GIT_CONFIG_NOSYSTEM=1` with `monkeypatch.setenv`, so the developer's git config (signing, hooks, default branch, user identity) cannot affect them. They do not set `GIT_AUTHOR_*` or `GIT_COMMITTER_*`, because those would override the `-c user.name/user.email` flags that Decision 19 tests. Each test passes `committer_name` and `committer_email` in its `SiteConfig`, except the test for the missing-email error. Remotes are local bare repos under `tmp_path`. No network.

---

## Task 1: `SiteConfig` block

**Files:** `src/paper_watch/config.py`, `tests/test_config.py`.

**Tests first** (in `tests/test_config.py`, following `test_feedback_refresh_block_parses`):

- `test_site_absent_defaults_to_none`: an empty config loads with `cfg.site is None`.
- `test_site_block_parses`: a block with `repo_path: ../paper-watch-site`, `remote: upstream`, `branch: pages`, `domain: example.org`, `committer_name: bot`, `committer_email: bot@example.org` loads with those values.
- `test_site_defaults`: a block with only `repo_path` gets `remote == "origin"`, `branch == "main"`, `domain == "papers.cupchinski.com"`, `committer_name == "paper-watch"`, and `committer_email is None`.
- `test_site_requires_repo_path`: `site: {}` raises `pydantic.ValidationError` on load.

**Implementation:**

- Add `class SiteConfig(BaseModel)` after `FeedbackRefreshConfig`, with `repo_path: str`, `remote: str = "origin"`, `branch: str = "main"`, `domain: str = "papers.cupchinski.com"` (written to the site's `CNAME` file), `committer_name: str = "paper-watch"`, and `committer_email: str | None = None` (None means the site checkout's `git config user.email`, read at publish time). Add a short docstring saying the block points at a local checkout of the site repo, which publish renders into, commits, and pushes.
- Add `site: SiteConfig | None = None` to `Config` next to `feedback_refresh`, with a comment that an absent block means the public site is not published.

**Commit:** `feat(config): optional site block for the public digest site`

---

## Task 2: record query

**Files:** `src/paper_watch/store.py`, new `src/paper_watch/site.py`, new `tests/test_site.py`, `tests/test_store.py`.

**Fixture** (in `tests/test_site.py`): `site_store(tmp_path)` returns an open `Store` holding:

- Entry A: an arXiv paper (`arxiv_id="2410.00001"`, links `{"abstract": "https://arxiv.org/abs/2410.00001", "pdf": "https://arxiv.org/pdf/2410.00001"}`, `published_at="2024-10-01T00:00:00Z"`, an abstract), enriched with tags, a tldr, and a `why` via `store.set_enrichment`, so the test can assert that abstract and why are absent from the output. Shown in digest D1 (`2026-10-02T12:00:00Z`) at rank 1 and in digest D2 (`2026-10-09T12:00:00Z`) at rank 2 with `resurfaced=True`. Feedback rows: week `2026-W40` rating 2, week `2026-W41` rating 4, week `2026-W42` rating NULL.
- Entry B: a LessWrong post whose canonical `abstract` link is `https://www.lesswrong.com/posts/abc123/slug`, with an `https://www.alignmentforum.org/posts/abc123/slug` URL added through `store.add_entry_url`. Shown in D1 at rank 2. One feedback row with `picked=True` and no rating.
- Entry C: no links, no `published_at`, one mention with an `https://example.slack.com/archives/C1/p123` `source_item_url` and a `published_at`. Shown in D2 at rank 1. One `readings` row with `entry_id` set to C.
- Entry D: never shown, so it must not appear anywhere.

Use `store.insert_entry`, `store.add_mention`, `store.add_entry_url`, `store.set_enrichment`, `store.record_shown`, `store.record_feedback`, and `store.record_reading`, not raw SQL.

A second helper, `_store_with_link(tmp_path, url)`, builds a store with one shown entry whose only link is `{"abstract": url}`, for the URL-safety tests.

**Tests first:**

In `tests/test_store.py`:

- `test_shown_history_joins_entries_and_aggregates`: `store.shown_history()` returns one row per `shown` row, ordered by `digest_at` descending then `rank` ascending with NULL ranks last, and each row carries `latest_rating` (from the latest week with a non-NULL rating), `picked` (0 or 1), and `read` (0 or 1).

In `tests/test_site.py`:

- `test_records_one_per_shown_row`: `load_records(store)` returns four records (A twice, B, C), and D is absent.
- `test_record_field_mapping`: A's record has the title, the authors list, the arXiv id, the tags list, the tldr, `published == "2024-10"`, `is_estimate is False`, and `url == "https://arxiv.org/abs/2410.00001"`.
- `test_record_url_prefers_alignmentforum`: B's `url` is the `alignmentforum.org` spelling.
- `test_record_url_falls_back_to_mention`: C's `url` is its mention's `source_item_url`, C's `is_estimate is True`, and its `published` comes from the mention date.
- `test_latest_week_rating_wins`: A's `group_rating == 4` on both of its records (Decision 8).
- `test_picked_and_read_flags`: B has `picked=True`, `read=False`, `group_rating=None`; C has `read=True`.
- `test_resurfaced_flag_per_digest`: A's D2 record has `resurfaced=True` and its D1 record has `resurfaced=False`.
- `test_group_by_digest_newest_first_rank_order`: `group_by_digest(records)` returns D2 with `[C, A]`, then D1 with `[A, B]`.
- `test_table_rows_once_per_paper_under_first_digest`: `table_rows(records)` returns three rows. A's row has `first_digest_at == D1`, `rank == 1`, and `times_shown == 2` (Decision 7). Rows are ordered by first digest, newest first, then by rank.
- `test_record_json_excludes_private_fields`: `record.to_json()` has exactly the keys `digest_at, rank, resurfaced, title, authors, url, arxiv_id, published, is_estimate, tags, tldr, group_rating, picked, read`, and none of `score, why, abstract, explanation, sources, entry_id`.
- `test_public_url_accepts_http_and_https`: parametrized over `https://arxiv.org/abs/1`, `http://example.org/x`, and `HTTPS://Example.org/x`; `public_url(u) == u`.
- `test_public_url_rejects_unsafe_and_malformed`: parametrized over `javascript:alert(1)`, ` javascript:alert(1)` (leading space), `JaVaScRiPt:alert(1)`, `data:text/html,<b>x</b>`, `slack://channel?team=T1&id=C1`, `//evil.example/x`, `http://`, `https:///path`, `not a url`, `https://exa mple.org/` (embedded space), `https://example.org/\n` (control character), and the empty string; `public_url(u) is None` (Decision 18).
- `test_record_with_unsafe_link_has_no_url`: for each rejected value above that `insert_entry` accepts as a link, `_store_with_link` gives a record with `url is None`.
- `test_slack_app_mention_fallback_has_no_url`: an entry with no links whose only mention has `source_item_url="slack://channel?team=T1&id=C1"` gives `url is None`.

**Implementation:**

- `Store.shown_history(self) -> list[sqlite3.Row]`, in the "digest history" section beside `entries_shown_since`: one query joining `shown` to `entries`, with correlated subqueries for `latest_rating` (`SELECT group_rating FROM feedback WHERE entry_id = e.id AND group_rating IS NOT NULL ORDER BY week DESC LIMIT 1`), `picked` (`EXISTS` over `feedback` with `picked = 1`), and `read` (`EXISTS` over `readings` with `entry_id = e.id`). Order by `shown.digest_at DESC, shown.rank IS NULL, shown.rank, shown.id`. Week labels are ISO-week strings such as `2026-W41`, so `ORDER BY week` is chronological. The query must select `entries.id AS id`, `published_at`, `first_seen_at`, and `links_json` under those names, because `_pub_display` and `_display_links` read them from the row.
- In `site.py`:
  - A module docstring stating what the site is and that `score`, `why`, `abstract`, source labels, and mentions are never published, and that only http and https links are published.
  - `public_url(url: str | None) -> str | None` per Decision 18.
  - `@dataclass(frozen=True) class SiteRecord` with `entry_id` (internal, never serialized) and the fields listed in the JSON test, plus `to_json(self) -> dict`.
  - `load_records(store) -> list[SiteRecord]`: for each `shown_history()` row, `url` is `public_url` of the first value of `_display_links(store, entry_id, json.loads(row["links_json"]))`, or None when that dict is empty, and `published, is_estimate` come from `_pub_display(store, row)`.
  - `group_by_digest(records) -> list[tuple[str, list[SiteRecord]]]`, preserving the query order.
  - `@dataclass(frozen=True) class TableRow` with `record: SiteRecord` (the first appearance), `first_digest_at: str`, and `times_shown: int`, and `table_rows(records) -> list[TableRow]`.

**Commit:** `feat(site): load digest history records for the public site`

---

## Task 3: renderers, stylesheet, CNAME, site README

**Files:** `src/paper_watch/site.py`, `tests/test_site.py`. Depends on Task 2. Decisions 1, 4, 6, 9, 12, 18, and 21 apply. `build_site` takes the domain as a plain string argument, so this task does not need Task 1's `SiteConfig`.

**Tests first** (in `tests/test_site.py`, reusing `site_store`):

- `test_index_renders_every_title`: `render_index(...)` contains A's, B's, and C's titles and not D's.
- `test_index_orders_digests_newest_first_and_entries_by_rank`: in the HTML, the D2 heading precedes the D1 heading; within D2, C precedes A; within D1, A precedes B.
- `test_index_marks_estimates_and_ratings`: C's published date renders with a leading `~`; A shows `group rating 4/5`; B shows `picked`; C shows `read`; A's D2 entry is marked resurfaced.
- `test_index_tldr_under_ai_summary_label`: A's tldr appears inside a `<details>` element whose `<summary>` text is `TLDR_LABEL` ("AI summary").
- `test_index_footer_links_and_ratings_note`: the footer contains `href="all.html"`, `href="papers.json"`, `RATINGS_NOTE`, and `Latest digest: 2026-10-09`.
- `test_all_renders_one_row_per_paper`: `render_all(...)` contains each title exactly once, a times-shown cell of `2` for A, and `Latest digest: 2026-10-09` in the footer.
- `test_all_has_filter_box_and_no_network`: the page contains an `<input` filter box and an inline `<script>`, has no `src=` on any script tag, and contains neither `fetch(` nor `XMLHttpRequest`.
- `test_json_shape`: `json.loads(render_json(...))` has exactly the top-level keys `latest_digest_at` and `digests`, `latest_digest_at == "2026-10-09T12:00:00Z"`, the digests are newest first, and each entry dict has exactly the published keys. There is no `generated_at` key.
- `test_titles_are_escaped`: with an entry titled `Attacks on <script>alert(1)</script> models` shown once, `render_index` and `render_all` contain `&lt;script&gt;` and not the raw tag, and `render_json` round-trips the raw title through `json.loads`.
- `test_unsafe_url_renders_title_unlinked`: parametrized over the rejected values from Task 2's `test_public_url_rejects_unsafe_and_malformed` that `insert_entry` accepts. For each, `render_index` and `render_all` contain the title, contain no `href` with the rejected value (checked on the escaped and unescaped forms), and render the title without an `<a>` wrapper; `render_json` gives `"url": null` for that entry.
- `test_build_site_is_deterministic`: two calls to `build_site(store, domain="papers.cupchinski.com")` return identical file contents.
- `test_build_site_returns_managed_files`: `build_site(store, domain="papers.cupchinski.com")` returns a dict whose keys are exactly `MANAGED_FILES` (`index.html`, `all.html`, `papers.json`, `style.css`, `CNAME`, `README.md`), with `CNAME` equal to `"papers.cupchinski.com\n"`.
- `test_cname_uses_configured_domain`: `build_site(store, domain="example.org")["CNAME"] == "example.org\n"`, and `papers.cupchinski.com` appears nowhere in the output.
- `test_index_shows_placeholder_title_and_description`: the index contains `SITE_TITLE` and `SITE_DESCRIPTION`.
- `test_empty_store_renders`: an empty store renders all three pages without error; `papers.json` is `{"latest_digest_at": null, "digests": []}`; neither HTML page contains `Latest digest`.

**Implementation** (all in `site.py`):

- Constants: `SITE_TITLE` and `SITE_DESCRIPTION` (Decision 9), `TLDR_LABEL`, `RATING_FORMAT`, and `RATINGS_NOTE` (Decision 21), `MANAGED_FILES` (a tuple), `STYLE_CSS` (a hand-written stylesheet: a readable single column around 760px wide, chips styled like the email's tag chips, a table style for `all.html`, and a `prefers-color-scheme: dark` block), and `SITE_README` (two or three sentences saying the content is generated by paper-watch and that hand edits are overwritten).
- `rating_markers(record) -> list[str]` per Decision 6, using `RATING_FORMAT`.
- `latest_digest_at(records) -> str | None`: the first record's `digest_at` (records are newest first), or None. No new store method.
- `_INDEX_TEMPLATE` and `_ALL_TEMPLATE` module strings. Both link `style.css`. Digest dates show as `digest_at[:10]`. A title is wrapped in `<a href>` only when `record.url` is not None. The footer shows `Latest digest: {{ latest[:10] }}` only when `latest` is set. In `all.html`, each `<tr>` carries its tags and lowercase title-plus-authors text in `data-` attributes, and an inline script of a few dozen lines filters rows by substring from the text box and by a clicked tag chip (clicking the active chip again clears the tag filter). With JavaScript off, the full table renders.
- `render_index(groups, *, latest_digest_at) -> str`, `render_all(rows, *, latest_digest_at) -> str`, and `render_json(groups, *, latest_digest_at) -> str` (`json.dumps(..., ensure_ascii=False, indent=1)` plus a trailing newline).
- `build_site(store, *, domain: str) -> dict[str, str]`: loads records once and returns every managed file's content. `CNAME` is `domain` plus a newline, and any absolute URL in the pages is built from `domain`.

**Commit:** `feat(site): render index, table, and JSON pages for the public site`

---

## Task 4: publish function

**Files:** `src/paper_watch/site.py`, `tests/test_site.py`. Depends on Tasks 1 and 3. Decisions 2, 3, 4, 12, 15, and 19 apply.

**Fixture:** `site_repo(tmp_path, monkeypatch)` applies the git environment isolation from "Conventions", creates a bare repo `origin.git` with `git init --bare`, clones it to `checkout/`, points the unborn branch at `main` with `git symbolic-ref HEAD refs/heads/main`, and returns `(SiteConfig(repo_path=str(checkout), committer_name="Test Bot", committer_email="bot@example.org"), origin_path)`.

**Tests first:**

- `test_first_publish_commits_and_pushes`: `publish(site_store, cfg, now=...)` returns `committed=True` and `pushed=True`. The bare repo's `main` has one commit whose message matches `site: 2 digests, 3 papers (2026-...Z)`, and its tree holds exactly `MANAGED_FILES`.
- `test_commit_uses_configured_identity`: the commit's author and committer are `Test Bot <bot@example.org>` (`git log -1 --format='%an <%ae>|%cn <%ce>'`).
- `test_committer_email_falls_back_to_repo_git_config`: with `committer_email=None` and `git config user.email repo@example.org` set in the checkout, the commit's author email is `repo@example.org`.
- `test_missing_committer_email_is_a_clear_error`: with `committer_email=None` and no `user.email` anywhere (the isolation makes global config empty), publish raises `SitePublishError` mentioning `committer_email` and `user.email`, and nothing is written into the checkout.
- `test_second_publish_unchanged_makes_no_commit`: a second call returns `changed=False`, `committed=False`, `pushed=False`, and the commit count does not change.
- `test_publish_after_new_digest_commits_again`: after `record_shown` adds a new digest, publish makes a second commit.
- `test_push_failure_raises_and_keeps_local_commit`: rename `origin.git` away, change the data, and publish raises `SitePublishError`, while the checkout's `HEAD` holds the new commit.
- `test_unpushed_commit_is_pushed_on_next_publish`: continuing from the failure above, rename `origin.git` back and publish with no data change. It returns `pushed=True`, and the bare repo now has the commit (Decision 3).
- `test_dry_run_leaves_repo_untouched`: with data that would change files, `publish(..., dry_run=True)` returns the list of files that would change, and afterwards `git status --porcelain` in the checkout is empty and the commit count has not changed (Decision 2).
- `test_publish_writes_configured_domain`: with `domain="example.org"`, the committed `CNAME` contains `example.org` (Decision 12).
- `test_precondition_missing_repo_path`: a `repo_path` that does not exist raises `SitePublishError` mentioning the path.
- `test_precondition_not_a_git_checkout`: a plain directory raises `SitePublishError` saying it is not a git checkout.
- `test_precondition_wrong_branch`: a checkout on branch `other` raises `SitePublishError` naming both branches.
- `test_precondition_foreign_dirty_file`: an untracked `notes.txt` in the checkout raises `SitePublishError` naming it, and nothing is written or committed.
- `test_dirty_managed_file_is_allowed`: a hand-modified `index.html` does not block publish and is overwritten.
- `test_git_env_without_key_is_batch_mode`: `_git_env({})["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"`, `GIT_TERMINAL_PROMPT == "0"`, and an empty `SITE_DEPLOY_KEY` gives the same result.
- `test_git_env_with_key_adds_identity`: `_git_env({"SITE_DEPLOY_KEY": "/k/my key"})["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes -i '/k/my key' -o IdentitiesOnly=yes"` (the path quoted with `shlex.quote`).
- `test_git_env_overrides_ambient_ssh_command`: an input `GIT_SSH_COMMAND="ssh -v"` is replaced, so `BatchMode=yes` is always present (Decision 15).
- `test_git_timeout_is_a_publish_error`: monkeypatch `subprocess.run` inside `site` to raise `subprocess.TimeoutExpired(cmd, 120)`; `_git(...)` raises `SitePublishError` whose message names the git subcommand and the timeout. A second assertion checks that `_git` passes `timeout=GIT_TIMEOUT_SECONDS` and `GIT_TIMEOUT_SECONDS == 120`.
- `test_git_error_message_is_first_stderr_line`: a failing git command whose stderr has several lines raises `SitePublishError` whose `str()` contains only the first stderr line, with the full stderr on `.stderr`.
- `test_missing_deploy_key_file_is_a_clear_error`: with `SITE_DEPLOY_KEY` pointing at a file that does not exist, publish raises `SitePublishError` naming the variable (not the path) before anything is written or committed.

**Implementation** (in `site.py`):

- `GIT_TIMEOUT_SECONDS = 120`.
- `class SitePublishError(RuntimeError)` with an optional `stderr: str` attribute.
- `@dataclass class PublishResult` with `changed: bool`, `committed: bool`, `pushed: bool`, `files: list[str]` (porcelain paths, or the would-change paths on a dry run), and `message: str | None` (the commit message).
- `_git(repo: Path, *args: str, env: Mapping[str, str]) -> str`: runs git with the timeout; converts `TimeoutExpired` and non-zero exits into `SitePublishError` per "Conventions".
- `_git_env(environ: Mapping[str, str]) -> dict[str, str]`: a copy of `environ` with `GIT_SSH_COMMAND` and `GIT_TERMINAL_PROMPT=0` set per Decision 15. Used for every git call publish makes.
- `_committer_email(repo, site, env) -> str`: `site.committer_email`, else the output of `git config user.email` in `repo` (a non-zero exit from `git config` means unset), else `SitePublishError`.
- `_check_preconditions(repo: Path, branch: str, env) -> None`: the path exists; `git rev-parse --show-toplevel` succeeds and equals the resolved `repo`; `git symbolic-ref --short HEAD` equals `branch` (this works on the unborn branch of a freshly cloned empty repo, where `rev-parse --abbrev-ref HEAD` fails); every path in `git status --porcelain` is in `MANAGED_FILES`.
- `_is_ahead(repo, remote, branch, env) -> bool`: true when `HEAD` has commits and either `refs/remotes/<remote>/<branch>` does not exist or `git rev-list --count <remote>/<branch>..HEAD` is non-zero. It reads only local refs and never fetches.
- `publish(store, site: SiteConfig, *, now: datetime, dry_run: bool = False, environ: Mapping[str, str] | None = None) -> PublishResult`:
  1. `env = _git_env(environ if environ is not None else os.environ)`. Resolve `repo = Path(site.repo_path)` and run `_check_preconditions`. If `SITE_DEPLOY_KEY` is set and the file is missing, raise here. Resolve the committer email here, so a missing one fails before any write.
  2. `files = build_site(store, domain=site.domain)`.
  3. On a dry run, write `files` into a `tempfile.TemporaryDirectory`, compare each with `repo / name` (a missing file or different bytes counts as a change), and return `PublishResult(changed=bool(diff), committed=False, pushed=False, files=diff, message=None)`.
  4. Otherwise write each file into `repo` and read `git status --porcelain`.
  5. If it is empty: when `_is_ahead`, push and return `pushed=True`; otherwise log `site unchanged` at info level and return all-false.
  6. Otherwise run `git add -A`, then `git -c user.name=<committer_name> -c user.email=<email> commit -m "site: <N> digests, <M> papers (<now as ISO Z>)"` where N is the number of distinct `digest_at` values and M the number of distinct entries, then `git push <remote> <branch>`. A push failure raises `SitePublishError` after the commit has been made.

**Commit:** `feat(site): publish rendered site to its git repo`

---

## Task 5: `paper-watch publish` command

**Files:** `src/paper_watch/cli.py`, `tests/test_cli.py`. Depends on Task 4. Can run in parallel with Tasks 6 and 7.

**Tests first** (in `tests/test_cli.py`, using `CliRunner` and `monkeypatch.setattr(site_mod, "publish", fake)` so no git runs):

- `test_publish_requires_site_block`: a config without `site:` exits non-zero with a message saying the `site` block is missing.
- `test_publish_passes_dry_run_through`: `publish --config cfg --dry-run` calls the fake with `dry_run=True`, and the output lists the would-change files.
- `test_publish_reports_unchanged`: when the fake returns all-false, the output contains `site unchanged` and the exit code is 0.
- `test_publish_reports_commit_and_push`: when the fake returns `committed=True`, `pushed=True`, and a message, the output contains the commit message.
- `test_publish_error_is_click_error`: when the fake raises `SitePublishError("boom")`, the exit code is non-zero and the output contains `boom` without a traceback.

**Implementation:**

- A new `@cli.command("publish")` with `--config` (default `config.yaml`, as in every other command) and `--dry-run`. It calls `load_dotenv()` inside `try/except ImportError` as `run` and `alert` do, so `SITE_DEPLOY_KEY` is read from `.env`. It loads `Config`, raises `click.ClickException` when `cfg.site is None`, opens `Store(cfg.db_path)`, calls `site.publish(store, cfg.site, now=datetime.now(timezone.utc), dry_run=dry_run)` inside `try/finally: store.close()`, converts `SitePublishError` into `click.ClickException`, and echoes one summary line, plus the file list on a dry run. The manual command does not send operator notices; its errors go to the terminal.

**Commit:** `feat(cli): paper-watch publish command for the public site`

---

## Task 6: tick wiring and failure reporting

**Files:** `src/paper_watch/runtime.py`, `tests/test_runtime.py`. Depends on Task 4. Can run in parallel with Tasks 5 and 7. Decisions 13, 14, and 20 apply. `run_pipeline`, `digest.py`, and `tests/test_digest.py` do not change.

**Tests first** (all in `tests/test_runtime.py`, modelled on `test_run_wires_feedback_refresh_on_a_due_tick`: `monkeypatch.chdir(tmp_path)`, a config file under `tmp_path` with `alerts: {log_file: ..., desktop: false, email: false}`, `runtime.run_pipeline` faked to return a `RunResult`, `refresh.run_feedback_refresh` faked where needed, `site.publish` monkeypatched to record calls, and `refresh._default_alert_send` monkeypatched to record `(subject, body)`):

- `test_run_publishes_once_after_a_send`: config with `site:` and no `feedback_refresh:`; the fake pipeline returns `RunResult(sent=True)`. `site.publish` is called once with `config.site`, and `store.get_source_health("site")` has `consecutive_failures == 0` and `label == "site publish"`.
- `test_run_publishes_once_when_send_and_refresh_share_a_tick`: config with `site:` and `feedback_refresh:`; the pipeline returns `sent=True` and the refresh returns `RefreshResult(performed=True, ok=True)`. `site.publish` is called exactly once, after the refresh (the fake refresh appends to the same call log, and the order is checked).
- `test_run_publishes_after_a_successful_refresh_without_send`: `sent=False`, refresh `ok=True`; one publish call.
- `test_run_skips_publish_after_a_failed_refresh_without_send`: `sent=False`, refresh `ok=False`; no publish call.
- `test_run_skips_publish_when_nothing_happened`: `sent=False` and no refresh due (refresh watermark set to now); no publish call, and no `site` health row.
- `test_run_dry_run_never_publishes`: `runtime.run(cfg, dry_run=True)` with a fake pipeline that returns `sent=True` (to prove the dry-run guard is explicit); no publish call.
- `test_run_without_site_block_never_publishes`: config with `feedback_refresh:` and no `site:`, pipeline `sent=True`, refresh ok; no publish call.
- `test_publish_failure_records_health_and_alerts_operator`: `SITE_DEPLOY_KEY` set to `/home/x/.ssh/paper-watch-site_deploy_key` with `monkeypatch.setenv`; `site.publish` raises `SitePublishError("git push failed (exit 128): fatal: Could not read from remote repository.\nWarning: Identity file /home/x/.ssh/paper-watch-site_deploy_key not accessible")`. `runtime.run` returns normally. The `site` health row has `consecutive_failures == 1` and a `last_error` equal to the first line. The alert fake was called once with subject `site publish failed`; the body contains `Could not read from remote repository`, does not contain `Identity file`, and does not contain the key path or the string `paper-watch-site_deploy_key`.
- `test_publish_failure_redacts_key_path_in_first_line`: as above, but the key path appears in the first line of the error. The notice body and `last_error` contain `<deploy key>` in its place.
- `test_publish_failure_does_not_reach_digest_warnings`: after five recorded `site` failures (`store.record_source_failure("site", ...)`), `source_warnings(store, 1)` contains no warning whose label is `site publish`, while a `page:https://example.com/blog` source with one failure in the same store is still returned.
- `test_publish_exception_of_any_type_is_contained`: `site.publish` raising a plain `RuntimeError` (not `SitePublishError`) is handled the same way: `run` returns, one failure recorded, one notice.

**Implementation:**

- A module constant `SITE_SOURCE_KEY = "site"` and a module-level `_log = logging.getLogger(__name__)` (`runtime.py` has none today).
- `source_warnings`: skip rows whose `source == SITE_SOURCE_KEY`. Update the docstring to say the site publish is excluded because the digest goes to the reading group, and that its failures go to the operator instead (Decision 13).
- `_site_failure_summary(exc: BaseException, environ: Mapping[str, str]) -> str`: the first line of `str(exc)` (or the exception class name if that is empty), with the value of `SITE_DEPLOY_KEY`, when set and non-empty, replaced by `<deploy key>`.
- `_publish_site(store, config, *, now: datetime, environ: Mapping[str, str] | None = None) -> bool`: imports `paper_watch.site` and `paper_watch.refresh` locally, calls `site.publish(store, config.site, now=now)`. On success, `store.record_source_ok(SITE_SOURCE_KEY, label="site publish", at=now_iso)` and return True. On any `Exception`: build the summary, `_log.warning("site publish failed: %s", summary)`, `store.record_source_failure(SITE_SOURCE_KEY, label="site publish", error=summary, at=now_iso)`, then call `refresh._default_alert_send(config.alerts, "site publish failed", body, config=config, now=now)` through the module attribute (so tests can patch it) with a plain-text body of two lines: the summary, and a line saying the digest was sent normally and the site will be retried on the next send or refresh. Wrap the alert call in its own `try/except Exception` that only logs, so a broken alert channel cannot fail the tick. Return False.
- In `run()`: keep the refresh's return value (`fr_result = None` before the block; `fr_result = refresh.run_feedback_refresh(store, config, now=now)` inside it; `result.refreshed = True` stays as it is). After the refresh block and before `alert_if_overdue`, add: `if config.site is not None and not dry_run and (result.sent or (fr_result is not None and fr_result.performed and fr_result.ok)): _publish_site(store, config, now=now)`. This is the only call site.

**Commit:** `feat(runtime): publish the site once per tick and alert the operator on failure`

---

## Task 7: docs and example config

**Files:** `.env.example`, `config.example.yaml`, `config.yaml`, `src/paper_watch/cli.py` (the `EXAMPLE_CONFIG` string only), `README.md`. Depends on Task 1 for key names. These are comment and doc changes, so they need no new tests beyond one guard: add `test_example_configs_parse` to `tests/test_config.py`, unless an equivalent test already exists. It loads `config.example.yaml` and `cli.EXAMPLE_CONFIG` through `Config.model_validate(yaml.safe_load(...))`, so the new commented block cannot break either file. No `.gitignore` change (Decision 16). The `site:` block is also added to the tracked `config.yaml` and committed in the same commit (Decision 17); `committer_email` is left unset there.

**Changes:**

- `.env.example`: a `SITE_DEPLOY_KEY=` entry after the OpenReview block, with a comment saying it is the path to the private deploy key used to push the public site (for example `~/.ssh/paper-watch-site_deploy_key`), and that leaving it blank uses the SSH agent and default keys, without ever prompting.
- `config.example.yaml` and `cli.EXAMPLE_CONFIG`: a commented-out `#site:` block with the six keys (`repo_path`, `remote`, `branch`, `domain`, `committer_name`, `committer_email`) and one-line comments, in the style of the existing commented `#feedback_refresh:` block in `EXAMPLE_CONFIG`. In `EXAMPLE_CONFIG` it goes right after that block; in `config.example.yaml` it goes after the `schedule:` block.
- `README.md` (Claude-maintained, Decision 11), matching the surrounding style:
  1. A `site` bullet under "Technical Setup" in the `config.yaml` section, covering the keys, that an absent block turns publishing off, and that publish runs once per tick when a digest was sent or a feedback refresh succeeded.
  2. A `SITE_DEPLOY_KEY` bullet under the `.env` section.
  3. A failure-mode paragraph after the SMTP one: a revoked or missing deploy key makes publishes fail; the tick still sends the digest; the failure goes to the operator alert channels (alerts log, desktop, operator email and Slack) and is recorded under the `site` health row; it never appears in the digest, because the digest goes to the reading group.
  4. A `paper-watch publish` bullet under "Other commands", mentioning `--dry-run`.

**Commit:** `docs: document the public site, deploy key, and publish command`

---

## Task 8: out-of-repo setup (primary session only)

The primary session runs this task, not an implementation agent, after Task 5 is on the branch. These commands create a public repo, a credential, and a DNS record, so the primary session confirms with the user before running them. Commands run from `/home/jkop/Code/paper-watch/.worktrees/feat-site-publish` unless stated otherwise.

1. Create the repo.
   ```bash
   gh repo create jkopczyn/paper-watch-site --public \
     --description "Papers suggested by paper-watch (generated; do not edit by hand)"
   ```

2. Generate the deploy key outside any repo, register it with write access, and record its path in `.env`.
   ```bash
   ssh-keygen -t ed25519 -N "" -C "paper-watch-site deploy key" -f ~/.ssh/paper-watch-site_deploy_key
   chmod 600 ~/.ssh/paper-watch-site_deploy_key
   gh repo deploy-key add ~/.ssh/paper-watch-site_deploy_key.pub \
     --repo jkopczyn/paper-watch-site --allow-write --title "paper-watch timer"
   python3 - <<'PY'
   from pathlib import Path
   env = Path.home() / "Code/paper-watch/.env"
   text = env.read_text() if env.exists() else ""
   if "SITE_DEPLOY_KEY=" not in text:
       if text and not text.endswith("\n"):
           text += "\n"
       text += f"SITE_DEPLOY_KEY={Path.home()}/.ssh/paper-watch-site_deploy_key\n"
       env.write_text(text)
   PY
   ssh-keygen -F github.com
   ```
   If the last command prints nothing, github.com is missing from `known_hosts`. Because publish runs ssh with `BatchMode=yes`, it will fail on host-key verification rather than prompt, so add the host key before going on.

3. Clone over SSH with the deploy key, so the remote URL is the SSH form that `GIT_SSH_COMMAND` applies to, and point the unborn branch at `main` regardless of the local `init.defaultBranch`.
   ```bash
   GIT_SSH_COMMAND="ssh -o BatchMode=yes -i $HOME/.ssh/paper-watch-site_deploy_key -o IdentitiesOnly=yes" \
     git clone git@github.com:jkopczyn/paper-watch-site.git ~/Code/paper-watch-site
   git -C ~/Code/paper-watch-site symbolic-ref HEAD refs/heads/main
   ```

4. Seed the repo by hand and enable Pages. The live database and config are in the main checkout, and a relative `repo_path` would resolve against the worktree, so seed with a scratchpad copy of `~/Code/paper-watch/config.yaml` that has `db_path: /home/jkop/Code/paper-watch/paper_watch.db` and a `site:` block with `repo_path: /home/jkop/Code/paper-watch-site` and `committer_email` set (or confirm `git config user.email` is set in the clone). Run from the worktree, since only the branch has the `publish` command, with `SITE_DEPLOY_KEY` exported in the shell because the worktree has no `.env`.
   ```bash
   export SITE_DEPLOY_KEY=$HOME/.ssh/paper-watch-site_deploy_key
   uv run paper-watch publish --config <scratchpad>/config.site-seed.yaml --dry-run
   uv run paper-watch publish --config <scratchpad>/config.site-seed.yaml
   gh api -X POST repos/jkopczyn/paper-watch-site/pages \
     -f 'source[branch]=main' -f 'source[path]=/'
   ```
   The `site:` block is already in `config.yaml` on the branch (Task 7, Decision 17), so the main checkout receives it at merge time. If the clone has no `user.email`, add `committer_email` to the block on the branch before merging.

5. Add the DNS record as DNS-only, set the custom domain, wait for the certificate, then enforce HTTPS.

   **Blocked on the user first.** The `cf` CLI is currently signed in to a Cloudflare account that does not hold the `cupchinski.com` zone. Before running anything in this step, ask the user to sign in to the account that owns the zone (for example with `cf auth login`, or by selecting the right profile and passing `--profile`), then confirm with `cf dns records list -z cupchinski.com` that the zone is visible. Do not create the record until that list command succeeds against the right zone. Steps 1 to 4 do not depend on Cloudflare and can run before the user signs in.
   ```bash
   cf dns records list -z cupchinski.com
   cf dns records create -z cupchinski.com \
     --body '{"type":"CNAME","name":"papers","content":"jkopczyn.github.io","proxied":false,"ttl":1}'
   gh api -X PUT repos/jkopczyn/paper-watch-site/pages -f cname=papers.cupchinski.com
   gh api repos/jkopczyn/paper-watch-site/pages --jq '.https_certificate.state'
   gh api -X PUT repos/jkopczyn/paper-watch-site/pages -F https_enforced=true
   curl -sI https://papers.cupchinski.com | head -1
   ```
   Check the `list` output for an existing `papers` record before creating one. Repeat the certificate-state query until it reports `approved` before enforcing HTTPS. GitHub recommends verifying the custom domain on the account to prevent takeover of a dangling record; that needs a TXT record whose value is shown only in the GitHub web settings, so it is left as an optional manual step for the user.

6. The systemd timer runs from the main checkout, so it starts publishing only after `feat/site-publish` is merged to `main`. Steps 1 to 4 should be done before that merge; otherwise every publish fails and sends an operator alert on each send or refresh tick. Step 5 only affects whether the custom domain serves the site.

Record in the final report which of these steps ran, their outputs, and any permission denials.
