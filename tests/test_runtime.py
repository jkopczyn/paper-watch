from paper_watch.config import (
    Config,
    ScoringWeights,
    SlackChannel,
    SlackConfig,
    SlackWorkspace,
)
from paper_watch.enrich import EnrichmentResult
from paper_watch.models import RawItem
from paper_watch.runtime import build_sources, ingest, run_pipeline
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

    def enrich(self, *, title, abstract, source):
        return EnrichmentResult(
            tldr=f"tldr:{title}",
            why="why",
            tags=["interp"],
            safety_relevant=self.relevant,
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
