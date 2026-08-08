import json
from datetime import datetime, timedelta, timezone

import pytest

from paper_watch.config import (
    Config,
    ScoringWeights,
    SlackChannel,
    SlackConfig,
    SlackWorkspace,
)
from paper_watch.enrich import EnrichmentResult
from paper_watch.models import RawItem
from paper_watch.runtime import (
    _pub_display,
    _to_item,
    build_sources,
    effective_since,
    fresh_start,
    ingest,
    recover_titles,
    resolve_paper_metadata,
    rewrite_paper_metadata,
    run_pipeline,
    select_digest,
)
from paper_watch.sources.slack import SlackSource
from paper_watch.store import Store


class ListSource:
    def __init__(self, name, items):
        self.name = name
        self._items = items

    def fetch(self, since=None):
        return list(self._items)


class QueueSource:
    """Items queued between ticks; each fetch drains the queue and is counted,
    so a test can see exactly which ticks actually polled."""

    def __init__(self):
        self.pending = []
        self.fetches = 0

    def fetch(self, since=None):
        self.fetches += 1
        items, self.pending = self.pending, []
        return items


class FakeEnricher:
    def __init__(self, relevant=True):
        self.relevant = relevant

    def enrich(self, *, title, abstract, source, mentions):
        # "Irrelevant" is a weak fit (2, under the >=4 bar), not a non-artifact
        # (0): trust bypasses the fit bar only, so 0 would gate even trusted
        # items — see test_trusted_does_not_rescue_a_non_artifact.
        return EnrichmentResult(
            tldr=f"tldr:{title}",
            why="why",
            tags=["interp"],
            relevance=8 if self.relevant else 2,
        )


class CapturingSender:
    def __init__(self):
        self.sent = []

    def send(self, *, subject, html, to_addr=None):
        self.sent.append((subject, html))


def _arxiv_item(arxiv_id, title, when="2026-06-19T08:00:00Z"):
    return RawItem(
        source="arxiv",
        url=f"https://arxiv.org/abs/{arxiv_id}",
        title=title,
        authors=["Neel Nanda"],
        abstract="abstract",
        published_at=when,
    )


def test_ingest_dedups_across_sources(tmp_path):
    store = Store(tmp_path / "pw.db")
    arxiv = ListSource("arxiv", [_arxiv_item("2406.00001", "Shared Paper")])
    # a tweet linking the same arxiv id
    twitter = ListSource(
        "twitter",
        [RawItem(source="twitter:x", url="https://nitter/x/1", text="great https://arxiv.org/abs/2406.00001")],
    )

    new_ids = ingest(store, [arxiv, twitter], since=None, now_iso="2026-06-19T09:00:00Z")
    assert len(new_ids) == 1  # one entry, two mentions
    eid = new_ids[0]
    assert store.count_distinct_sources(eid) == 2
    store.close()


def test_ingest_dedups_same_tweet_across_nitter_instances(tmp_path):
    store = Store(tmp_path / "pw.db")
    tweet_text = "agenda https://arxiv.org/abs/2605.01642"
    run1 = ListSource(
        "twitter",
        [RawItem(source="twitter:x", url="https://nitter.net/x/status/207169#m", text=tweet_text)],
    )
    run2 = ListSource(
        "twitter",
        [RawItem(source="twitter:x", url="http://localhost/x/status/207169#m", text=tweet_text)],
    )

    ids1 = ingest(store, [run1], since=None, now_iso="2026-06-30T08:00:00Z")
    ids2 = ingest(store, [run2], since=None, now_iso="2026-06-30T19:00:00Z")
    assert len(ids1) == 1 and ids2 == []
    mentions = store.get_mentions(ids1[0])
    assert len(mentions) == 1  # URL variants collapse to one canonical mention
    assert mentions[0]["source_item_url"] == "https://twitter.com/x/status/207169"
    store.close()


def test_ingest_multi_link_slack_message_is_one_mention(tmp_path):
    store = Store(tmp_path / "pw.db")
    key = "slack://far/C001/1719.9"
    text = "paper + tweet + workshop links"
    items = [
        RawItem(source="slack:far:papers", url=u, text=f"{text} https://arxiv.org/abs/2605.01642", mention_url=key)
        for u in (
            "https://x.com/x/status/207169?s=20",
            "https://arxiv.org/abs/2605.01642",
            "https://pluralistic-alignment.github.io/#schedule",
        )
    ]
    new_ids = ingest(store, [ListSource("slack", items)], since=None, now_iso="2026-07-01T06:45:22Z")
    assert len(new_ids) == 1
    assert len(store.get_mentions(new_ids[0])) == 1
    store.close()


_ARXIV_META_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2605.01642v1</id>
    <link href="http://arxiv.org/abs/2605.01642v1" rel="alternate" type="text/html"/>
    <link title="pdf" href="http://arxiv.org/pdf/2605.01642v1" rel="related" type="application/pdf"/>
    <title>Adaptive Pluralistic Alignment</title>
    <summary>We propose a pipeline for dynamic artificial democracy.</summary>
    <author><name>Rachel Freedman</name></author>
    <published>2026-06-28T00:00:00Z</published>
  </entry>
</feed>
"""


def test_ingest_records_an_authoritative_date_for_the_work_itself(tmp_path):
    store = Store(tmp_path / "pw.db")
    item = _arxiv_item("2406.00050", "Dated Paper", when="2026-06-18T10:00:00Z")
    item.published_at_is_work_date = True
    ingest(store, [ListSource("arxiv", [item])], None, "2026-06-19T09:00:00Z")

    (entry_id,) = [r["id"] for r in store.conn.execute("SELECT id FROM entries")]
    assert store.get_entry(entry_id)["published_at"] == "2026-06-18T10:00:00Z"
    # Known exactly, so the digest shows it without the "~" estimate marker.
    assert _pub_display(store, store.get_entry(entry_id)) == ("2026-06", False)
    store.close()


def test_ingest_does_not_let_a_mention_date_masquerade_as_the_works(tmp_path):
    store = Store(tmp_path / "pw.db")
    # A Slack message linking a paper: the ts says when someone posted it, not
    # when the paper came out. Claiming that as authoritative would date a
    # 2024 paper to last Tuesday.
    msg = RawItem(
        source="slack:far:papers",
        url="https://arxiv.org/abs/2406.00051",
        title="Linked Paper",
        published_at="2026-07-30T00:00:00Z",
    )
    ingest(store, [ListSource("slack", [msg])], None, "2026-07-30T09:00:00Z")

    (entry_id,) = [r["id"] for r in store.conn.execute("SELECT id FROM entries")]
    assert store.get_entry(entry_id)["published_at"] is None
    # It still informs the estimate, which renders with a leading "~".
    assert _pub_display(store, store.get_entry(entry_id)) == ("2026-07", True)
    store.close()


def test_a_later_authoritative_date_fills_in_an_undated_entry(tmp_path):
    store = Store(tmp_path / "pw.db")
    tweet = RawItem(
        source="twitter:NeelNanda5",
        url="https://arxiv.org/abs/2406.00052",
        title="Tweeted Paper",
        published_at="2026-07-30T00:00:00Z",
    )
    ingest(store, [ListSource("twitter", [tweet])], None, "2026-07-30T09:00:00Z")

    # The arXiv feed then yields the same paper and does know its date.
    paper = _arxiv_item("2406.00052", "Tweeted Paper", when="2026-06-18T10:00:00Z")
    paper.published_at_is_work_date = True
    ingest(store, [ListSource("arxiv", [paper])], None, "2026-07-31T09:00:00Z")

    rows = list(store.conn.execute("SELECT id, published_at FROM entries"))
    assert len(rows) == 1  # same paper, not a duplicate
    assert rows[0]["published_at"] == "2026-06-18T10:00:00Z"
    store.close()


def test_an_authoritative_date_is_never_overwritten_by_a_later_one(tmp_path):
    store = Store(tmp_path / "pw.db")
    first = _arxiv_item("2406.00053", "Revised Paper", when="2026-06-18T10:00:00Z")
    first.published_at_is_work_date = True
    ingest(store, [ListSource("arxiv", [first])], None, "2026-06-19T09:00:00Z")

    # A later sighting reports the v2 revision date; the original submit date
    # is the one we already trusted, so it stands.
    revised = _arxiv_item("2406.00053", "Revised Paper", when="2026-07-01T10:00:00Z")
    revised.published_at_is_work_date = True
    ingest(store, [ListSource("arxiv", [revised])], None, "2026-07-02T09:00:00Z")

    rows = list(store.conn.execute("SELECT published_at FROM entries"))
    assert [r["published_at"] for r in rows] == ["2026-06-18T10:00:00Z"]
    store.close()


def test_resolve_paper_metadata_turns_post_into_paper(tmp_path):
    store = Store(tmp_path / "pw.db")
    tweet = RawItem(
        source="twitter:FreedmanRach",
        url="https://nitter.net/FreedmanRach/status/207169#m",
        text="My new research agenda: https://arxiv.org/abs/2605.01642",
    )
    new_ids = ingest(store, [ListSource("twitter", [tweet])], since=None, now_iso="2026-06-30T08:00:00Z")
    assert len(new_ids) == 1
    row = store.get_entry(new_ids[0])
    assert row["title"].startswith("My new research agenda")  # post-shaped

    updated = resolve_paper_metadata(store, new_ids, lambda url: _ARXIV_META_XML)
    assert updated == 1
    row = store.get_entry(new_ids[0])
    assert row["title"] == "Adaptive Pluralistic Alignment"
    assert row["abstract"].startswith("We propose")
    assert json.loads(row["authors_json"]) == ["Rachel Freedman"]
    assert json.loads(row["links_json"])["abstract"] == "http://arxiv.org/abs/2605.01642v1"
    # the authoritative publication date lands on the entry
    assert row["published_at"] == "2026-06-28T00:00:00Z"
    # the tweet survives as the mention
    assert store.get_mentions(new_ids[0])[0]["source"] == "twitter:FreedmanRach"
    store.close()


def test_resolve_paper_metadata_skips_entries_with_abstract(tmp_path):
    store = Store(tmp_path / "pw.db")
    new_ids = ingest(
        store,
        [ListSource("arxiv", [_arxiv_item("2605.01642", "Already Complete")])],
        since=None,
        now_iso="2026-06-30T08:00:00Z",
    )

    def boom(url):
        raise AssertionError("should not fetch")

    assert resolve_paper_metadata(store, new_ids, boom) == 0
    store.close()


_NOW = datetime(2026, 6, 19, 9, tzinfo=timezone.utc)


def test_effective_since_uses_lookback_when_never_polled():
    # No poll on record -> plain lookback window.
    assert effective_since(None, None, "7d", _NOW) == "2026-06-12T09:00:00Z"


def test_effective_since_widens_to_cover_gap_when_off():
    # Last poll was 20 days ago -> further back than the 7d lookback, so the
    # window widens to the last poll to cover the gap left by being powered off.
    assert effective_since("2026-05-30T09:00:00Z", None, "7d", _NOW) == "2026-05-30T09:00:00Z"


def test_effective_since_keeps_lookback_when_recent_poll():
    # A poll 12h ago is more recent than the 7d lookback; don't shrink the window.
    assert effective_since("2026-06-18T21:00:00Z", None, "7d", _NOW) == "2026-06-12T09:00:00Z"


def test_effective_since_explicit_override_ignores_last_poll():
    # An explicit --since wins over gap coverage.
    assert effective_since("2026-05-30T09:00:00Z", "2026-06-15T00:00:00Z", "7d", _NOW) == "2026-06-15T00:00:00Z"


def test_run_pipeline_dry_run_writes_digest(tmp_path):
    store = Store(tmp_path / "pw.db")
    arxiv = ListSource("arxiv", [_arxiv_item("2406.00001", "Oversight Paper")])
    sender = CapturingSender()

    result = run_pipeline(
        store,
        sources=[arxiv],
        enricher=FakeEnricher(),
        sender=sender,
        weights=ScoringWeights(),
        top_n=10,
        since="2026-06-01T00:00:00Z",
        candidate_window_days=21,
        resurface_window_days=21,
        now=__import__("datetime").datetime(2026, 6, 19, 9, tzinfo=__import__("datetime").timezone.utc),
        max_enrich=50,
        dry_run=True,
        out_dir=tmp_path / "out",
    )

    assert sender.sent == []  # dry-run does not send
    assert result.digest_path is not None
    html = result.digest_path.read_text()
    assert "Oversight Paper" in html
    assert "tldr:Oversight Paper" in html
    # dry-run does not record shown, so it can be re-run
    assert not store.was_shown(result.chosen_ids[0])
    store.close()


def test_run_pipeline_sends_and_records_when_not_dry(tmp_path):
    store = Store(tmp_path / "pw.db")
    arxiv = ListSource("arxiv", [_arxiv_item("2406.00002", "Sendable Paper")])
    sender = CapturingSender()

    result = run_pipeline(
        store,
        sources=[arxiv],
        enricher=FakeEnricher(),
        sender=sender,
        weights=ScoringWeights(),
        top_n=10,
        since="2026-06-01T00:00:00Z",
        candidate_window_days=21,
        resurface_window_days=21,
        now=__import__("datetime").datetime(2026, 6, 19, 9, tzinfo=__import__("datetime").timezone.utc),
        max_enrich=50,
        dry_run=False,
        out_dir=tmp_path / "out",
    )

    assert len(sender.sent) == 1
    subject, html = sender.sent[0]
    assert "Sendable Paper" in html
    assert store.was_shown(result.chosen_ids[0])
    store.close()


class ExplodingSender:
    def send(self, *, subject, html, to_addr=None):
        raise RuntimeError("smtp is down")


def _pipeline(store, sources, sender, **kw):
    """run_pipeline with the boilerplate a delivery-gating test doesn't care about."""
    kw.setdefault("now", datetime(2026, 8, 7, 12, tzinfo=timezone.utc))
    kw.setdefault("since", "2026-08-01T00:00:00Z")
    return run_pipeline(
        store,
        sources=sources,
        enricher=FakeEnricher(),
        sender=sender,
        weights=ScoringWeights(),
        top_n=20,
        candidate_window_days=7,
        resurface_window_days=21,
        max_enrich=50,
        **{
            "dry_run": False,
            "out_dir": "out",  # only touched by a dry run, which passes its own
            **kw,
        },
    )


