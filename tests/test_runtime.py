import json

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
    ingest,
    resolve_paper_metadata,
    run_pipeline,
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
