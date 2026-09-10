"""The weekly feedback refresh: pull the reading group's poll votes into the
learning loop on a schedule, from inside the ordinary tick.

A refresh is a second scheduled duty alongside delivery and shares its dueness
machinery (`paper_watch.schedule`): each tick asks whether a refresh moment has
passed that the last *successful* refresh did not cover, so missed Thursdays
collapse into one catch-up run and a failed refresh stays owed until a later
tick lands it. A refresh = append-mode groundtruth export → vote import →
notice; the import's idempotence is what makes those blind retries safe.

Notices are operator-facing and must never reach the digest recipients (the
digest's `to_addrs` include the reading group's Slack address). A successful
refresh writes one line to the alerts log file; a failed one fans out through
the operational-alert channels (`alerts.send_alert`), which are configured to
reach only the operator.

Failures never advance the watermark, and — to avoid a 4-hourly drumbeat while
one stays owed — at most one failure alert goes out per owed refresh point
(tracked under `FEEDBACK_FAILURE_NOTICED_KEY` in `meta`). A notice that itself
fails to record does not fail an otherwise-successful refresh: the spec ties
the watermark to the refresh, not the notice.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, time, timezone

from paper_watch import alerts
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
    # Success: the log line was written. Failure: at least one alert channel
    # landed (or none was owed because this point was already noticed).
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
    """The refresh notice body, plain text: what happened, or why nothing did."""
    if error is not None:
        return (
            "Feedback refresh FAILED; it stays owed and later ticks will "
            f"retry it.\nError: {error}"
        )
    assert result is not None
    weeks = f": {', '.join(result.weeks)}" if result.weeks else ""
    parts = [
        f"Appended {appended} new poll option(s) to the groundtruth CSV.",
        f"Imported {result.imported} vote row(s) across {len(result.weeks)} "
        f"week(s){weeks}; touched {result.weight_keys_touched} feedback weight "
        "key(s).",
        f"Skipped {result.skipped_zero} zero-vote row(s) and "
        f"{result.skipped_existing} already-imported row(s).",
        f"Recorded {result.readings_recorded} reading(s); backfilled "
        f"{result.resolutions_backfilled} earlier resolution(s).",
    ]
    if result.reimported:
        parts.append(
            f"Re-imported {result.reimported} row(s) from hand-edited "
            "poll(s); their feedback rows and ledger winners were re-derived "
            "(the prior weight nudge decays rather than being unwound)."
        )
    if result.ties:
        parts.append(f"Tie(s) awaiting a human call: {', '.join(result.ties)}")
    if result.unresolved_urls:
        parts.append(f"Unresolved URL(s): {', '.join(result.unresolved_urls)}")
    return "\n".join(parts)


def _log_notice(config: Config, now: datetime, body: str) -> bool:
    subject = f"feedback refresh — {now:%Y-%m-%d}"
    try:
        alerts.append_log(config.alerts.log_file, subject, body, now=now)
        return True
    except Exception as exc:  # a log hiccup must not fail the refresh itself
        _log.warning("feedback refresh notice failed to log: %s", exc)
        return False


def _default_alert_send(cfg, subject, body, *, config: Config, now: datetime):
    from paper_watch.delivery.email import GmailSender

    return alerts.send_alert(
        cfg,
        subject,
        body,
        smtp=config.smtp,
        sender=GmailSender(config.smtp, os.environ.get("SMTP_APP_PASSWORD", "")),
        slack_post=alerts.slack_poster(config),
        now=now,
    )


def run_feedback_refresh(
    store: Store,
    config: Config,
    *,
    now: datetime,
    export=export_groundtruth,
    importer=import_votes,
    alert_send=None,
) -> RefreshResult:
    """Export new polls, import their votes, and record the notice.

    Success advances the refresh watermark (even if the log line fails to
    write) and logs a one-line notice to the alerts log file; any export/import
    failure leaves the watermark untouched and raises an operational alert at
    most once per owed refresh point.
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
        body = render_notice(imported, appended=appended)
        result.notice_sent = _log_notice(config, now, body)
        _log.info("feedback refresh ok: %s", result.summary)
        store.set_last_feedback_refresh_at(now.strftime(_ISO))
        return result

    result.summary = f"feedback refresh failed: {error}"
    point = last_delivery_at_or_before(now, days=fr.weekdays, at=fr.at_time)
    point_iso = (
        point.astimezone(timezone.utc).strftime(_ISO) if point else "unscheduled"
    )
    if store.get_meta(FEEDBACK_FAILURE_NOTICED_KEY) != point_iso:
        subject = "feedback refresh failed"
        body = render_notice(None, error=error)
        if alert_send is None:
            outcome = _default_alert_send(
                config.alerts, subject, body, config=config, now=now
            )
        else:
            outcome = alert_send(config.alerts, subject, body, now=now)
        result.notice_sent = any(v is None for v in (outcome or {}).values())
        if result.notice_sent:
            store.set_meta(FEEDBACK_FAILURE_NOTICED_KEY, point_iso)
    return result