def test_fresh_start_falls_back_to_new_window_before_any_delivery(tmp_path):
    store = Store(tmp_path / "pw.db")
    now = datetime(2026, 8, 7, 12, tzinfo=timezone.utc)
    assert fresh_start(store, "24h", now) == "2026-08-06T12:00:00Z"
    store.close()


def test_fresh_start_measures_from_the_last_delivered_digest(tmp_path):
    store = Store(tmp_path / "pw.db")
    store.set_last_sent_at("2026-08-04T12:00:00Z")
    now = datetime(2026, 8, 7, 12, tzinfo=timezone.utc)
    # The series opened at Tuesday's send, not 24h ago.
    assert fresh_start(store, "24h", now) == "2026-08-04T12:00:00Z"
    store.close()


def test_ingest_only_tick_fetches_but_does_not_deliver(tmp_path):
    store = Store(tmp_path / "pw.db")
    sender = CapturingSender()
    result = _pipeline(
        store,
        [ListSource("arxiv", [_arxiv_item("2408.00010", "Wednesday Paper")])],
        sender,
        now=datetime(2026, 8, 5, 8, tzinfo=timezone.utc),
        deliver=False,
    )

    assert result.new_count == 1 and result.enriched_count == 1
    assert sender.sent == []
    assert result.chosen_ids == []
    # Nothing was consumed: the paper is still unshown and still owed a digest.
    assert not store.was_shown(1)
    assert store.get_last_sent_at() is None
    store.close()


def test_a_paper_ingested_midweek_still_leads_the_next_digest(tmp_path):
    store = Store(tmp_path / "pw.db")
    sender = CapturingSender()
    store.set_last_sent_at("2026-08-04T12:00:00Z")  # Tuesday's digest

    _pipeline(
        store,
        [ListSource("arxiv", [_arxiv_item("2408.00011", "Midweek Paper")])],
        sender,
        now=datetime(2026, 8, 5, 8, tzinfo=timezone.utc),
        new_window="24h",
        deliver=False,
    )
    result = _pipeline(
        store,
        [ListSource("arxiv", [])],
        sender,
        now=datetime(2026, 8, 7, 12, tzinfo=timezone.utc),
        new_window="24h",
        deliver=True,
    )

    # Two days old — well outside the 24h fallback window — but the series began
    # at Tuesday's send, so Friday's digest still leads with it.
    assert len(sender.sent) == 1
    assert "Midweek Paper" in sender.sent[0][1]
    assert result.sent
    assert store.get_last_sent_at() == "2026-08-07T12:00:00Z"
    assert store.was_shown(result.chosen_ids[0])
    store.close()


def test_a_long_published_paper_reaches_the_email_marked_older(tmp_path):
    store = Store(tmp_path / "pw.db")
    sender = CapturingSender()
    old = _arxiv_item("2401.00001", "Long Published Paper", when="2025-01-15T00:00:00Z")
    _pipeline(store, [ListSource("arxiv", [old])], sender, old_after_days=90)

    assert len(sender.sent) == 1
    html = sender.sent[0][1]
    assert "Long Published Paper" in html
    # The arXiv mention carries the real submit date, so it is marked even with
    # no authoritative entries.published_at.
    assert "OLDER · 2025-01" in html
    store.close()


def test_a_recent_paper_is_not_marked_older(tmp_path):
    store = Store(tmp_path / "pw.db")
    sender = CapturingSender()
    fresh = _arxiv_item("2408.00002", "Fresh Paper", when="2026-08-01T00:00:00Z")
    _pipeline(store, [ListSource("arxiv", [fresh])], sender, old_after_days=90)

    assert "OLDER" not in sender.sent[0][1]
    store.close()


def test_a_dead_watched_page_is_flagged_in_the_digest(tmp_path):
    store = Store(tmp_path / "pw.db")
    sender = CapturingSender()
    key = "page:https://transluce.org/news"
    for _ in range(3):
        store.record_source_failure(
            key, label="Transluce", error="404 Not Found", at="2026-08-05T08:00:00Z"
        )

    result = _pipeline(
        store,
        [ListSource("arxiv", [_arxiv_item("2408.00020", "Some Paper")])],
        sender,
        alert_after_failures=3,
    )

    html = sender.sent[0][1]
    assert "1 source unhealthy" in html
    assert "Transluce" in html and "404 Not Found" in html
    assert [w.label for w in result.warnings] == ["Transluce"]
    store.close()


def test_a_briefly_flaky_page_is_not_flagged(tmp_path):
    store = Store(tmp_path / "pw.db")
    sender = CapturingSender()
    # One 429 is weather, not a dead source.
    store.record_source_failure(
        "page:https://x.example", label="Flaky", error="429", at="2026-08-05T08:00:00Z"
    )

    result = _pipeline(
        store,
        [ListSource("arxiv", [_arxiv_item("2408.00021", "Some Paper")])],
        sender,
        alert_after_failures=3,
    )

    assert result.warnings == []
    assert "unhealthy" not in sender.sent[0][1]
    store.close()


def test_a_failed_send_leaves_the_delivery_owed(tmp_path):
    store = Store(tmp_path / "pw.db")
    with pytest.raises(RuntimeError):
        _pipeline(
            store,
            [ListSource("arxiv", [_arxiv_item("2408.00012", "Undelivered Paper")])],
            ExplodingSender(),
            deliver=True,
        )

    # The watermark must not move, or the 16:00 retry would skip the digest and
    # the paper would never be shown.
    assert store.get_last_sent_at() is None
    assert not store.was_shown(1)
    store.close()


def test_gated_tick_skips_ingest_but_still_delivers(tmp_path):
    store = Store(tmp_path / "pw.db")
    sender = CapturingSender()
    # Friday morning's poll put the paper in the DB (and moved the watermark).
    _pipeline(
        store,
        [ListSource("arxiv", [_arxiv_item("2408.00030", "Polled Earlier", when="2026-08-07T07:00:00Z")])],
        CapturingSender(),
        now=datetime(2026, 8, 7, 8, tzinfo=timezone.utc),
        deliver=False,
    )
    assert store.get_last_polled_at() == "2026-08-07T08:00:00Z"

    source = QueueSource()
    result = _pipeline(store, [source], sender, deliver=True, do_ingest=False)
    assert source.fetches == 0
    assert result.polled is False
    assert len(sender.sent) == 1 and "Polled Earlier" in sender.sent[0][1]
    # A gated tick fetched nothing, so it must not move the poll watermark.
    assert store.get_last_polled_at() == "2026-08-07T08:00:00Z"
    store.close()


def test_failed_send_retry_reuses_db_content(tmp_path):
    store = Store(tmp_path / "pw.db")
    with pytest.raises(RuntimeError):
        _pipeline(
            store,
            [ListSource("arxiv", [_arxiv_item("2408.00012", "Undelivered Paper")])],
            ExplodingSender(),
            deliver=True,
        )
    # The noon poll is on record even though the send blew up: that is what
    # lets the 16:00 retry rebuild from the DB instead of re-fetching.
    assert store.get_last_polled_at() == "2026-08-07T12:00:00Z"

    sender = CapturingSender()
    source = QueueSource()
    result = _pipeline(
        store, [source], sender,
        now=datetime(2026, 8, 7, 16, tzinfo=timezone.utc),
        deliver=True, do_ingest=False,
    )
    assert source.fetches == 0
    assert result.sent and result.chosen_ids == [1]
    assert "Undelivered Paper" in sender.sent[0][1]
    store.close()


def test_gated_tick_still_enriches_the_backlog(tmp_path):
    store = Store(tmp_path / "pw.db")
    # A previously-polled entry that never got enriched (e.g. max_enrich hit).
    eid = store.insert_entry(
        title="Backlog Paper", title_norm="backlog paper",
        first_seen_at="2026-08-07T08:00:00Z",
    )
    store.add_mention(
        entry_id=eid, source="rss:Blog", fetched_at="2026-08-07T08:00:00Z",
        source_item_url="https://blog/backlog",
    )
    result = _pipeline(store, [QueueSource()], CapturingSender(), deliver=False, do_ingest=False)
    assert result.enriched_count == 1
    assert store.get_entry(eid)["relevance"] is not None
    store.close()


def test_dry_run_never_moves_the_poll_watermark(tmp_path):
    store = Store(tmp_path / "pw.db")
    _pipeline(
        store,
        [ListSource("arxiv", [_arxiv_item("2408.00031", "Previewed Poll")])],
        CapturingSender(),
        deliver=True,
        dry_run=True,
        out_dir=tmp_path / "out",
    )
    assert store.get_last_polled_at() is None
    store.close()


