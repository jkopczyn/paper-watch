from paper_watch.identity import (
    canonicalize_url,
    extract_arxiv_id,
    extract_doi,
    is_distinctive_title,
    normalize_title,
    resolve_or_create,
)
from paper_watch.store import Store


# -- arXiv ID extraction ---------------------------------------------------
def test_extract_bare_arxiv_id():
    assert extract_arxiv_id("2406.01234") == "2406.01234"


def test_extract_arxiv_id_strips_version():
    assert extract_arxiv_id("2406.01234v3") == "2406.01234"


def test_extract_arxiv_id_from_abs_url():
    assert extract_arxiv_id("https://arxiv.org/abs/2406.01234") == "2406.01234"


def test_extract_arxiv_id_from_pdf_url_with_version():
    assert extract_arxiv_id("http://arxiv.org/pdf/2406.01234v2") == "2406.01234"


def test_extract_arxiv_id_from_surrounding_text():
    text = "Great new paper! https://arxiv.org/abs/2406.01234 check it out"
    assert extract_arxiv_id(text) == "2406.01234"


def test_extract_old_style_arxiv_id():
    assert extract_arxiv_id("https://arxiv.org/abs/hep-th/9901001") == "hep-th/9901001"


def test_extract_arxiv_id_absent():
    assert extract_arxiv_id("no identifiers here") is None
    assert extract_arxiv_id(None) is None


def test_old_style_ignores_ordinary_url_paths():
    # news-site URL segments must not parse as old-style arXiv ids
    assert extract_arxiv_id("https://example.com/technology/5934266") is None
    assert extract_arxiv_id("https://jstor.org/stable/2946648") is None
    assert extract_arxiv_id("https://site.com/articles/8241253") is None


# -- URL canonicalization ----------------------------------------------------
def test_canonicalize_nitter_hosts_to_twitter():
    assert (
        canonicalize_url("https://nitter.net/FreedmanRach/status/207169#m")
        == "https://twitter.com/FreedmanRach/status/207169"
    )
    assert (
        canonicalize_url("http://localhost/FreedmanRach/status/207169#m")
        == "https://twitter.com/FreedmanRach/status/207169"
    )


def test_canonicalize_x_com_share_link():
    assert (
        canonicalize_url("https://x.com/FreedmanRach/status/207169?s=20")
        == "https://twitter.com/FreedmanRach/status/207169"
    )


def test_canonicalize_non_tweet_url_keeps_query_drops_fragment():
    assert (
        canonicalize_url("https://pluralistic-alignment.github.io/page?a=1#schedule")
        == "https://pluralistic-alignment.github.io/page?a=1"
    )


def test_canonicalize_passthrough():
    assert canonicalize_url(None) is None
    assert canonicalize_url("slack://far/C001/1719.9") == "slack://far/C001/1719.9"
    # a status-shaped path on an unrelated host is left alone
    assert (
        canonicalize_url("https://myblog.example/foo/status/123")
        == "https://myblog.example/foo/status/123"
    )


# -- DOI extraction --------------------------------------------------------
def test_extract_doi_plain():
    assert extract_doi("10.1145/1234567.8901234") == "10.1145/1234567.8901234"


def test_extract_doi_with_prefix_and_trailing_punct():
    assert extract_doi("see doi:10.1038/s41586-020-2649-2.") == "10.1038/s41586-020-2649-2"


def test_extract_doi_absent():
    assert extract_doi("nothing") is None


# -- title normalization ---------------------------------------------------
def test_normalize_title_lowercases_and_strips_punct():
    assert normalize_title("Scalable Oversight: A Survey!") == "scalable oversight a survey"


def test_normalize_title_collapses_whitespace():
    assert normalize_title("  Deep   Learning\nMatters ") == "deep learning matters"


def test_normalize_title_strips_site_suffix():
    bare = "Consistency Training while Mitigating Obfuscation via Rate Matching"
    assert normalize_title(f"{bare} — LessWrong") == normalize_title(bare)
    assert normalize_title(f"{bare} | OpenAI") == normalize_title(bare)


