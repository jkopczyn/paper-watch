from paper_watch.config import ScoringWeights
from paper_watch.enrich import EnrichmentResult
from paper_watch.models import RawItem
from paper_watch.runtime import ingest, run_pipeline
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