def test_an_empty_digest_does_not_count_as_delivered(tmp_path):
    store = Store(tmp_path / "pw.db")
    sender = CapturingSender()
    result = _pipeline(store, [ListSource("arxiv", [])], sender, deliver=True)

    assert sender.sent == []
    assert not result.sent
    # Nothing went out, so the delivery stays owed and later ticks retry it.
    assert store.get_last_sent_at() is None
    store.close()


def test_dry_run_never_moves_the_delivery_watermark(tmp_path):
    store = Store(tmp_path / "pw.db")
    sender = CapturingSender()
    _pipeline(
        store,
        [ListSource("arxiv", [_arxiv_item("2408.00013", "Previewed Paper")])],
        sender,
        deliver=True,
        dry_run=True,
        out_dir=tmp_path / "out",
    )
    assert store.get_last_sent_at() is None
    store.close()


def test_run_pipeline_gates_non_safety_newsletter_items(tmp_path):
    store = Store(tmp_path / "pw.db")
    # a newsletter item (not arxiv) that the enricher flags as NOT safety-relevant
    rss = ListSource(
        "rss",
        [RawItem(source="rss:Blog", url="https://blog/p1", title="Off-topic Post", text="no paper")],
    )
    sender = CapturingSender()

    result = run_pipeline(
        store,
        sources=[rss],
        enricher=FakeEnricher(relevant=False),
        sender=sender,
        weights=ScoringWeights(),
        top_n=10,
        since="2026-06-01T00:00:00Z",
        candidate_window_days=21,
        resurface_window_days=21,
        now=__import__("datetime").datetime(2026, 6, 19, 9, tzinfo=__import__("datetime").timezone.utc),
        max_enrich=50,
        dry_run=True,
        out_dir=tmp_path / "out",
    )
    # gated out -> nothing chosen
    assert result.chosen_ids == []
    store.close()


def test_run_pipeline_arxiv_bypasses_gate_even_if_flagged_irrelevant(tmp_path):
    store = Store(tmp_path / "pw.db")
    arxiv = ListSource("arxiv", [_arxiv_item("2406.00003", "Trusted Author Paper")])
    sender = CapturingSender()

    result = run_pipeline(
        store,
        sources=[arxiv],
        enricher=FakeEnricher(relevant=False),  # LLM says not relevant
        sender=sender,
        weights=ScoringWeights(),
        top_n=10,
        since="2026-06-01T00:00:00Z",
        candidate_window_days=21,
        resurface_window_days=21,
        now=__import__("datetime").datetime(2026, 6, 19, 9, tzinfo=__import__("datetime").timezone.utc),
        max_enrich=50,
        dry_run=True,
        out_dir=tmp_path / "out",
    )
    # arxiv author whitelist bypasses the gate
    assert len(result.chosen_ids) == 1
    store.close()


def _slack_item(url, *, trusted, title="Slack Paper"):
    return RawItem(
        source="slack:mats:papers",
        url=url,
        title=title,
        text=f"check this {url}",
        published_at="2026-06-19T08:00:00Z",
        trusted=trusted,
    )


def _run_slack(store, item, tmp_path):
    return run_pipeline(
        store,
        sources=[ListSource("slack", [item])],
        enricher=FakeEnricher(relevant=False),  # LLM says not relevant
        sender=CapturingSender(),
        weights=ScoringWeights(),
        top_n=10,
        since="2026-06-01T00:00:00Z",
        candidate_window_days=21,
        resurface_window_days=21,
        now=__import__("datetime").datetime(2026, 6, 19, 9, tzinfo=__import__("datetime").timezone.utc),
        max_enrich=50,
        dry_run=True,
        out_dir=tmp_path / "out",
    )


def _pdf_item(url, title=None):
    """A post that links a bare PDF: no title of its own, so the entry is born
    titled with its own URL until a resolver fills the real title in."""
    return RawItem(source="rss:AF", url=url, title=title, authors=[], abstract=None,
                   published_at="2026-07-10T00:00:00Z")


def test_reingesting_a_url_after_its_title_was_rewritten_does_not_duplicate(tmp_path):
    # The regression that put 100 duplicate rows in the live DB: an entry is
    # created titled with its URL, a resolver rewrites title_norm to the real
    # title, and the next run's title_norm lookup then misses -- so the same URL
    # spawns a brand-new entry every single run.
    store = Store(tmp_path / "pw.db")
    url = "https://ae.studio/research/modular-pretraining.pdf"

    ingest(store, [ListSource("rss:AF", [_pdf_item(url)])], None, "2026-07-10T00:00:00Z")
    (entry_id,) = [r["id"] for r in store.conn.execute("SELECT id FROM entries")]

    # the PDF resolver lands the real title, clobbering the URL-derived title_norm
    store.update_paper_metadata(
        entry_id, title="Modular Pretraining Enables Access Control",
        title_norm="modular pretraining enables access control",
        authors=[], abstract="abs", links={},
    )

    # next run sees the very same item again
    ingest(store, [ListSource("rss:AF", [_pdf_item(url)])], None, "2026-07-10T12:00:00Z")

    ids = [r["id"] for r in store.conn.execute("SELECT id FROM entries")]
    assert ids == [entry_id], f"re-ingest spawned a duplicate: {ids}"
    store.close()


def test_metadata_rewrite_merges_into_an_existing_twin(tmp_path):
    # Same paper reached by two different URLs in one run (the AF post and the
    # arXiv link). They only become recognisably the same once metadata resolution
    # lands the real title on the second -- at which point it must merge, not twin.
    store = Store(tmp_path / "pw.db")
    post = store.insert_entry(
        title="Modular Pretraining Enables Access Control",
        title_norm="modular pretraining enables access control",
        first_seen_at="2026-07-11T00:00:00Z",
    )
    twin = store.insert_entry(
        title="https://arxiv.org/abs/2607.08077",
        title_norm="https arxiv org abs 2607 08077",
        first_seen_at="2026-07-11T00:00:00Z",
        arxiv_id="2607.08077",
    )
    store.add_mention(
        entry_id=twin, source="rss:AF", fetched_at="2026-07-11T00:00:00Z",
        source_item_url="https://arxiv.org/abs/2607.08077",
    )

    rewrite_paper_metadata(
        store, twin,
        title="Modular Pretraining Enables Access Control",
        authors=[], abstract="abs", links={},
    )

    ids = [r["id"] for r in store.conn.execute("SELECT id FROM entries ORDER BY id")]
    assert ids == [post], f"expected a merge into {post}, got {ids}"
    # the merged-away entry's provenance and identity survive on the winner
    assert store.get_entry(post)["arxiv_id"] == "2607.08077"
    assert len(store.get_mentions(post)) == 1
    store.close()


def test_a_merged_away_url_still_resolves_to_the_survivor(tmp_path):
    # After a merge the loser is gone, but its URL is still out there in the feed.
    # If the survivor didn't inherit it, the next run would re-create the entry,
    # re-resolve it and merge it away again -- burning a PDF fetch and an LLM
    # enrichment every run, forever.
    store = Store(tmp_path / "pw.db")
    pdf = "https://ae.studio/research/modular-pretraining.pdf"

    ingest(store, [ListSource("rss:AF", [_pdf_item(pdf)])], None, "2026-07-10T00:00:00Z")
    (loser,) = [r["id"] for r in store.conn.execute("SELECT id FROM entries")]
    winner = store.insert_entry(
        title="Modular Pretraining Enables Access Control",
        title_norm="modular pretraining enables access control",
        first_seen_at="2026-07-09T00:00:00Z",
        source_url="https://alignmentforum.org/posts/xyz",
    )
    store.merge_entries(winner_id=winner, loser_id=loser)

    ingest(store, [ListSource("rss:AF", [_pdf_item(pdf)])], None, "2026-07-11T00:00:00Z")

    ids = [r["id"] for r in store.conn.execute("SELECT id FROM entries")]
    assert ids == [winner], f"the loser's URL re-created an entry: {ids}"
    store.close()


def test_two_pdfs_resolving_to_a_generic_title_are_not_merged(tmp_path):
    # Two different Anthropic system cards, two different CDN URLs, and the PDF
    # resolver extracts "System Card" from both. They are not the same paper and
    # must not be merged away into one.
    store = Store(tmp_path / "pw.db")
    ids = []
    for slug in ("0f0c97ad", "2f9323ab"):
        ingest(
            store,
            [ListSource("rss:AF", [_pdf_item(f"https://www-cdn.anthropic.com/{slug}.pdf")])],
            None,
            "2026-07-10T00:00:00Z",
        )
    for row in store.conn.execute("SELECT id FROM entries ORDER BY id"):
        ids.append(row["id"])
    assert len(ids) == 2

    for entry_id in ids:
        rewrite_paper_metadata(
            store, entry_id, title="System Card", authors=[], abstract="abs", links={}
        )

    survivors = [r["id"] for r in store.conn.execute("SELECT id FROM entries ORDER BY id")]
    assert survivors == ids, f"the two system cards were fused: {survivors}"
    store.close()


def _shown_entry_with_mentions(store, n_occasions, *, citations=None):
    """An already-shown arxiv paper mentioned on `n_occasions` separate days,
    plus an optional pair of citation measurements (prev, latest)."""
    entry_id = store.insert_entry(
        title="Language Models are Few-Shot Learners",
        title_norm="language models are few shot learners",
        first_seen_at="2026-06-01T00:00:00Z",
        arxiv_id="2005.14165",
    )
    for i in range(n_occasions):
        store.add_mention(
            entry_id=entry_id,
            source="arxiv",
            fetched_at=f"2026-07-{10 + i:02d}T00:00:00Z",
            source_item_url=f"https://arxiv.org/abs/2005.14165#{i}",
            published_at=f"2026-07-{10 + i:02d}T00:00:00Z",
        )
    if citations:
        prev, latest = citations
        store.record_metrics(entry_id, prev, "2026-07-08T00:00:00Z")
        store.record_metrics(entry_id, latest, "2026-07-12T00:00:00Z")
    store.record_shown(
        entry_id=entry_id, digest_at="2026-07-09T00:00:00Z", rank=1, score=3.0,
        resurfaced=False,
    )
    return entry_id


def _select(store, **kw):
    kw.setdefault("top_n", 10)
    return select_digest(
        store,
        ScoringWeights(),
        candidate_start="2026-07-06T00:00:00Z",
        resurface_start="2026-06-22T00:00:00Z",
        **kw,
    )


def test_citation_drift_alone_does_not_resurface_a_shown_paper(tmp_path):
    # A famous paper's citation count ticks up on nearly every measurement.
    # That is not fresh attention, so it must not drag the paper back into the
    # digest run after run.
    store = Store(tmp_path / "pw.db")
    _shown_entry_with_mentions(store, 1, citations=(19000, 19040))
    assert _select(store) == []
    store.close()


def test_two_new_mentions_do_resurface_a_shown_paper(tmp_path):
    # Genuinely renewed attention still brings a paper back.
    store = Store(tmp_path / "pw.db")
    entry_id = _shown_entry_with_mentions(store, 2)
    chosen = _select(store)
    assert [c["entry_id"] for c in chosen] == [entry_id]
    assert chosen[0]["resurfaced"] is True
    store.close()