def test_normalize_title_keeps_dash_in_short_titles():
    # too little would remain — treat the dash as part of the title
    assert normalize_title("Attention — Is All You Need") == "attention is all you need"


# -- dedup / resolution ----------------------------------------------------
def _fields(**kw):
    base = {
        "title": "Scalable Oversight",
        "title_norm": "scalable oversight",
        "arxiv_id": None,
        "doi": None,
        "authors": [],
        "abstract": None,
        "links": {},
        "first_seen_at": "2026-06-19T00:00:00Z",
    }
    base.update(kw)
    return base


def test_resolve_creates_then_reuses_by_arxiv_id():
    store = Store(":memory:")
    eid1, created1 = resolve_or_create(store, _fields(arxiv_id="2406.01234"))
    assert created1 is True

    eid2, created2 = resolve_or_create(
        store, _fields(arxiv_id="2406.01234", title="Scalable Oversight (v2)")
    )
    assert created2 is False
    assert eid2 == eid1
    store.close()


def test_resolve_matches_by_title_when_no_ids():
    store = Store(":memory:")
    eid1, _ = resolve_or_create(store, _fields())
    eid2, created = resolve_or_create(
        store, _fields(arxiv_id=None, title="Scalable  Oversight")
    )
    assert created is False
    assert eid2 == eid1
    store.close()


def test_resolve_distinct_papers_are_separate():
    store = Store(":memory:")
    eid1, _ = resolve_or_create(store, _fields(arxiv_id="2406.01234"))
    eid2, created = resolve_or_create(
        store,
        _fields(arxiv_id="2407.99999", title="Other Paper", title_norm="other paper"),
    )
    assert created is True
    assert eid2 != eid1
    store.close()


def test_generic_titles_are_not_distinctive_enough_to_identify_a_paper():
    # Two different Anthropic system-card PDFs both extract the title "System
    # Card". Identity must not be inferred from a title that generic, or the two
    # documents get merged into one.
    assert not is_distinctive_title(normalize_title("System Card"))
    assert not is_distinctive_title(normalize_title("Technical Report"))
    assert not is_distinctive_title(normalize_title(""))
    # A real paper title carries enough signal.
    assert is_distinctive_title(normalize_title("Modular Pretraining Enables Access Control"))
    assert is_distinctive_title(normalize_title("Attention Is All You Need"))


def test_springer_running_head_is_not_a_title():
    # "Vol.:(0123456789)" is boilerplate on every Springer PDF, so it must never
    # act as identity -- it would fuse every Springer paper into one entry.
    assert not is_distinctive_title(normalize_title("Vol.:(0123456789)"))


def test_doi_from_a_url_drops_the_file_extension():
    # A DOI lifted out of a publisher URL swallowed the path's extension, so the
    # stored id was "10.1007/s11023-020-09539-2.pdf" -- which matches nothing.
    assert (
        extract_doi("https://link.springer.com/content/pdf/10.1007/s11023-020-09539-2.pdf")
        == "10.1007/s11023-020-09539-2"
    )
    assert extract_doi("https://x.org/10.1145/1234567.8901234.html") == "10.1145/1234567.8901234"
    # a dot inside the DOI itself is not an extension and must survive
    assert extract_doi("10.1145/1234567.8901234") == "10.1145/1234567.8901234"


def test_two_links_sharing_generic_anchor_text_stay_separate(tmp_path):
    # An AF post links several PDFs, each anchored "paper". Both entries were
    # born titled "paper", and title dedup fused two unrelated papers into one.
    from paper_watch.models import RawItem
    from paper_watch.normalize import to_entry_fields

    store = Store(":memory:")
    ids = set()
    for url in (
        "https://ae.studio/research/modular-pretraining.pdf",
        "https://link.springer.com/content/pdf/10.1007/s11023-020-09539-2.pdf",
    ):
        fields = to_entry_fields(RawItem(source="rss:AF", text="paper", url=url))
        fields["first_seen_at"] = "2026-07-13T00:00:00Z"
        entry_id, _ = resolve_or_create(store, fields)
        ids.add(entry_id)
    assert len(ids) == 2, "two unrelated papers were fused by their anchor text"
    store.close()
