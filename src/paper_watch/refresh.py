"""The weekly feedback refresh: pull the reading group's poll votes into the
learning loop on a schedule, from inside the ordinary tick.

A refresh is a second scheduled duty alongside delivery and shares its dueness
machinery (`paper_watch.schedule`): each tick asks whether a refresh moment has
passed that the last *successful* refresh did not cover, so missed Thursdays
collapse into one catch-up run and a failed refresh stays owed until a later
tick lands it. A refresh = append-mode groundtruth export → vote import →
notice email; the import's idempotence is what makes those blind retries safe.

Failures never advance the watermark, and — to avoid a 4-hourly drumbeat while
one stays owed — at most one failure notice is mailed per owed refresh point
(tracked under `FEEDBACK_FAILURE_NOTICED_KEY` in `meta`). A notice email that
itself fails to send does not fail an otherwise-successful refresh: the spec
ties the watermark to the refresh, not the mail.
"""

from __future__ import annotations

import html
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, time, timezone

from paper_watch.config import Config
from paper_watch.dates import since_to_iso
from paper_watch.feedback import VoteImportResult, import_votes
from paper_watch.groundtruth import changed_polls, export_groundtruth
from paper_watch.schedule import is_delivery_due, last_delivery_at_or_before
from paper_watch.sources.slack import iso_to_ts
from paper_watch.store import FEEDBACK_FAILURE_NOTICED_KEY, Store

_ISO = "%Y-%m-%dT%H:%M:%SZ"

# How far back the export reaches when the CSV is empty or missing — the same
# default as the manual `paper-watch groundtruth --since` (append mode derives
# `oldest` from the file's own max ts whenever it has rows).
_EMPTY_CSV_LOOKBACK = "180d"

_log = logging.getLogger(__name__)


@dataclass
class RefreshResult:
    performed: bool = False
    ok: bool = False
    summary: str = ""
    notice_sent: bool = False


def is_refresh_due(
    now: datetime, last_refresh_at: str | None, *, days: set[int], at: time
) -> bool:
    """Is a feedback refresh owed right now?

    The delivery question against the refresh watermark: missed points collapse
    into one owed run, and a failure stays owed until a tick succeeds.
    """
    return is_delivery_due(now, last_refresh_at, days=days, at=at)


def _workspace_token(config: Config, workspace: str) -> tuple[str, list[str]]:
    """The workspace's Slack token + voting channel ids, resolved exactly as
    the `groundtruth` CLI does. Raises on anything missing — a refresh failure."""
    workspaces = config.slack.workspaces if config.slack else []
    ws = next((w for w in workspaces if w.name == workspace), None)
    if ws is None:
        raise RuntimeError(f"workspace {workspace!r} not in config.slack.workspaces")
    token = os.environ.get(ws.token_env)
    if not token:
        raise RuntimeError(f"no Slack token in env var {ws.token_env}")
    channel_ids = [ch.id for ch in ws.voting_channels]
    if not channel_ids:
        raise RuntimeError(f"no voting_channels configured for workspace {workspace!r}")
    return token, channel_ids


def render_notice(
    result: VoteImportResult | None, *, appended: int = 0, error: str | None = None
) -> str:
    """The refresh notice email body: what happened, or why nothing did."""
    if error is not None:
        return (
            "<p>Feedback refresh FAILED; it stays owed and later ticks will "
            f"retry it.</p>\n<p>Error: {html.escape(error)}</p>"
        )
    assert result is not None
    weeks = f": {html.escape(', '.join(result.weeks))}" if result.weeks else ""
    parts = [
        f"<p>Appended {appended} new poll option(s) to the groundtruth CSV.</p>",
        f"<p>Imported {result.imported} vote row(s) across {len(result.weeks)} "
        f"week(s){weeks}; touched {result.weight_keys_touched} feedback weight "
        "key(s).</p>",
        f"<p>Skipped {result.skipped_zero} zero-vote row(s) and "
        f"{result.skipped_existing} already-imported row(s).</p>",
        f"<p>Recorded {result.readings_recorded} reading(s); backfilled "
        f"{result.resolutions_backfilled} earlier resolution(s).</p>",
    ]
    if result.reimported:
        parts.append(
            f"<p>Re-imported {result.reimported} row(s) from hand-edited "
            "poll(s); their feedback rows and ledger winners were re-derived "
            "(the prior weight nudge decays rather than being unwound).</p>"
        )
    if result.ties:
        parts.append(
            "<p>Tie(s) awaiting a human call: "
            f"{html.escape(', '.join(result.ties))}</p>"
        )
    if result.unresolved_urls:
        items = "".join(f"<li>{html.escape(u)}</li>" for u in result.unresolved_urls)
        parts.append(f"<p>Unresolved URL(s):</p>\n<ul>{items}</ul>")
    return "\n".join(parts)


def _send_notice(sender, now: datetime, body: str) -> bool:
    try:
        sender.send(
            subject=f"paper-watch feedback refresh — {now:%Y-%m-%d}", html=body
        )
        return True
    except Exception as exc:  # a mail hiccup must not fail the refresh itself
        _log.warning("feedback refresh notice failed to send: %s", exc)
        return False


def run_feedback_refresh(
    store: Store,
    config: Config,
    sender,
    *,
    now: datetime,
    export=export_groundtruth,
    importer=import_votes,
) -> RefreshResult:
    """Export new polls, import their votes, and mail the notice.

    Success advances the refresh watermark (even if the notice mail fails);
    any export/import failure leaves it untouched and mails a failure notice
    at most once per owed refresh point.
    """
    fr = config.feedback_refresh
    result = RefreshResult(performed=True)
    appended = 0
    imported: VoteImportResult | None = None
    error: str | None = None
    # Hand edits since the last import are detected against a snapshot copy of
    # the CSV, taken below after each successful import — checked BEFORE the
    # export appends anything, so only human changes register.
    snapshot = str(fr.groundtruth_path) + ".imported"
    try:
        forced = changed_polls(fr.groundtruth_path, snapshot)
        token, channel_ids = _workspace_token(config, fr.workspace)
        appended = export(
            token,
            channel_ids,
            oldest=iso_to_ts(since_to_iso(_EMPTY_CSV_LOOKBACK, now=now)),
            path=fr.groundtruth_path,
            append=True,
        )
        imported = importer(
            store, path=fr.groundtruth_path, config=config, force_ts=forced
        )
        if os.path.exists(fr.groundtruth_path):
            shutil.copyfile(fr.groundtruth_path, snapshot)
    except Exception as exc:
        error = str(exc)
        _log.warning("feedback refresh failed: %s", exc)

    if error is None:
        result.ok = True
        result.summary = (
            f"appended {appended}, imported {imported.imported} "
            f"({len(imported.weeks)} week(s), {len(imported.ties)} tie(s), "
            f"{imported.unresolved} unresolved)"
        )
        result.notice_sent = _send_notice(
            sender, now, render_notice(imported, appended=appended)
        )
        store.set_last_feedback_refresh_at(now.strftime(_ISO))
        return result

    result.summary = f"feedback refresh failed: {error}"
    point = last_delivery_at_or_before(now, days=fr.weekdays, at=fr.at_time)
    point_iso = (
        point.astimezone(timezone.utc).strftime(_ISO) if point else "unscheduled"
    )
    if store.get_meta(FEEDBACK_FAILURE_NOTICED_KEY) != point_iso:
        result.notice_sent = _send_notice(sender, now, render_notice(None, error=error))
        if result.notice_sent:
            store.set_meta(FEEDBACK_FAILURE_NOTICED_KEY, point_iso)
    return result