def _shown_entry(store, mentions):
    """`mentions` is a list of (source, fetched_at, url) making up the window."""
    entry_id = store.insert_entry(
        title="Modular Pretraining Enables Access Control",
        title_norm="modular pretraining enables access control",
        first_seen_at="2026-07-01T00:00:00Z",
    )
    for source, fetched_at, url in mentions:
        store.add_mention(
            entry_id=entry_id, source=source, fetched_at=fetched_at,
            source_item_url=url, published_at=fetched_at,
        )
    # clear the relevance gate, so these tests turn on the surge rule alone
    store.set_enrichment(
        entry_id, tldr="t", why="w", tags=[], relevance=8, version=2
    )
    store.record_shown(
        entry_id=entry_id, digest_at="2026-07-09T00:00:00Z", rank=1, score=3.0,
        resurfaced=False,
    )
    return entry_id


def test_one_post_linking_a_paper_three_ways_is_not_a_surge(tmp_path):
    # An AF post that links the paper as the post, the arXiv abs and the PDF
    # produces three mention rows -- but it is one source, on one day, saying one
    # thing. That is not renewed attention and must not resurface the paper.
    store = Store(tmp_path / "pw.db")
    _shown_entry(store, [
        ("rss:AF", "2026-07-10T01:00:00Z", "https://alignmentforum.org/posts/xyz"),
        ("rss:AF", "2026-07-10T01:00:00Z", "https://arxiv.org/abs/2607.08077"),
        ("rss:AF", "2026-07-10T01:00:00Z", "https://ae.studio/modular.pdf"),
    ])
    assert _select(store) == []
    store.close()


def test_two_sources_on_one_day_is_a_surge(tmp_path):
    store = Store(tmp_path / "pw.db")
    _shown_entry(store, [
        ("rss:AF", "2026-07-10T01:00:00Z", "https://alignmentforum.org/posts/xyz"),
        ("slack:far:papers", "2026-07-10T02:00:00Z", "slack://far/C1/1.2"),
    ])
    assert len(_select(store)) == 1
    store.close()


def test_one_source_on_two_days_is_a_surge(tmp_path):
    store = Store(tmp_path / "pw.db")
    _shown_entry(store, [
        ("rss:AF", "2026-07-10T01:00:00Z", "https://alignmentforum.org/posts/xyz"),
        ("rss:AF", "2026-07-12T01:00:00Z", "https://alignmentforum.org/posts/abc"),
    ])
    assert len(_select(store)) == 1
    store.close()


def test_resurface_min_mentions_raises_the_surge_bar(tmp_path):
    # Two mentions resurface at the default bar of 2, but not at 3.
    store = Store(tmp_path / "pw.db")
    _shown_entry_with_mentions(store, 2)
    assert len(_select(store, resurface_min_mentions=2)) == 1
    assert _select(store, resurface_min_mentions=3) == []
    store.close()


def _new_entry(store, key, *, n_mentions=1, relevance=8, fetched_at="2026-07-10T00:00:00Z"):
    """A never-shown, freshly-mentioned paper. More mentions ⇒ higher velocity
    ⇒ higher score, which lets a test order new items deterministically."""
    entry_id = store.insert_entry(
        title=f"New Paper {key}",
        title_norm=f"new paper {key}",
        first_seen_at=fetched_at,
    )
    for i in range(n_mentions):
        store.add_mention(
            entry_id=entry_id, source="rss:Blog", fetched_at=fetched_at,
            source_item_url=f"https://blog/{key}/{i}",
        )
    store.set_enrichment(entry_id, tldr="t", why="w", tags=[], relevance=relevance, version=2)
    return entry_id


def test_new_items_are_capped_at_max_new_extras_dropped(tmp_path):
    store = Store(tmp_path / "pw.db")
    # 12 new papers with strictly increasing scores (1..12 mentions).
    ids = {n: _new_entry(store, f"m{n}", n_mentions=n) for n in range(1, 13)}
    chosen = _select(store, new_start="2026-07-06T00:00:00Z", max_new=10, top_n=15)
    assert len(chosen) == 10
    picked = {c["entry_id"] for c in chosen}
    # the two lowest-scored new papers (1 and 2 mentions) are dropped
    assert ids[1] not in picked and ids[2] not in picked
    assert ids[12] in picked and ids[3] in picked
    store.close()


def test_never_shown_paper_outside_new_window_is_not_selected(tmp_path):
    store = Store(tmp_path / "pw.db")
    # mentioned 2026-07-07 — inside the 21d candidate window but before new_start.
    _new_entry(store, "old", fetched_at="2026-07-07T00:00:00Z")
    chosen = _select(store, new_start="2026-07-09T00:00:00Z", max_new=10, top_n=15)
    assert chosen == []
    store.close()


def test_resurfaced_below_new_average_is_dropped(tmp_path):
    store = Store(tmp_path / "pw.db")
    # Strong new items (relevance 10, many mentions) push the average high.
    for n in range(1, 4):
        _new_entry(store, f"hi{n}", n_mentions=8, relevance=10)
    # A weak resurfacing classic (minimum surge) scores below it.
    _shown_entry(store, [
        ("rss:AF", "2026-07-10T01:00:00Z", "https://af/x"),
        ("rss:AF", "2026-07-12T01:00:00Z", "https://af/y"),
    ])
    chosen = _select(store, new_start="2026-07-06T00:00:00Z", max_new=10, top_n=15)
    assert all(not c["resurfaced"] for c in chosen)
    store.close()


def test_resurfaced_above_new_average_pads_the_digest(tmp_path):
    store = Store(tmp_path / "pw.db")
    # One modest new item (relevance 5) sets a low average.
    _new_entry(store, "lo", n_mentions=1, relevance=5)
    # A strong resurfacing paper (relevance 10) clears it and pads the digest.
    strong = _shown_entry_with_mentions(store, 2)
    store.set_enrichment(strong, tldr="t", why="w", tags=[], relevance=10, version=2)
    chosen = _select(store, new_start="2026-07-06T00:00:00Z", max_new=10, top_n=15)
    assert any(c["resurfaced"] and c["entry_id"] == strong for c in chosen)
    store.close()


def test_select_digest_uses_ramped_feedback_weight(tmp_path):
    """As feedback weeks accumulate, w.feedback ramps 2->4, so a paper whose
    learned key weight is positive scores strictly higher than with no feedback."""

    def build_and_score(weeks):
        store = Store(tmp_path / f"pw{weeks}.db")
        eid = _new_entry(store, "fb", n_mentions=1, relevance=8)
        # _new_entry's only feedback key is its source ("rss:Blog"); a strong
        # positive learned weight makes feedback_affinity ~= +1.
        store.set_feedback_weight("source", "rss:Blog", 5.0)
        for w in range(weeks):
            store.record_feedback(
                entry_id=eid, week=f"2026-W{w:02d}", picked=True,
                group_rating=None, notes=None, imported_at="2026-07-10T00:00:00Z",
            )
        chosen = _select(store, new_start="2026-07-06T00:00:00Z", max_new=10, top_n=15)
        score = next(c["score"] for c in chosen if c["entry_id"] == eid)
        store.close()
        return score

    # 10 weeks of feedback -> w.feedback 3.0 vs 2.0 at zero weeks (~+1 to score).
    assert build_and_score(10) > build_and_score(0)


def test_fewer_than_max_new_still_pads_to_top_n_with_resurfaced(tmp_path):
    store = Store(tmp_path / "pw.db")
    news = [_new_entry(store, f"n{n}", n_mentions=1, relevance=5) for n in range(2)]
    # three strong resurfacing papers, all above the modest new average
    resurf = []
    for i in range(3):
        eid = store.insert_entry(
            title=f"Classic {i}", title_norm=f"classic {i}",
            first_seen_at="2026-06-01T00:00:00Z",
        )
        for d in (10, 12):
            store.add_mention(
                entry_id=eid, source=f"rss:AF{i}", fetched_at=f"2026-07-{d}T00:00:00Z",
                source_item_url=f"https://af/{i}/{d}",
            )
        store.set_enrichment(eid, tldr="t", why="w", tags=[], relevance=10, version=2)
        store.record_shown(entry_id=eid, digest_at="2026-07-08T00:00:00Z", rank=1, score=3.0, resurfaced=False)
        resurf.append(eid)
    chosen = _select(store, new_start="2026-07-06T00:00:00Z", max_new=10, top_n=5)
    assert len(chosen) == 5
    picked = {c["entry_id"] for c in chosen}
    assert set(news) <= picked
    assert len({c["entry_id"] for c in chosen if c["resurfaced"]}) == 3
    store.close()


def _strong_resurfacer(store, i: int) -> int:
    """An already-shown paper surging again, scored well above any new item."""
    entry_id = store.insert_entry(
        title=f"Classic {i}", title_norm=f"classic {i}",
        first_seen_at="2026-06-01T00:00:00Z",
    )
    for d in (10, 12):
        store.add_mention(
            entry_id=entry_id, source=f"rss:AF{i}", fetched_at=f"2026-07-{d}T00:00:00Z",
            source_item_url=f"https://af/{i}/{d}",
        )
    store.set_enrichment(entry_id, tldr="t", why="w", tags=[], relevance=10, version=2)
    store.record_shown(
        entry_id=entry_id, digest_at="2026-07-08T00:00:00Z", rank=1, score=3.0,
        resurfaced=False,
    )
    return entry_id


def test_resurfaced_are_capped_at_max_resurface(tmp_path):
    store = Store(tmp_path / "pw.db")
    # A quiet series: two modest new papers, eight classics all clearing the bar.
    _new_entry(store, "n1", n_mentions=1, relevance=5)
    _new_entry(store, "n2", n_mentions=1, relevance=5)
    for i in range(8):
        _strong_resurfacer(store, i)
    chosen = _select(
        store, new_start="2026-07-06T00:00:00Z", max_new=20, top_n=20, max_resurface=5
    )
    # Room for 18 more, but resurfaced papers must never take more than 5 slots.
    assert sum(1 for c in chosen if c["resurfaced"]) == 5
    assert len(chosen) == 7
    store.close()


def test_max_resurface_keeps_the_highest_scoring_classics(tmp_path):
    store = Store(tmp_path / "pw.db")
    _new_entry(store, "n1", n_mentions=1, relevance=5)
    weak = _strong_resurfacer(store, 0)
    strong = [_strong_resurfacer(store, i) for i in (1, 2)]
    # Give the two keepers an extra source, which lifts their overlap term.
    for entry_id in strong:
        store.add_mention(
            entry_id=entry_id, source="slack:far:papers",
            fetched_at="2026-07-11T00:00:00Z",
            source_item_url=f"https://slack/{entry_id}",
        )
    chosen = _select(
        store, new_start="2026-07-06T00:00:00Z", max_new=20, top_n=20, max_resurface=2
    )
    resurfaced = {c["entry_id"] for c in chosen if c["resurfaced"]}
    assert resurfaced == set(strong)
    assert weak not in resurfaced
    store.close()


OLD_BEFORE = "2026-04-13T00:00:00Z"  # ~3 months before the tests' 2026-07 "now"


def _set_published(store, entry_id, iso):
    store.conn.execute(
        "UPDATE entries SET published_at = ? WHERE id = ?", (iso, entry_id)
    )
    store.conn.commit()


def _old_entry(store, key, *, published_at="2025-11-01T00:00:00Z", relevance=5):
    """A never-shown paper that is fresh to *us* but long since published."""
    entry_id = store.insert_entry(
        title=f"Old {key}", title_norm=f"old {key}",
        first_seen_at="2026-07-10T00:00:00Z",
    )
    _set_published(store, entry_id, published_at)
    store.add_mention(
        entry_id=entry_id, source="rss:Blog", fetched_at="2026-07-10T00:00:00Z",
        source_item_url=f"https://blog/{key}",
    )
    store.set_enrichment(
        entry_id, tldr="t", why="w", tags=[], relevance=relevance, version=2
    )
    return entry_id


