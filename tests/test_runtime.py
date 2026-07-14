import json
from datetime import datetime, timezone

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
    build_sources,
    effective_since,
    ingest,
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


class FakeEnricher:
    def __init__(self, relevant=True):
        self.relevant = relevant

    def enrich(self, *, title, abstract, source, mentions):
        return EnrichmentResult(
            tldr=f"tldr:{title}",
            why="why",
            tags=["interp"],
            relevance=3 if self.relevant else 0,
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


def test_effective_since_uses_lookback_when_no_prior_run(tmp_path):
    store = Store(tmp_path / "pw.db")
    # No last run recorded -> plain lookback window.
    assert effective_since(store, None, "7d", _NOW) == "2026-06-12T09:00:00Z"
    store.close()


def test_effective_since_widens_to_cover_gap_when_off(tmp_path):
    store = Store(tmp_path / "pw.db")
    # Last real run was 20 days ago -> further back than the 7d lookback, so the
    # window widens to the last run to cover the gap left by being powered off.
    store.set_last_run_at("2026-05-30T09:00:00Z")
    assert effective_since(store, None, "7d", _NOW) == "2026-05-30T09:00:00Z"
    store.close()


def test_effective_since_keeps_lookback_when_recent_run(tmp_path):
    store = Store(tmp_path / "pw.db")
    # A run 12h ago is more recent than the 7d lookback; don't shrink the window.
    store.set_last_run_at("2026-06-18T21:00:00Z")
    assert effective_since(store, None, "7d", _NOW) == "2026-06-12T09:00:00Z"
    store.close()


def test_effective_since_explicit_override_ignores_last_run(tmp_path):
    store = Store(tmp_path / "pw.db")
    store.set_last_run_at("2026-05-30T09:00:00Z")
    # An explicit --since wins over gap coverage.
    assert effective_since(store, "2026-06-15T00:00:00Z", "7d", _NOW) == "2026-06-15T00:00:00Z"
    store.close()


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
    return select_digest(
        store,
        ScoringWeights(),
        top_n=10,
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
        entry_id, tldr="t", why="w", tags=[], relevance=3, version=2
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
                    channels=[SlackChannel(id="C1", name="papers")],
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
    assert row["relevance"] == 3  # medium-high prior, survives (won't be re-enriched down)
    assert row["title"] == "A Structured Study of Oversight"  # link metadata promoted
    # and it now passes the gate on relevance alone
    from paper_watch.runtime import _passes_gate

    assert _passes_gate(row, {"rss:Import AI"}, trusted=False)
    store.close()
