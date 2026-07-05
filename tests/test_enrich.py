from paper_watch.enrich import (
    ENRICH_VERSION,
    ClaudeEnricher,
    EnrichmentResult,
    build_system_prompt,
    enrich_unenriched,
    load_profile,
    load_tag_vocabulary,
    mention_snippets,
)
from paper_watch.store import Store


class FakeEnricher:
    def __init__(self):
        self.calls = []

    def enrich(self, *, title, abstract, source, mentions):
        self.calls.append((title, mentions))
        return EnrichmentResult(
            tldr=f"TLDR for {title}",
            why="Relevant to oversight.",
            tags=["evals", "oversight"],
            relevance=3,
        )


def _seed_entry(store: Store, title: str) -> int:
    return store.insert_entry(
        title=title,
        title_norm=title.lower(),
        first_seen_at="2026-06-19T00:00:00Z",
        abstract="Some abstract.",
    )


def _enrich(store, eid, **kw):
    args = dict(tldr="t", why="w", tags=["interp"], relevance=3, version=ENRICH_VERSION)
    args.update(kw)
    store.set_enrichment(eid, **args)


def test_set_and_get_enrichment_roundtrip(tmp_path):
    store = Store(tmp_path / "pw.db")
    eid = _seed_entry(store, "Paper A")
    assert [r["id"] for r in store.get_unenriched(10, version=ENRICH_VERSION)] == [eid]

    _enrich(store, eid)
    row = store.get_entry(eid)
    assert row["tldr"] == "t"
    assert row["relevance"] == 3
    assert row["enrich_version"] == ENRICH_VERSION
    # now enriched -> no longer returned by get_unenriched
    assert store.get_unenriched(10, version=ENRICH_VERSION) == []
    store.close()


def test_old_version_enrichment_is_redone(tmp_path):
    store = Store(tmp_path / "pw.db")
    eid = _seed_entry(store, "Paper A")
    _enrich(store, eid, version=ENRICH_VERSION - 1)
    assert [r["id"] for r in store.get_unenriched(10, version=ENRICH_VERSION)] == [eid]
    store.close()


def test_enrich_unenriched_only_touches_new_entries(tmp_path):
    store = Store(tmp_path / "pw.db")
    a = _seed_entry(store, "Paper A")
    b = _seed_entry(store, "Paper B")
    _enrich(store, a, tldr="done")

    enricher = FakeEnricher()
    n = enrich_unenriched(store, enricher, limit=50)

    assert n == 1  # only Paper B was unenriched
    assert [c[0] for c in enricher.calls] == ["Paper B"]
    assert store.get_entry(b)["tldr"] == "TLDR for Paper B"
    assert store.get_entry(b)["enrich_version"] == ENRICH_VERSION
    store.close()


def test_enrich_unenriched_respects_limit(tmp_path):
    store = Store(tmp_path / "pw.db")
    for i in range(5):
        _seed_entry(store, f"Paper {i}")

    enricher = FakeEnricher()
    n = enrich_unenriched(store, enricher, limit=2)

    assert n == 2
    assert len(enricher.calls) == 2
    assert len(store.get_unenriched(10, version=ENRICH_VERSION)) == 3
    store.close()


def test_enrich_passes_mention_provenance(tmp_path):
    store = Store(tmp_path / "pw.db")
    eid = _seed_entry(store, "Paper A")
    store.add_mention(
        entry_id=eid,
        source="slack:far:papers",
        source_item_url="slack://far/C1/1",
        mention_text="strong new result, worth a read",
        fetched_at="2026-06-19T01:00:00Z",
    )

    enricher = FakeEnricher()
    enrich_unenriched(store, enricher, limit=10)
    (_, mentions), = enricher.calls
    assert mentions == ["slack:far:papers: strong new result, worth a read"]
    store.close()


def test_mention_snippets_caps_and_cleans(tmp_path):
    store = Store(tmp_path / "pw.db")
    eid = _seed_entry(store, "Paper A")
    for i in range(5):
        store.add_mention(
            entry_id=eid,
            source=f"twitter:u{i}",
            source_item_url=f"https://twitter.com/u{i}/status/{i}",
            mention_text="x" * 1000,
            fetched_at="2026-06-19T01:00:00Z",
        )
    snippets = mention_snippets(store, eid)
    assert len(snippets) == 3
    assert all(len(s) < 320 for s in snippets)
    store.close()


# -- profile / vocabulary loading -------------------------------------------
def test_load_profile_and_vocabulary(tmp_path):
    (tmp_path / "profile.md").write_text("Cares about interp.\n")
    (tmp_path / "tags.yaml").write_text("tags:\n  interp: internals\n  evals: evals\n")
    assert load_profile(tmp_path / "profile.md") == "Cares about interp."
    assert load_tag_vocabulary(tmp_path / "tags.yaml") == {
        "interp": "internals",
        "evals": "evals",
    }


def test_load_profile_and_vocabulary_missing_files(tmp_path):
    assert "no reader profile" in load_profile(tmp_path / "nope.md")
    assert load_tag_vocabulary(tmp_path / "nope.yaml") == {}


def test_build_system_prompt_includes_profile_rubric_and_tags():
    prompt = build_system_prompt("PROFILE-SENTINEL", {"interp": "internals"})
    assert "PROFILE-SENTINEL" in prompt
    assert "relevance` 0-4" in prompt
    assert "- interp: internals" in prompt


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


def _claude(obj, **kw):
    return ClaudeEnricher(model="claude-haiku-4-5", client=_FakeClient(obj), **kw)


def test_claude_enricher_maps_parsed_output():
    from paper_watch.enrich import _LLMEnrichment

    obj = _LLMEnrichment(
        tldr="A short summary.",
        why="Bears on scalable oversight.",
        tags=["oversight", "evals"],
        relevance=3,
    )
    enricher = _claude(obj, vocabulary={"oversight": "d", "evals": "d"})

    result = enricher.enrich(
        title="Scalable Oversight",
        abstract="We study...",
        source="arxiv",
        mentions=["twitter:janleike: great paper"],
    )
    assert isinstance(result, EnrichmentResult)
    assert result.tldr == "A short summary."
    assert result.tags == ["oversight", "evals"]
    assert result.relevance == 3
    # model id and provenance are threaded through to the SDK call
    assert enricher._client.messages.kwargs["model"] == "claude-haiku-4-5"
    assert "janleike" in enricher._client.messages.kwargs["messages"][0]["content"]


def test_claude_enricher_drops_unknown_tags_and_clamps_relevance():
    from paper_watch.enrich import _LLMEnrichment

    obj = _LLMEnrichment(tldr="t", why="w", tags=["interp", "made-up"], relevance=9)
    enricher = _claude(obj, vocabulary={"interp": "d"})
    result = enricher.enrich(title="T", abstract=None, source="arxiv", mentions=[])
    assert result.tags == ["interp"]
    assert result.relevance == 4