def test_a_long_published_paper_is_padding_not_a_lead(tmp_path):
    store = Store(tmp_path / "pw.db")
    old = _old_entry(store, "a")
    fresh = _new_entry(store, "n1", n_mentions=1, relevance=5)
    chosen = _select(
        store, new_start="2026-07-06T00:00:00Z", max_new=20, top_n=20,
        max_resurface=5, old_before=OLD_BEFORE,
    )
    by_id = {c["entry_id"]: c for c in chosen}
    assert by_id[old]["is_old"]
    assert not by_id[fresh]["is_old"]
    # It is not a repeat, so it gets no resurface boost and is not recorded as one.
    assert by_id[old]["resurfaced"] is False
    assert by_id[old]["features"].resurfaced is False
    store.close()


def test_a_long_published_paper_does_not_consume_a_new_slot(tmp_path):
    store = Store(tmp_path / "pw.db")
    olds = [_old_entry(store, f"o{i}") for i in range(3)]
    fresh = [_new_entry(store, f"n{i}", n_mentions=1, relevance=5) for i in range(2)]
    # Only two lead slots: the fresh pair takes both, the old papers pad instead.
    chosen = _select(
        store, new_start="2026-07-06T00:00:00Z", max_new=2, top_n=20,
        max_resurface=5, old_before=OLD_BEFORE,
    )
    picked = {c["entry_id"] for c in chosen}
    assert set(fresh) <= picked
    assert set(olds) <= picked
    store.close()


def test_long_published_papers_count_against_the_resurface_cap(tmp_path):
    store = Store(tmp_path / "pw.db")
    _new_entry(store, "n1", n_mentions=1, relevance=5)
    for i in range(4):
        _old_entry(store, f"o{i}")
    for i in range(4):
        _strong_resurfacer(store, i)
    chosen = _select(
        store, new_start="2026-07-06T00:00:00Z", max_new=20, top_n=20,
        max_resurface=5, old_before=OLD_BEFORE,
    )
    padding = [c for c in chosen if c["is_old"] or c["resurfaced"]]
    # One shared cap: old papers and reruns compete for the same 5 slots.
    assert len(padding) == 5
    assert len(chosen) == 6
    store.close()


def test_a_long_published_paper_survives_a_strong_fresh_crop(tmp_path):
    store = Store(tmp_path / "pw.db")
    # A high-scoring fresh crop that a weak old paper cannot possibly outscore.
    for i in range(3):
        _new_entry(store, f"hi{i}", n_mentions=8, relevance=10)
    # relevance 4 is the lowest that clears the relevance gate, so this tests
    # the outscore bar rather than accidentally re-testing the gate.
    old = _old_entry(store, "quiet", relevance=4)
    chosen = _select(
        store, new_start="2026-07-06T00:00:00Z", max_new=20, top_n=20,
        max_resurface=5, old_before=OLD_BEFORE,
    )
    # Nothing else will ever surface it, so it skips the "beat the fresh crop" bar.
    assert old in {c["entry_id"] for c in chosen}
    store.close()


def test_an_already_shown_old_paper_still_faces_the_bar(tmp_path):
    store = Store(tmp_path / "pw.db")
    for i in range(3):
        _new_entry(store, f"hi{i}", n_mentions=8, relevance=10)
    # A genuine classic: long published AND already shown. Age must not become a
    # loophole that readmits every stale favourite on a minimum surge.
    classic = _shown_entry(store, [
        ("rss:AF", "2026-07-10T01:00:00Z", "https://af/x"),
        ("rss:AF", "2026-07-12T01:00:00Z", "https://af/y"),
    ])
    _set_published(store, classic, "2024-01-01T00:00:00Z")
    chosen = _select(
        store, new_start="2026-07-06T00:00:00Z", max_new=20, top_n=20,
        max_resurface=5, old_before=OLD_BEFORE,
    )
    assert classic not in {c["entry_id"] for c in chosen}
    store.close()


def test_age_falls_back_to_the_estimated_publication_date(tmp_path):
    store = Store(tmp_path / "pw.db")
    # No authoritative published_at; the earliest mention carries a 2025 date.
    entry_id = store.insert_entry(
        title="Undated", title_norm="undated", first_seen_at="2026-07-10T00:00:00Z"
    )
    store.add_mention(
        entry_id=entry_id, source="rss:Blog", fetched_at="2026-07-10T00:00:00Z",
        source_item_url="https://blog/undated", published_at="2025-09-01T00:00:00Z",
    )
    store.set_enrichment(entry_id, tldr="t", why="w", tags=[], relevance=5, version=2)
    chosen = _select(
        store, new_start="2026-07-06T00:00:00Z", max_new=20, top_n=20,
        max_resurface=5, old_before=OLD_BEFORE,
    )
    assert next(c for c in chosen if c["entry_id"] == entry_id)["is_old"]
    store.close()


def test_old_before_unset_marks_nothing(tmp_path):
    store = Store(tmp_path / "pw.db")
    old = _old_entry(store, "a")
    chosen = _select(store, new_start="2026-07-06T00:00:00Z", max_new=20, top_n=20)
    assert not next(c for c in chosen if c["entry_id"] == old)["is_old"]
    store.close()


def test_max_resurface_unset_means_no_cap(tmp_path):
    store = Store(tmp_path / "pw.db")
    _new_entry(store, "n1", n_mentions=1, relevance=5)
    for i in range(6):
        _strong_resurfacer(store, i)
    chosen = _select(store, new_start="2026-07-06T00:00:00Z", max_new=20, top_n=20)
    assert sum(1 for c in chosen if c["resurfaced"]) == 6
    store.close()


class _StubWebSearch:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def resolve(self, url, blurb=None):
        self.calls.append((url, blurb))
        return self.result


def test_recover_titles_recovers_a_url_only_entry(tmp_path):
    store = Store(tmp_path / "pw.db")
    # a bare-URL entry: title is the URL, no abstract
    eid = store.insert_entry(
        title="https://dead.link/paper", title_norm="https dead link paper",
        first_seen_at="2026-07-10T00:00:00Z",
        links={"abstract": "https://dead.link/paper"},
    )
    store.add_mention(
        entry_id=eid, source="twitter:x", fetched_at="2026-07-10T00:00:00Z",
        source_item_url="https://dead.link/paper", mention_text="cool paper on oversight",
    )
    resolver = _StubWebSearch({"title": "Scalable Oversight of AI", "snippet": "A method.", "abstract": "We propose..."})
    assert recover_titles(store, [eid], resolver) == 1
    row = store.get_entry(eid)
    assert row["title"] == "Scalable Oversight of AI"
    assert row["abstract"] == "We propose..."
    # it searched the entry's URL and passed the mention blurb as context
    assert resolver.calls == [("https://dead.link/paper", "cool paper on oversight")]
    store.close()


def test_recover_titles_skips_entries_that_already_have_a_title(tmp_path):
    store = Store(tmp_path / "pw.db")
    eid = store.insert_entry(
        title="A Perfectly Good Title", title_norm="a perfectly good title",
        first_seen_at="2026-07-10T00:00:00Z", abstract="Has an abstract too.",
    )
    resolver = _StubWebSearch({"title": "WRONG"})
    assert recover_titles(store, [eid], resolver) == 0
    assert resolver.calls == []
    assert store.get_entry(eid)["title"] == "A Perfectly Good Title"
    store.close()


class _StubSearch:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def search(self, title):
        self.calls.append(title)
        return self.result


def test_resolve_fills_blank_url_via_title_search(tmp_path):
    store = Store(tmp_path / "pw.db")
    # an entry with no http link at all (links_json empty)
    eid = store.insert_entry(
        title="Impossibility Results for Fairness", title_norm="impossibility results for fairness",
        first_seen_at="2026-07-10T00:00:00Z", links={},
    )
    resolver = _StubSearch({
        "url": "https://arxiv.org/abs/1810.08810", "arxiv_id": "1810.08810",
        "doi": None, "published_at": "2018-10-19T00:00:00Z",
        "title": "Impossibility Results for Fairness", "authors": ["A"], "abstract": "We show",
    })
    updated = resolve_paper_metadata(store, [eid], None, search_resolver=resolver)
    assert updated == 1
    row = store.get_entry(eid)
    assert json.loads(row["links_json"])["abstract"] == "https://arxiv.org/abs/1810.08810"
    assert row["published_at"] == "2018-10-19T00:00:00Z"
    assert resolver.calls == ["Impossibility Results for Fairness"]
    store.close()


def test_resolve_does_not_search_when_entry_already_has_a_link(tmp_path):
    store = Store(tmp_path / "pw.db")
    eid = store.insert_entry(
        title="Some Blog Post", title_norm="some blog post",
        first_seen_at="2026-07-10T00:00:00Z",
        links={"abstract": "https://blog.example/post"},
    )
    resolver = _StubSearch({"url": "https://wrong.example"})
    resolve_paper_metadata(store, [eid], None, search_resolver=resolver)
    assert resolver.calls == []  # a real link is never overwritten by a search
    assert json.loads(store.get_entry(eid)["links_json"])["abstract"] == "https://blog.example/post"
    store.close()


def _item_for(store, entry_id):
    chosen = _select(store, new_start="2026-07-06T00:00:00Z", max_new=10, top_n=15)
    c = next(c for c in chosen if c["entry_id"] == entry_id)
    return _to_item(store, c, recent_start="2026-07-09T00:00:00Z")


def test_to_item_uses_authoritative_pub_date_exactly(tmp_path):
    store = Store(tmp_path / "pw.db")
    eid = _new_entry(store, "p")
    store.update_paper_metadata(
        eid, title="P", title_norm="new paper p", authors=[], abstract="x",
        links={}, published_at="2018-10-05T00:00:00Z",
    )
    item = _item_for(store, eid)
    assert item.pub_display == "2018-10" and item.pub_is_estimate is False


def test_to_item_estimates_pub_date_from_mentions(tmp_path):
    store = Store(tmp_path / "pw.db")
    eid = store.insert_entry(
        title="New Paper q", title_norm="new paper q",
        first_seen_at="2026-07-10T00:00:00Z",
    )
    store.add_mention(
        entry_id=eid, source="rss:Blog", fetched_at="2026-07-10T00:00:00Z",
        source_item_url="https://blog/q", published_at="2020-03-01T00:00:00Z",
    )
    store.set_enrichment(eid, tldr="t", why="w", tags=[], relevance=8, version=2)
    item = _item_for(store, eid)
    assert item.pub_display == "2020-03" and item.pub_is_estimate is True


def test_to_item_tags_sources_trust_and_recency(tmp_path):
    store = Store(tmp_path / "pw.db")
    eid = store.insert_entry(
        title="New Paper r", title_norm="new paper r",
        first_seen_at="2026-07-10T00:00:00Z",
    )
    store.add_mention(
        entry_id=eid, source="arxiv", fetched_at="2026-07-10T00:00:00Z",
        source_item_url="https://arxiv.org/abs/2607.1",
    )
    store.add_mention(
        entry_id=eid, source="slack:far:papers", fetched_at="2026-07-10T00:00:00Z",
        source_item_url="slack://far/C1/1.2", trusted=True,
    )
    store.set_enrichment(eid, tldr="t", why="w", tags=[], relevance=8, version=2)
    # surfaced twice inside the recent window (see _item_for), once before it
    store.record_shown(entry_id=eid, digest_at="2026-07-09T08:00:00Z", rank=1, score=1.0, resurfaced=False)
    store.record_shown(entry_id=eid, digest_at="2026-07-09T20:00:00Z", rank=1, score=1.0, resurfaced=False)
    store.record_shown(entry_id=eid, digest_at="2026-07-01T00:00:00Z", rank=1, score=1.0, resurfaced=False)
    item = _item_for(store, eid)
    # full source labels, not just the type prefix
    assert item.sources == ["arxiv", "slack:far:papers"]
    assert item.trusted is True
    assert item.surfaced_recent == 2


