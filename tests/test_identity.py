from paper_watch.identity import (
    extract_arxiv_id,
    extract_doi,
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
