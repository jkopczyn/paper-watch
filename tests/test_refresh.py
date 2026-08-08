"""The weekly feedback refresh: dueness, the export→import→notice chain, and
failure semantics (a failed refresh stays owed; at most one failure notice is
mailed per owed refresh point)."""

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


class CapturingSender:
    def __init__(self):
        self.sent = []

    def send(self, *, subject, html, to_addr=None):
        self.sent.append((subject, html))


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
        }
    )


def test_refresh_runs_export_then_import_and_mails(tmp_path, monkeypatch, tz):
    tz("UTC")
    monkeypatch.setenv("SLACK_TOKEN_FAR", "xoxp-test")
    cfg = _config(tmp_path)
    store = Store(cfg.db_path)
    calls = []

    def fake_export(token, channel_ids, *, oldest, path, append=False):
        assert token == "xoxp-test"
        assert channel_ids == ["C05"]
        assert append is True
        assert str(path) == str(tmp_path / "gt.csv")
        calls.append("export")
        return 4

    def fake_import(store_arg, *, path, config):
        assert store_arg is store
        calls.append("import")
        return VoteImportResult(
            imported=3, weeks=["2026-W31", "2026-W32"], weight_keys_touched=7
        )

    sender = CapturingSender()
    result = run_feedback_refresh(
        store,
        cfg,
        sender,
        now=utc(2026, 8, 6, 12, 5),
        export=fake_export,
        importer=fake_import,
    )
    assert calls == ["export", "import"]
    assert result.performed and result.ok and result.notice_sent
    assert store.get_last_feedback_refresh_at() == "2026-08-06T12:05:00Z"
    subject, html = sender.sent[0]
    assert subject == "paper-watch feedback refresh — 2026-08-06"
    assert "Appended 4" in html
    assert "Imported 3 vote row(s)" in html
    assert "2026-W31, 2026-W32" in html
    assert "7 feedback weight key(s)" in html


def test_refresh_export_failure_mails_once_and_stays_owed(tmp_path, monkeypatch, tz):
    tz("UTC")
    monkeypatch.setenv("SLACK_TOKEN_FAR", "xoxp-test")
    cfg = _config(tmp_path)
    store = Store(cfg.db_path)
    sender = CapturingSender()

    def bad_export(token, channel_ids, *, oldest, path, append=False):
        raise RuntimeError("slack down")

    def never_import(store_arg, *, path, config):  # pragma: no cover
        raise AssertionError("import must not run after a failed export")

    result = run_feedback_refresh(
        store, cfg, sender, now=utc(2026, 8, 6, 12, 5),
        export=bad_export, importer=never_import,
    )
    assert result.performed and not result.ok
    assert result.notice_sent
    assert store.get_last_feedback_refresh_at() is None
    assert len(sender.sent) == 1
    assert "slack down" in sender.sent[0][1]

    # The 16:00 retry of the same owed point: still failing, but no new mail.
    result = run_feedback_refresh(
        store, cfg, sender, now=utc(2026, 8, 6, 16, 5),
        export=bad_export, importer=never_import,
    )
    assert not result.ok and not result.notice_sent
    assert len(sender.sent) == 1
    assert store.get_last_feedback_refresh_at() is None

    # The 20:00 tick succeeds: normal notice, watermark advanced.
    result = run_feedback_refresh(
        store, cfg, sender, now=utc(2026, 8, 6, 20, 5),
        export=lambda token, channel_ids, *, oldest, path, append=False: 0,
        importer=lambda store_arg, *, path, config: VoteImportResult(),
    )
    assert result.ok and result.notice_sent
    assert len(sender.sent) == 2
    assert store.get_last_feedback_refresh_at() == "2026-08-06T20:05:00Z"

    # A failure at the NEXT owed point mails again — the cap is per point.
    result = run_feedback_refresh(
        store, cfg, sender, now=utc(2026, 8, 13, 12, 5),
        export=bad_export, importer=never_import,
    )
    assert not result.ok and result.notice_sent
    assert len(sender.sent) == 3


def test_refresh_missing_token_is_a_failure(tmp_path, monkeypatch, tz):
    tz("UTC")
    monkeypatch.delenv("SLACK_TOKEN_FAR", raising=False)
    cfg = _config(tmp_path)
    store = Store(cfg.db_path)
    sender = CapturingSender()

    result = run_feedback_refresh(
        store, cfg, sender, now=utc(2026, 8, 6, 12, 5),
        export=lambda *a, **kw: 0,
        importer=lambda *a, **kw: VoteImportResult(),
    )
    assert result.performed and not result.ok
    assert store.get_last_feedback_refresh_at() is None
    assert "SLACK_TOKEN_FAR" in sender.sent[0][1]


def test_refresh_notice_lists_ties_and_unresolved(tmp_path, monkeypatch, tz):
    tz("UTC")
    monkeypatch.setenv("SLACK_TOKEN_FAR", "xoxp-test")
    cfg = _config(tmp_path)
    store = Store(cfg.db_path)
    sender = CapturingSender()

    def fake_import(store_arg, *, path, config):
        return VoteImportResult(
            imported=1,
            skipped_zero=2,
            weeks=["2026-W32"],
            unresolved=1,
            unresolved_urls=["https://example.test/unknown-paper"],
            ties=["2026-W30"],
        )

    run_feedback_refresh(
        store, cfg, sender, now=utc(2026, 8, 6, 12, 5),
        export=lambda token, channel_ids, *, oldest, path, append=False: 0,
        importer=fake_import,
    )
    html = sender.sent[0][1]
    assert "2026-W30" in html  # the tie awaiting a human call
    assert "https://example.test/unknown-paper" in html
    assert "2 zero-vote" in html