def test_to_item_fills_blank_links_from_owned_url(tmp_path):
    store = Store(tmp_path / "pw.db")
    eid = store.insert_entry(
        title="New Paper s", title_norm="new paper s",
        first_seen_at="2026-07-10T00:00:00Z", links={},
    )
    store.add_mention(
        entry_id=eid, source="twitter:x", fetched_at="2026-07-10T00:00:00Z",
        source_item_url="https://twitter.com/x/status/1",
    )
    store.set_enrichment(eid, tldr="t", why="w", tags=[], relevance=8, version=2)
    item = _item_for(store, eid)
    assert item.links == {"link": "https://twitter.com/x/status/1"}


def test_trusted_slack_item_bypasses_gate(tmp_path):
    store = Store(tmp_path / "pw.db")
    item = _slack_item("https://some-blog.example/post", trusted=True)
    result = _run_slack(store, item, tmp_path)
    # trusted mention bypasses the gate even though enricher flagged irrelevant
    assert len(result.chosen_ids) == 1
    store.close()


def test_untrusted_slack_item_is_gated(tmp_path):
    store = Store(tmp_path / "pw.db")
    item = _slack_item("https://some-blog.example/post", trusted=False)
    result = _run_slack(store, item, tmp_path)
    assert result.chosen_ids == []
    store.close()


def test_build_sources_includes_slack_when_configured():
    cfg = Config(
        slack=SlackConfig(
            workspaces=[
                SlackWorkspace(
                    name="mats",
                    token_env="SLACK_TOKEN_MATS",
                    ingestion_channels=[SlackChannel(id="C1", name="papers")],
                )
            ]
        )
    )
    sources = build_sources(cfg)
    assert any(isinstance(s, SlackSource) for s in sources)


def test_build_sources_omits_slack_when_no_workspaces():
    assert not any(isinstance(s, SlackSource) for s in build_sources(Config()))
    cfg = Config(slack=SlackConfig(workspaces=[]))
    assert not any(isinstance(s, SlackSource) for s in build_sources(cfg))


def test_build_sources_includes_pages_only_with_a_store(tmp_path):
    from paper_watch.config import PageConfig
    from paper_watch.sources.page_watch import PageWatchSource

    cfg = Config(pages=[PageConfig(name="TC", url="https://tc.example/")])
    # no store (unit-test wiring): the diff has nowhere to keep its state
    assert not any(isinstance(s, PageWatchSource) for s in build_sources(cfg))

    store = Store(tmp_path / "pw.db")
    sources = build_sources(cfg, store=store)
    assert any(isinstance(s, PageWatchSource) for s in sources)
    store.close()


def test_slack_dedups_and_trust_propagates_across_sources(tmp_path):
    # A blog posts a paper (flagged not-relevant by the LLM) AND someone drops
    # the same arXiv link in a trusted Slack channel. They dedup to one entry,
    # and the trusted Slack mention bypasses the gate.
    store = Store(tmp_path / "pw.db")
    rss = ListSource(
        "rss",
        [RawItem(source="rss:Blog", url="https://blog/x", title="Same Paper",
                 text="see https://arxiv.org/abs/2406.09999")],
    )
    slack = ListSource(
        "slack",
        [RawItem(source="slack:mats:papers", url="https://arxiv.org/abs/2406.09999",
                 text="cool https://arxiv.org/abs/2406.09999", trusted=True,
                 published_at="2026-06-19T08:00:00Z")],
    )
    result = run_pipeline(
        store,
        sources=[rss, slack],
        enricher=FakeEnricher(relevant=False),
        sender=CapturingSender(),
        weights=ScoringWeights(),
        top_n=10,
        since="2026-06-01T00:00:00Z",
        candidate_window_days=21,
        resurface_window_days=21,
        now=__import__("datetime").datetime(2026, 6, 19, 9, tzinfo=__import__("datetime").timezone.utc),
        max_enrich=50,
        dry_run=True,
        out_dir=tmp_path / "out",
    )
    assert len(result.chosen_ids) == 1  # one deduped entry, kept via trusted bypass
    assert store.count_distinct_sources(result.chosen_ids[0]) == 2
    store.close()


# -- link resolution (tweet augment, newsletter fan-out, metadata dispatch) --
class _StubTweetResolver:
    """augment() unconditionally injects an arXiv id, standing in for Nitter."""

    def augment(self, raw):
        from dataclasses import replace

        return replace(raw, text=f"{raw.text or ''} https://arxiv.org/abs/2605.01642")


class _StubMetaResolver:
    def __init__(self, meta):
        self.meta = meta
        self.seen = []

    def resolve(self, url):
        self.seen.append(url)
        return self.meta


def test_ingest_augments_tweet_then_resolves_metadata(tmp_path):
    store = Store(tmp_path / "pw.db")
    tweet = RawItem(source="slack:x", url="https://twitter.com/h/status/111", text="great thread")
    new_ids = ingest(
        store,
        [ListSource("slack", [tweet])],
        since=None,
        now_iso="2026-06-30T08:00:00Z",
        tweet_resolver=_StubTweetResolver(),
    )
    assert len(new_ids) == 1
    assert store.get_entry(new_ids[0])["arxiv_id"] == "2605.01642"  # id recovered at ingest
    resolve_paper_metadata(store, new_ids, lambda url: _ARXIV_META_XML)
    assert store.get_entry(new_ids[0])["title"] == "Adaptive Pluralistic Alignment"
    store.close()


def test_ingest_newsletter_fans_out_without_identity_hijack(tmp_path, fixture_text):
    from paper_watch.sources.newsletter_links import extract_paper_links

    domains = ["arxiv.org", "openreview.net"]
    newsletter = RawItem(
        source="rss:Import AI",
        url="https://newsletter.example/1",
        title="Import AI #401",
        text=fixture_text("newsletter_body.html"),
        extract_ids_from_text=False,
    )
    new_ids = ingest(
        store := Store(tmp_path / "pw.db"),
        [ListSource("rss", [newsletter])],
        since=None,
        now_iso="2026-06-30T08:00:00Z",
        newsletter_extractor=lambda raw: extract_paper_links(raw, domains),
    )
    entries = [store.get_entry(i) for i in new_ids]
    # the newsletter itself + the papers it links; the newsletter did NOT adopt an id
    newsletter_entry = next(e for e in entries if e["title"] == "Import AI #401")
    assert newsletter_entry["arxiv_id"] is None
    paper = store.get_entry_by_arxiv_id("2606.08243")
    assert paper is not None
    assert store.get_mentions(paper["id"])[0]["source"] == "rss:Import AI"  # provenance
    store.close()


def test_resolve_paper_metadata_dispatches_openreview_and_pdf(tmp_path):
    store = Store(tmp_path / "pw.db")
    items = [
        RawItem(source="slack:x", url="https://openreview.net/forum?id=dy2HwmOvFX", text="oversight"),
        RawItem(source="slack:x", url="https://aibetrayal.com/paper.pdf", text=None),
    ]
    new_ids = ingest(store, [ListSource("slack", items)], since=None, now_iso="2026-06-30T08:00:00Z")
    orv = _StubMetaResolver({"title": "OR Paper", "abstract": "or abstract", "authors": ["A"]})
    pdf = _StubMetaResolver({"title": "PDF Paper", "abstract": "pdf abstract"})

    updated = resolve_paper_metadata(store, new_ids, None, openreview_resolver=orv, pdf_resolver=pdf)
    assert updated == 2
    assert orv.seen == ["https://openreview.net/forum?id=dy2HwmOvFX"]
    assert pdf.seen == ["https://aibetrayal.com/paper.pdf"]
    titles = {store.get_entry(i)["title"] for i in new_ids}
    assert {"OR Paper", "PDF Paper"} <= titles
    store.close()


def test_resolve_paper_metadata_dispatches_html_pages(tmp_path):
    store = Store(tmp_path / "pw.db")
    items = [
        # an HTML landing page, titled with its own URL at ingest
        RawItem(source="rss:AF", url="https://www.anthropic.com/research/off-switch", text=None),
        # a PDF and an arXiv link must NOT be routed to the HTML resolver
        RawItem(source="rss:AF", url="https://x.example/paper.pdf", text=None),
        RawItem(source="rss:AF", url="https://arxiv.org/abs/2406.01234", text=None),
    ]
    new_ids = ingest(store, [ListSource("rss:AF", items)], since=None, now_iso="2026-07-01T00:00:00Z")
    html = _StubMetaResolver({"title": "Off-Switch for Dual-Use Knowledge", "abstract": "a"})
    pdf = _StubMetaResolver({"title": "PDF Paper", "abstract": "p"})

    resolve_paper_metadata(store, new_ids, None, pdf_resolver=pdf, html_resolver=html)

    assert html.seen == ["https://www.anthropic.com/research/off-switch"]
    assert pdf.seen == ["https://x.example/paper.pdf"]  # pdf still to pdf
    titles = {store.get_entry(i)["title"] for i in new_ids}
    assert "Off-Switch for Dual-Use Knowledge" in titles
    store.close()


def test_resolve_lands_publication_date_from_html_and_pdf(tmp_path):
    store = Store(tmp_path / "pw.db")
    items = [
        RawItem(source="rss:AF", url="https://blog.example/post", text=None),
        RawItem(source="rss:AF", url="https://x.example/paper.pdf", text=None),
    ]
    new_ids = ingest(store, [ListSource("rss:AF", items)], since=None, now_iso="2026-07-01T00:00:00Z")
    html = _StubMetaResolver(
        {"title": "Dated Post", "abstract": "a", "published_at": "2019-03-11T00:00:00Z"}
    )
    pdf = _StubMetaResolver(
        {"title": "Dated PDF", "abstract": "p", "published_at": "2020-05-02T00:00:00Z"}
    )
    resolve_paper_metadata(store, new_ids, None, pdf_resolver=pdf, html_resolver=html)

    by_title = {store.get_entry(i)["title"]: store.get_entry(i) for i in new_ids}
    assert by_title["Dated Post"]["published_at"] == "2019-03-11T00:00:00Z"
    assert by_title["Dated PDF"]["published_at"] == "2020-05-02T00:00:00Z"
    store.close()


def test_reresolve_reprocesses_entries_that_already_have_an_abstract(tmp_path):
    # The 8 PDF-furniture entries have a correct abstract but a junk title (the
    # old parser got the body right, the title wrong). The normal skip-if-abstract
    # short-circuit would leave them; reresolve=True forces them back through.
    store = Store(tmp_path / "pw.db")
    items = [RawItem(source="rss:AF", url="https://x.example/paper.pdf", text=None)]
    (entry_id,) = ingest(store, [ListSource("rss:AF", items)], None, "2026-07-01T00:00:00Z")
    store.update_paper_metadata(
        entry_id, title="Vol.:(0123456789)", title_norm="vol 0123456789",
        authors=[], abstract="a real abstract from the old run", links={},
    )
    pdf = _StubMetaResolver({"title": "The Real Title of the Paper", "abstract": "abs"})

    # default: skipped because it already has an abstract
    assert resolve_paper_metadata(store, [entry_id], None, pdf_resolver=pdf) == 0
    assert pdf.seen == []

    # reresolve: forced through
    assert resolve_paper_metadata(store, [entry_id], None, pdf_resolver=pdf, reresolve=True) == 1
    assert store.get_entry(entry_id)["title"] == "The Real Title of the Paper"
    store.close()


