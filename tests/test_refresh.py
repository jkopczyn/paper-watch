"""The weekly feedback refresh: dueness, the export→import→notice chain, and
failure semantics (a failed refresh stays owed; at most one failure alert goes
out per owed refresh point).

Notice routing: a successful refresh writes a line to the alerts log file and
never emails anyone (the digest sender's to_addrs include the reading group's
Slack address, so refresh notices must not go through it). A failure fans out
through the operational-alert channels (alerts.send_alert), which reach only
the operator.
"""

import time as _time
from datetime import datetime, time, timezone

import pytest

from paper_watch.config import Config
from paper_watch.feedback import VoteImportResult
from paper_watch.refresh import is_refresh_due, run_feedback_refresh

from paper_watch.store import Store

THU = {3}
NOON = time(12, 0)


@pytest.fixture
def tz(monkeypatch):
    """Pin the process timezone; refresh times are local, so tests must be too."""

    def _set(name):
        monkeypatch.setenv("TZ", name)
        _time.tzset()

    yield _set
    monkeypatch.undo()
    _time.tzset()


def utc(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


# 2026-08-06 is a Thursday; so is 2026-08-13.


def test_refresh_due_thursday_after_noon(tz):
    tz("UTC")
    assert is_refresh_due(
        utc(2026, 8, 6, 12), "2026-07-30T12:00:00Z", days=THU, at=NOON
    )


def test_refresh_not_due_before(tz):
    tz("UTC")
    # Thursday morning: this week's point has not passed yet.
    assert not is_refresh_due(
        utc(2026, 8, 6, 9), "2026-07-30T12:00:00Z", days=THU, at=NOON
    )
    # Saturday, after a successful Thursday refresh: nothing owed.
    assert not is_refresh_due(
        utc(2026, 8, 8, 9), "2026-08-06T12:05:00Z", days=THU, at=NOON
    )


def test_missed_thursdays_collapse_to_one(tz):
    tz("UTC")
    # Machine off across two Thursdays: one refresh is owed, and one success
    # covers both missed points.
    assert is_refresh_due(
        utc(2026, 8, 14, 8), "2026-07-30T12:05:00Z", days=THU, at=NOON
    )
    assert not is_refresh_due(
        utc(2026, 8, 14, 8), "2026-08-13T14:00:00Z", days=THU, at=NOON
    )


class CapturingAlerts:
    """Stands in for alerts.send_alert; records (subject, body) per call."""

    def __init__(self):
        self.sent = []

    def __call__(self, cfg, subject, body, **kwargs):
        self.sent.append((cfg, subject, body))
        return {"log": None}


def _config(tmp_path) -> Config:
    return Config.model_validate(
        {
            "db_path": str(tmp_path / "pw.db"),
            "slack": {
                "workspaces": [
                    {
                        "name": "far",
                        "token_env": "SLACK_TOKEN_FAR",
                        "voting_channels": [{"id": "C05", "name": "polls"}],
                    }
                ]
            },
            "feedback_refresh": {
                "days": ["thu"],
                "at": "12:00",
                "workspace": "far",
                "groundtruth_path": str(tmp_path / "gt.csv"),
            },
            "alerts": {
                "log_file": str(tmp_path / "alerts.log"),
                "desktop": False,
                "email": False,
            },
        }
    )


def _log_text(cfg: Config) -> str:
    from pathlib import Path

    path = Path(cfg.alerts.log_file)
    return path.read_text() if path.exists() else ""


def test_refresh_runs_export_then_import_and_logs(tmp_path, monkeypatch, tz):
    tz("UTC")
    monkeypatch.setenv("SLACK_TOKEN_FAR", "xoxp-test")
    cfg = _config(tmp_path)
    store = Store(cfg.db_path)
    calls = []
    alert_send = CapturingAlerts()

    def fake_export(token, channel_ids, *, oldest, path, append=False):
        assert token == "xoxp-test"
        assert channel_ids == ["C05"]
        assert append is True
        assert str(path) == str(tmp_path / "gt.csv")
        calls.append("export")
        return 4

    def fake_import(store_arg, *, path, config, force_ts=frozenset()):
        assert store_arg is store
        calls.append("import")
        return VoteImportResult(
            imported=3, weeks=["2026-W31", "2026-W32"], weight_keys_touched=7
        )

    result = run_feedback_refresh(
        store,
        cfg,
        now=utc(2026, 8, 6, 12, 5),
        export=fake_export,
        importer=fake_import,
        alert_send=alert_send,
    )
    assert calls == ["export", "import"]
    assert result.performed and result.ok and result.notice_sent
    assert store.get_last_feedback_refresh_at() == "2026-08-06T12:05:00Z"
    # Success is a log line, never an alert fan-out (and never a digest email).
    assert alert_send.sent == []
    logged = _log_text(cfg)
    assert "feedback refresh" in logged
    assert "Appended 4" in logged
    assert "Imported 3 vote row(s)" in logged
    assert "2026-W31, 2026-W32" in logged
    assert "7 feedback weight key(s)" in logged


def test_refresh_export_failure_alerts_once_and_stays_owed(tmp_path, monkeypatch, tz):
    tz("UTC")
    monkeypatch.setenv("SLACK_TOKEN_FAR", "xoxp-test")
    cfg = _config(tmp_path)
    store = Store(cfg.db_path)
    alert_send = CapturingAlerts()

    def bad_export(token, channel_ids, *, oldest, path, append=False):
        raise RuntimeError("slack down")

    def never_import(store_arg, *, path, config, force_ts=frozenset()):  # pragma: no cover
        raise AssertionError("import must not run after a failed export")

    result = run_feedback_refresh(
        store, cfg, now=utc(2026, 8, 6, 12, 5),
        export=bad_export, importer=never_import, alert_send=alert_send,
    )
    assert result.performed and not result.ok
    assert result.notice_sent
    assert store.get_last_feedback_refresh_at() is None
    assert len(alert_send.sent) == 1
    # The alert goes to the operator channels with the alerts config, not the
    # digest recipients.
    assert alert_send.sent[0][0] is cfg.alerts
    assert "slack down" in alert_send.sent[0][2]

    # The 16:00 retry of the same owed point: still failing, but no new alert.
    result = run_feedback_refresh(
        store, cfg, now=utc(2026, 8, 6, 16, 5),
        export=bad_export, importer=never_import, alert_send=alert_send,
    )
    assert not result.ok and not result.notice_sent
    assert len(alert_send.sent) == 1
    assert store.get_last_feedback_refresh_at() is None

    # The 20:00 tick succeeds: log line, watermark advanced, no alert.
    result = run_feedback_refresh(
        store, cfg, now=utc(2026, 8, 6, 20, 5),
        export=lambda token, channel_ids, *, oldest, path, append=False: 0,
        importer=lambda store_arg, *, path, config, force_ts=frozenset(): VoteImportResult(),
        alert_send=alert_send,
    )
    assert result.ok and result.notice_sent
    assert len(alert_send.sent) == 1
    assert store.get_last_feedback_refresh_at() == "2026-08-06T20:05:00Z"
    assert "feedback refresh" in _log_text(cfg)

    # A failure at the NEXT owed point alerts again — the cap is per point.
    result = run_feedback_refresh(
        store, cfg, now=utc(2026, 8, 13, 12, 5),
        export=bad_export, importer=never_import, alert_send=alert_send,
    )
    assert not result.ok and result.notice_sent
    assert len(alert_send.sent) == 2


def test_refresh_failure_alert_that_fails_stays_unnoticed(tmp_path, monkeypatch, tz):
    """If no alert channel lands, the failure stays un-noticed and the next
    tick tries the alert again."""
    tz("UTC")
    monkeypatch.setenv("SLACK_TOKEN_FAR", "xoxp-test")
    cfg = _config(tmp_path)
    store = Store(cfg.db_path)
    attempts = []

    def all_channels_fail(cfg_arg, subject, body, **kwargs):
        attempts.append(subject)
        return {"log": "OSError: disk full"}

    def bad_export(token, channel_ids, *, oldest, path, append=False):
        raise RuntimeError("slack down")

    result = run_feedback_refresh(
        store, cfg, now=utc(2026, 8, 6, 12, 5),
        export=bad_export, alert_send=all_channels_fail,
    )
    assert not result.ok and not result.notice_sent
    result = run_feedback_refresh(
        store, cfg, now=utc(2026, 8, 6, 16, 5),
        export=bad_export, alert_send=all_channels_fail,
    )
    assert not result.ok and not result.notice_sent
    assert len(attempts) == 2


def test_refresh_missing_token_is_a_failure(tmp_path, monkeypatch, tz):
    tz("UTC")
    monkeypatch.delenv("SLACK_TOKEN_FAR", raising=False)
    cfg = _config(tmp_path)
    store = Store(cfg.db_path)
    alert_send = CapturingAlerts()

    result = run_feedback_refresh(
        store, cfg, now=utc(2026, 8, 6, 12, 5),
        export=lambda *a, **kw: 0,
        importer=lambda *a, **kw: VoteImportResult(),
        alert_send=alert_send,
    )
    assert result.performed and not result.ok
    assert store.get_last_feedback_refresh_at() is None
    assert "SLACK_TOKEN_FAR" in alert_send.sent[0][2]


def test_refresh_notice_lists_ties_and_unresolved(tmp_path, monkeypatch, tz):
    tz("UTC")
    monkeypatch.setenv("SLACK_TOKEN_FAR", "xoxp-test")
    cfg = _config(tmp_path)
    store = Store(cfg.db_path)

    def fake_import(store_arg, *, path, config, force_ts=frozenset()):
        return VoteImportResult(
            imported=1,
            skipped_zero=2,
            weeks=["2026-W32"],
            unresolved=1,
            unresolved_urls=["https://example.test/unknown-paper"],
            ties=["2026-W30"],
        )

    run_feedback_refresh(
        store, cfg, now=utc(2026, 8, 6, 12, 5),
        export=lambda token, channel_ids, *, oldest, path, append=False: 0,
        importer=fake_import,
    )
    logged = _log_text(cfg)
    assert "2026-W30" in logged  # the tie awaiting a human call
    assert "https://example.test/unknown-paper" in logged
    assert "2 zero-vote" in logged


def test_refresh_detects_hand_edits_and_snapshots_on_success(tmp_path, monkeypatch, tz):
    tz("UTC")
    monkeypatch.setenv("SLACK_TOKEN_FAR", "xoxp-test")
    cfg = _config(tmp_path)
    store = Store(cfg.db_path)
    hdr = "week,message_ts,option,emoji,votes,url,context\n"
    csv_path = tmp_path / "gt.csv"
    snap_path = tmp_path / "gt.csv.imported"
    # Snapshot from the last refresh; the CSV has since been hand-corrected.
    snap_path.write_text(hdr + "2026-W27,111.0,1,one,1,https://x/a,A\n")
    csv_path.write_text(hdr + "2026-W27,111.0,1,one,5,https://x/a,A\n")

    seen = {}

    def fake_export(token, channel_ids, *, oldest, path, append=False):
        return 0

    def fake_import(store_arg, *, path, config, force_ts=frozenset()):
        seen["force_ts"] = set(force_ts)
        return VoteImportResult(reimported=1)

    result = run_feedback_refresh(
        store, cfg, now=utc(2026, 8, 6, 12, 5),
        export=fake_export, importer=fake_import,
    )
    assert result.ok
    assert seen["force_ts"] == {"111.0"}
    # Snapshot refreshed to match the CSV, so the edit is not re-detected.
    assert snap_path.read_text() == csv_path.read_text()
    assert "hand-edited" in _log_text(cfg)


def test_refresh_failure_leaves_snapshot_untouched(tmp_path, monkeypatch, tz):
    tz("UTC")
    monkeypatch.setenv("SLACK_TOKEN_FAR", "xoxp-test")
    cfg = _config(tmp_path)
    store = Store(cfg.db_path)
    (tmp_path / "gt.csv").write_text("week,message_ts,option,emoji,votes,url,context\n")

    def bad_export(token, channel_ids, *, oldest, path, append=False):
        raise RuntimeError("slack down")

    result = run_feedback_refresh(
        store, cfg, now=utc(2026, 8, 6, 12, 5), export=bad_export,
        alert_send=CapturingAlerts(),
    )
    assert not result.ok
    assert not (tmp_path / "gt.csv.imported").exists()
