from paper_watch.enrich import (
    ClaudeEnricher,
    EnrichmentResult,
    enrich_unenriched,
)
from paper_watch.store import Store


class FakeEnricher:
    def __init__(self):
        self.calls = []

    def enrich(self, *, title, abstract, source):
        self.calls.append(title)
        return EnrichmentResult(
            tldr=f"TLDR for {title}",
            why="Relevant to oversight.",
            tags=["evals", "oversight"],
            safety_relevant=True,
        )


def _seed_entry(store: Store, title: str) -> int:
    return store.insert_entry(
        title=title,
        title_norm=title.lower(),
        first_seen_at="2026-06-19T00:00:00Z",
        abstract="Some abstract.",
    )


def test_set_and_get_enrichment_roundtrip(tmp_path):
    store = Store(tmp_path / "pw.db")
    eid = _seed_entry(store, "Paper A")
    assert [r["id"] for r in store.get_unenriched(10)] == [eid]

    store.set_enrichment(
        eid, tldr="t", why="w", tags=["interp"], safety_relevant=True
    )
    row = store.get_entry(eid)
    assert row["tldr"] == "t"
    assert row["safety_relevant"] == 1
    # now enriched -> no longer returned by get_unenriched
    assert store.get_unenriched(10) == []
    store.close()


def test_enrich_unenriched_only_touches_new_entries(tmp_path):
    store = Store(tmp_path / "pw.db")
    a = _seed_entry(store, "Paper A")
    b = _seed_entry(store, "Paper B")
    store.set_enrichment(a, tldr="done", why="w", tags=[], safety_relevant=True)

    enricher = FakeEnricher()
    n = enrich_unenriched(store, enricher, limit=50)

    assert n == 1  # only Paper B was unenriched
    assert enricher.calls == ["Paper B"]
    assert store.get_entry(b)["tldr"] == "TLDR for Paper B"
    store.close()


def test_enrich_unenriched_respects_limit(tmp_path):
    store = Store(tmp_path / "pw.db")
    for i in range(5):
        _seed_entry(store, f"Paper {i}")

    enricher = FakeEnricher()
    n = enrich_unenriched(store, enricher, limit=2)

    assert n == 2
    assert len(enricher.calls) == 2
    assert len(store.get_unenriched(10)) == 3  # three still pending
    store.close()


# -- ClaudeEnricher maps structured output without touching the network ----
class _FakeParsed:
    def __init__(self, obj):
        self.parsed_output = obj


class _FakeMessages:
    def __init__(self, obj):
        self._obj = obj
        self.kwargs = None

    def parse(self, **kwargs):
        self.kwargs = kwargs
        return _FakeParsed(self._obj)


class _FakeClient:
    def __init__(self, obj):
        self.messages = _FakeMessages(obj)


def test_claude_enricher_maps_parsed_output():
    from paper_watch.enrich import _LLMEnrichment

    obj = _LLMEnrichment(
        tldr="A short summary.",
        why="Bears on scalable oversight.",
        tags=["oversight", "evals"],
        safety_relevant=True,
    )
    client = _FakeClient(obj)
    enricher = ClaudeEnricher(model="claude-haiku-4-5", client=client)

    result = enricher.enrich(
        title="Scalable Oversight", abstract="We study...", source="arxiv"
    )
    assert isinstance(result, EnrichmentResult)
    assert result.tldr == "A short summary."
    assert result.tags == ["oversight", "evals"]
    assert result.safety_relevant is True
    # model id is threaded through to the SDK call
    assert client.messages.kwargs["model"] == "claude-haiku-4-5"