class _NullResolver:
    def resolve(self, url):
        return None  # API gated / unreachable


def test_openreview_fallback_flags_medium_high(tmp_path):
    store = Store(tmp_path / "pw.db")
    item = RawItem(
        source="rss:Import AI",
        url="https://openreview.net/forum?id=dy2HwmOvFX",
        text="A Structured Study of Oversight",  # the link's blurb
        extract_ids_from_text=True,
    )
    new_ids = ingest(store, [ListSource("rss", [item])], since=None, now_iso="2026-06-30T08:00:00Z")
    resolve_paper_metadata(store, new_ids, None, openreview_resolver=_NullResolver())
    row = store.get_entry(new_ids[0])
    assert row["relevance"] == 8  # medium-high prior, survives (won't be re-enriched down)
    assert row["title"] == "A Structured Study of Oversight"  # link metadata promoted
    # and it now passes the gate on relevance alone
    from paper_watch.runtime import _passes_gate

    assert _passes_gate(row, {"rss:Import AI"}, trusted=False)
    store.close()


# -- readings-ledger exclusion (read papers stay out of the digest) --------
def _record_reading(store, *, message_ts, url="https://example.com/read",
                    arxiv_id=None, title_norm=None, entry_id=None):
    store.record_reading(
        week="2026-W28", message_ts=message_ts, url=url, arxiv_id=arxiv_id,
        title_norm=title_norm, entry_id=entry_id,
        recorded_at="2026-07-10T00:00:00Z",
    )


def _epoch(iso: str) -> str:
    dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return str(dt.timestamp())


# a horizon comfortably before the July mentions the _new_entry helper seeds
_READ_SINCE = "2026-01-12T00:00:00Z"
_READ_TS = _epoch("2026-07-07T12:00:00Z")


def test_a_read_paper_is_excluded_by_entry_id(tmp_path):
    store = Store(tmp_path / "pw.db")
    eid = _new_entry(store, "read")
    _record_reading(store, message_ts=_READ_TS, entry_id=eid)
    # exclusion is opt-in: without the kwarg the paper is still selected
    assert [c["entry_id"] for c in _select(store)] == [eid]
    assert _select(store, exclude_read_since=_READ_SINCE) == []
    store.close()


def test_a_read_paper_is_excluded_by_arxiv_id(tmp_path):
    # The 2607.28607 case: the reading lands first (entry_id NULL), the paper
    # is only ingested later -- the arxiv_id recorded from the poll option must
    # still keep it out.
    store = Store(tmp_path / "pw.db")
    _record_reading(
        store, message_ts=_READ_TS,
        url="https://arxiv.org/abs/2607.28607", arxiv_id="2607.28607",
    )
    eid = store.insert_entry(
        title="Read Before Ingest", title_norm="read before ingest",
        first_seen_at="2026-07-10T00:00:00Z", arxiv_id="2607.28607",
    )
    store.add_mention(
        entry_id=eid, source="rss:Blog", fetched_at="2026-07-10T00:00:00Z",
        source_item_url="https://arxiv.org/abs/2607.28607",
    )
    store.set_enrichment(eid, tldr="t", why="w", tags=[], relevance=8, version=2)
    assert [c["entry_id"] for c in _select(store)] == [eid]
    assert _select(store, exclude_read_since=_READ_SINCE) == []
    store.close()


def test_a_read_paper_is_excluded_by_title_norm(tmp_path):
    store = Store(tmp_path / "pw.db")
    eid = _new_entry(store, "tn")  # title_norm "new paper tn"
    _record_reading(store, message_ts=_READ_TS, title_norm="new paper tn")
    assert [c["entry_id"] for c in _select(store)] == [eid]
    assert _select(store, exclude_read_since=_READ_SINCE) == []
    store.close()


def test_a_read_paper_is_excluded_by_url(tmp_path):
    # No id/arxiv/title tie -- only an entry_urls row matches the reading's
    # URL (canonicalized: the poll link's fragment must not defeat the match).
    store = Store(tmp_path / "pw.db")
    eid = _new_entry(store, "u")
    store.add_entry_url(eid, "https://example.org/paper")
    _record_reading(store, message_ts=_READ_TS, url="https://example.org/paper#abstract")
    assert [c["entry_id"] for c in _select(store)] == [eid]
    assert _select(store, exclude_read_since=_READ_SINCE) == []
    store.close()


def test_a_reading_older_than_the_horizon_does_not_exclude(tmp_path):
    store = Store(tmp_path / "pw.db")
    eid = _new_entry(store, "old-read")
    # one second before the cutoff: outside the horizon, paper is selected
    before = str(float(_epoch(_READ_SINCE)) - 1.0)
    _record_reading(store, message_ts=before, entry_id=eid)
    assert [c["entry_id"] for c in _select(store, exclude_read_since=_READ_SINCE)] == [eid]
    # exactly at the cutoff: inclusive (>=), paper is excluded
    _record_reading(
        store, message_ts=_epoch(_READ_SINCE), url="https://example.com/read2",
        entry_id=eid,
    )
    assert _select(store, exclude_read_since=_READ_SINCE) == []
    store.close()


def test_exclusion_is_display_only(tmp_path):
    # Dropping a read paper from the digest must not touch its feedback rows
    # or learned weights -- the group's votes keep steering scores.
    store = Store(tmp_path / "pw.db")
    eid = _new_entry(store, "fbk")
    store.record_feedback(entry_id=eid, week="2026-W28", picked=True,
                          group_rating=None, notes=None,
                          imported_at="2026-07-10T00:00:00Z")
    store.set_feedback_weight("source", "rss:Blog", 0.7)
    _record_reading(store, message_ts=_READ_TS, entry_id=eid)
    assert _select(store, exclude_read_since=_READ_SINCE) == []
    assert store.has_feedback(eid, "2026-W28")
    assert store.get_feedback_weight("source", "rss:Blog") == pytest.approx(0.7)
    store.close()


def test_run_pipeline_passes_exclude_read_weeks_through(tmp_path):
    # An entry read 2 weeks ago is dropped at a 26-week horizon but not at 1.
    store = Store(tmp_path / "pw.db")
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    eid = _new_entry(store, "wired", fetched_at="2026-07-13T00:00:00Z")
    _record_reading(store, message_ts=_epoch("2026-06-30T12:00:00Z"), entry_id=eid)

    def pipeline(**kw):
        return run_pipeline(
            store, sources=[], enricher=None, sender=CapturingSender(),
            weights=ScoringWeights(), top_n=10, since=None,
            candidate_window_days=7, resurface_window_days=30,
            new_window="7d", now=now, max_enrich=0, dry_run=True,
            out_dir=tmp_path / "out", **kw,
        )

    assert pipeline(exclude_read_weeks=1).chosen_ids == [eid]
    assert pipeline(exclude_read_weeks=26).chosen_ids == []
    assert pipeline().chosen_ids == [eid]  # default: feature off
    store.close()


def test_run_wires_feedback_refresh_on_a_due_tick(tmp_path, monkeypatch):
    from paper_watch import refresh, runtime
    from paper_watch.refresh import RefreshResult
    from paper_watch.runtime import RunResult

    # Keep the repo's .env (and its API keys) out of the run.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        f"""
db_path: {tmp_path / "pw.db"}
feedback_refresh:
  workspace: far
  groundtruth_path: {tmp_path / "gt.csv"}
  exclude_read_weeks: 9
"""
    )

    captured = {}

    def fake_pipeline(store, **kwargs):
        captured.update(kwargs)
        return RunResult()

    refreshed_with = []

    def fake_refresh(store, config, sender, *, now):
        refreshed_with.append(now)
        return RefreshResult(performed=True, ok=True)

    monkeypatch.setattr(runtime, "run_pipeline", fake_pipeline)
    monkeypatch.setattr(refresh, "run_feedback_refresh", fake_refresh)

    # No refresh watermark yet: the first tick refreshes, whatever the day.
    result = runtime.run(str(cfg_file))
    assert captured["exclude_read_weeks"] == 9
    assert len(refreshed_with) == 1
    assert result.refreshed

    # Watermark fresh (set to now by hand): nothing owed, no refresh.
    store = Store(tmp_path / "pw.db")
    store.set_last_feedback_refresh_at(
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    store.close()
    result = runtime.run(str(cfg_file))
    assert len(refreshed_with) == 1
    assert not result.refreshed


def test_run_gates_ingest_and_widens_from_last_poll(tmp_path, monkeypatch):
    from paper_watch import runtime
    from paper_watch.runtime import RunResult

    # Keep the repo's .env (and its API keys) out of the run.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(f"db_path: {tmp_path / 'pw.db'}\n")

    captured = {}

    def fake_pipeline(store, **kwargs):
        captured.update(kwargs)
        return RunResult()

    monkeypatch.setattr(runtime, "run_pipeline", fake_pipeline)

    iso = "%Y-%m-%dT%H:%M:%SZ"
    now = datetime.now(timezone.utc)
    old_poll = (now - timedelta(days=20)).strftime(iso)
    store = Store(tmp_path / "pw.db")
    # No delivery owed, so the gate is judged on poll age alone.
    store.set_last_sent_at(now.strftime(iso))
    # Ticked five minutes ago, but last actually POLLED 20 days ago: the fetch
    # window must widen from the poll — the gated ticks covered nothing.
    store.set_last_run_at((now - timedelta(minutes=5)).strftime(iso))
    store.set_last_polled_at(old_poll)
    store.close()

    runtime.run(str(cfg_file))
    assert captured["do_ingest"] is True  # 20 days is well past the 24h gate
    assert captured["since"] == old_poll  # widened past the 7d lookback

    store = Store(tmp_path / "pw.db")
    store.set_last_polled_at((now - timedelta(hours=1)).strftime(iso))
    store.close()
    result = runtime.run(str(cfg_file))
    assert captured["do_ingest"] is False  # fresh poll: gated
    assert not result.polled


# -- a fake week of 4-hourly ticks, end to end ------------------------------
@pytest.fixture
def tz(monkeypatch):
    """Pin the process timezone; delivery/poll points are local wall-clock."""
    import time as _time

    def _set(name):
        monkeypatch.setenv("TZ", name)
        _time.tzset()

    yield _set
    monkeypatch.undo()
    _time.tzset()


def test_fake_week_of_ticks_end_to_end(tmp_path, tz, monkeypatch):
    """Drive Mon–Sun of 4-hourly ticks through the pieces run() composes:
    poll gate -> pipeline -> feedback refresh. Polls land ~daily, Tue/Fri
    digests send, Thursday's refresh runs once, and a paper the group read
    before it was ever ingested stays out of Friday's digest."""
    tz("UTC")
    from paper_watch import refresh
    from paper_watch.feedback import VoteImportResult
    from paper_watch.runtime import effective_since
    from paper_watch.schedule import is_delivery_due, is_poll_due

    monkeypatch.setenv("SLACK_TOKEN_FAR", "xoxp-test")
    cfg = Config.model_validate(
        {
            "db_path": str(tmp_path / "pw.db"),
            "schedule": {"deliver_days": ["tue", "fri"], "deliver_at": "12:00"},
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
    fr = cfg.feedback_refresh
    store = Store(cfg.db_path)
    # Sunday baseline: nothing owed until Tuesday noon / Thursday noon.
    store.set_last_sent_at("2026-08-02T12:00:00Z")
    store.set_last_feedback_refresh_at("2026-08-02T12:00:00Z")

    source = QueueSource()
    sender = CapturingSender()
    # Papers appear in the feed at these moments (between polls).
    arrivals = {
        "2026-08-03T08:00": _arxiv_item("2408.10001", "Monday Paper", when="2026-08-03T07:00:00Z"),
        "2026-08-06T08:00": _arxiv_item("2408.10002", "Thursday Paper", when="2026-08-06T07:00:00Z"),
        # Read at Thursday's meeting, first seen by us only on Friday.
        "2026-08-07T08:00": _arxiv_item("2408.10003", "Read Before Ingest", when="2026-08-06T07:00:00Z"),
    }

    def refresh_importer(store_arg, *, path, config):
        # Thursday's poll winner enters the readings ledger; the paper is not
        # in the DB yet, so the reading lands with entry_id NULL.
        row = store_arg.get_entry_by_arxiv_id("2408.10003")
        store_arg.record_reading(
            week="2026-W32", message_ts=_epoch("2026-08-06T11:00:00Z"),
            url="https://arxiv.org/abs/2408.10003", arxiv_id="2408.10003",
            title_norm=None, entry_id=row["id"] if row else None,
            recorded_at="2026-08-06T12:00:00Z",
        )
        return VoteImportResult(imported=1, weeks=["2026-W32"], readings_recorded=1)

    polls, refreshes = [], []
    now = datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc)  # Monday 00:00
    end = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)
    while now < end:
        key = now.strftime("%Y-%m-%dT%H:%M")
        if key in arrivals:
            source.pending.append(arrivals[key])
        scheduled = is_delivery_due(
            now, store.get_last_sent_at(),
            days=cfg.schedule.weekdays, at=cfg.schedule.at_time,
        )
        do_ingest = is_poll_due(
            now, store.get_last_polled_at(), delivery_due=scheduled,
            days=cfg.schedule.weekdays, at=cfg.schedule.at_time,
        )
        if do_ingest:
            polls.append(now.strftime("%a %H:%M"))
        run_pipeline(
            store,
            sources=[source],
            enricher=FakeEnricher(),
            sender=sender,
            weights=ScoringWeights(),
            top_n=10,
            since=effective_since(store.get_last_polled_at(), None, "7d", now),
            candidate_window_days=7,
            resurface_window_days=21,
            exclude_read_weeks=fr.exclude_read_weeks,
            now=now,
            max_enrich=50,
            dry_run=False,
            deliver=scheduled,
            do_ingest=do_ingest,
            out_dir=tmp_path / "out",
        )
        if refresh.is_refresh_due(
            now, store.get_last_feedback_refresh_at(), days=fr.weekdays, at=fr.at_time
        ):
            refreshes.append(now.strftime("%a %H:%M"))
            refresh.run_feedback_refresh(
                store, cfg, sender, now=now,
                export=lambda token, channel_ids, *, oldest, path, append=False: 0,
                importer=refresh_importer,
            )
        now += timedelta(hours=4)

    # Polls happen roughly daily, not on all 42 ticks — with the delivery-due
    # Tuesday noon tick polling early because its midnight poll was stale.
    assert polls == [
        "Mon 00:00", "Tue 00:00", "Tue 12:00", "Wed 12:00",
        "Thu 12:00", "Fri 12:00", "Sat 12:00", "Sun 12:00",
    ]
    assert source.fetches == len(polls)
    assert refreshes == ["Thu 12:00"]

    digests = [(s, h) for s, h in sender.sent if s.startswith("paper-watch digest")]
    assert len(digests) == 2  # Tuesday and Friday
    assert "Monday Paper" in digests[0][1]
    assert "Thursday Paper" in digests[1][1]
    # Fresh to the pipeline, but the group already read it: excluded.
    assert "Read Before Ingest" not in digests[1][1]
    # The refresh's own notice email went out too.
    assert any(s.startswith("paper-watch feedback refresh") for s, _ in sender.sent)


# -- historical replay ------------------------------------------------------
def _replayable_store(tmp_path):
    """A: shown 08-02; B: new before the 08-06 digest, shown in it; C: after."""
    from paper_watch.enrich import ENRICH_VERSION

    store = Store(tmp_path / "pw.db")
    ids = {}
    for key, title, mentioned in (
        ("A", "Old Paper", "2026-08-01T00:00:00Z"),
        ("B", "Fresh Paper", "2026-08-05T00:00:00Z"),
        ("C", "Future Paper", "2026-08-07T00:00:00Z"),
    ):
        eid = store.insert_entry(
            title=title, title_norm=title.lower(), first_seen_at=mentioned
        )
        store.add_mention(
            entry_id=eid, source="rss:AF", fetched_at=mentioned,
            source_item_url=f"https://ex.com/{key}",
        )
        store.set_enrichment(
            eid, tldr="t", why="w", tags=[], relevance=6, version=ENRICH_VERSION
        )
        ids[key] = eid
    store.record_shown(
        entry_id=ids["A"], digest_at="2026-08-02T12:00:00Z", rank=1, score=1.0, resurfaced=False
    )
    store.record_shown(
        entry_id=ids["B"], digest_at="2026-08-06T12:00:00Z", rank=1, score=1.0, resurfaced=False
    )
    return store, ids


def test_replay_reconstructs_a_past_digest_from_current_data(tmp_path):
    from paper_watch.store import AsOfStoreView

    store, ids = _replayable_store(tmp_path)
    at = datetime(2026, 8, 6, 12, 0, 0, tzinfo=timezone.utc)
    view = AsOfStoreView(store, "2026-08-06T12:00:00Z")

    result = run_pipeline(
        view, sources=[], enricher=None, sender=None,
        weights=ScoringWeights(), top_n=20, since=None,
        candidate_window_days=7, resurface_window_days=21, new_window="4d",
        now=at, max_enrich=0, dry_run=True, deliver=True, out_dir=tmp_path / "out",
    )

    # B leads as new (its own 08-06 digest doesn't suppress it); A was shown
    # on 08-02 and isn't surging; C hadn't been fetched yet.
    assert result.chosen_ids == [ids["B"]]
    assert result.digest_path is not None
    assert "Fresh Paper" in result.digest_path.read_text()
    store.close()


def test_replay_entrypoint_reads_config_and_writes_the_digest(tmp_path, monkeypatch):
    from paper_watch import runtime

    store, ids = _replayable_store(tmp_path)
    store.close()
    (tmp_path / "config.yaml").write_text(f"db_path: {tmp_path / 'pw.db'}\n")
    monkeypatch.chdir(tmp_path)

    result = runtime.replay("config.yaml", at="2026-08-06T12:00:00Z")

    assert result.chosen_ids == [ids["B"]]
    assert result.digest_path is not None and result.digest_path.exists()
    assert result.sent is False


def test_replay_accepts_a_bare_date_as_end_of_day(tmp_path, monkeypatch):
    from paper_watch import runtime

    store, ids = _replayable_store(tmp_path)
    store.close()
    (tmp_path / "config.yaml").write_text(f"db_path: {tmp_path / 'pw.db'}\n")
    monkeypatch.chdir(tmp_path)

    # end of 08-05: B is new (mentioned that morning), its 08-06 digest hasn't
    # happened, C hasn't been fetched
    result = runtime.replay("config.yaml", at="2026-08-05")
    assert result.chosen_ids == [ids["B"]]


def test_trusted_does_not_rescue_a_non_artifact():
    # Goodfire's trusted research page grew /legal/tos footer links (2026-08-06):
    # trusted page, real diff, but the enricher rightly scored them 0 = "not a
    # research artifact". Trusted skips the fit bar, not the artifact bar.
    from paper_watch.runtime import _passes_gate

    def row(relevance):
        return {"relevance": relevance, "safety_relevant": None}

    assert _passes_gate(row(0), {"page:Goodfire Research"}, trusted=True) is False
    # a weak-fit but real artifact on a trusted page still bypasses the fit bar
    assert _passes_gate(row(1), {"page:Goodfire Research"}, trusted=True) is True
    # not yet enriched: keep trusting the page (no-LLM setups have no scores)
    assert _passes_gate(row(None), {"page:Goodfire Research"}, trusted=True) is True
    # the arXiv author whitelist stays unconditional (tracked authors' papers
    # are wanted even when the LLM shrugs)
    assert _passes_gate(row(0), {"arxiv"}, trusted=False) is True


# -- AF/LW mirror presentation ----------------------------------------------
def _af_item(url):
    return RawItem(source="rss:Alignment Forum", url=url, title="Why do models task game?",
                   authors=[], abstract="abs", published_at="2026-08-06T00:00:00Z")


def test_af_feed_dual_hosts_collapse_and_flag_as_af(tmp_path):
    # The AF feed emits the same post under both hosts; it must land as ONE
    # entry whose source chip says rss:Alignment Forum.
    from paper_watch.runtime import _entry_sources

    store = Store(tmp_path / "pw.db")
    ingest(store, [ListSource("rss:Alignment Forum", [
        _af_item("https://www.alignmentforum.org/posts/HACa4/why-do-models-task-game"),
        _af_item("https://www.lesswrong.com/posts/HACa4/why-do-models-task-game"),
    ])], None, "2026-08-06T12:00:00Z")

    ids = [r["id"] for r in store.conn.execute("SELECT id FROM entries")]
    assert len(ids) == 1
    assert _entry_sources(store, ids[0]) == {"rss:Alignment Forum"}
    store.close()


def test_af_post_displays_the_af_link(tmp_path):
    # Identity canonicalizes to lesswrong.com, but AF is the curated venue:
    # when the AF feed emitted the post, display its alignmentforum.org URL.
    from paper_watch.runtime import _display_links

    store = Store(tmp_path / "pw.db")
    ingest(store, [ListSource("rss:Alignment Forum", [
        _af_item("https://www.alignmentforum.org/posts/HACa4/why-do-models-task-game"),
    ])], None, "2026-08-06T12:00:00Z")
    (eid,) = [r["id"] for r in store.conn.execute("SELECT id FROM entries")]

    links = _display_links(store, eid, json.loads(store.get_entry(eid)["links_json"]))
    assert links["abstract"] == (
        "https://www.alignmentforum.org/posts/HACa4/why-do-models-task-game"
    )
    # ...while identity still answers to the canonical LW spelling
    assert store.get_entry_by_source_url(
        "https://www.lesswrong.com/posts/HACa4/why-do-models-task-game"
    ) is not None
    store.close()


def test_lw_only_post_keeps_the_lw_link(tmp_path):
    # A post that only ever appeared on LW has no AF page to point at.
    from paper_watch.runtime import _display_links

    store = Store(tmp_path / "pw.db")
    item = RawItem(source="graphql:LessWrong AI",
                   url="https://www.lesswrong.com/posts/xyz9/an-lw-only-post",
                   title="An LW-only post", authors=[], abstract="abs",
                   published_at="2026-08-06T00:00:00Z")
    ingest(store, [ListSource("graphql:LessWrong AI", [item])], None, "2026-08-06T12:00:00Z")
    (eid,) = [r["id"] for r in store.conn.execute("SELECT id FROM entries")]

    links = _display_links(store, eid, json.loads(store.get_entry(eid)["links_json"]))
    assert links["abstract"] == "https://www.lesswrong.com/posts/xyz9/an-lw-only-post"
    store.close()
