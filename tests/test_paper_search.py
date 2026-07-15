import json

from paper_watch.sources.paper_search import (
    PaperSearchResolver,
    parse_crossref,
    parse_s2_search,
)

_TITLE = "Impossibility Results for Fair Machine Learning"


def _s2(**item) -> str:
    return json.dumps({"total": 1, "data": [item]})


def test_s2_prefers_arxiv_id_for_the_url():
    text = _s2(
        title=_TITLE,
        externalIds={"ArXiv": "1810.08810", "DOI": "10.1/x"},
        openAccessPdf={"url": "https://cdn/paper.pdf"},
        url="https://semanticscholar.org/paper/abc",
        publicationDate="2018-10-19",
        authors=[{"name": "A. Chouldechova"}],
        abstract="We show...",
    )
    res = parse_s2_search(text, _TITLE)
    assert res["url"] == "https://arxiv.org/abs/1810.08810"
    assert res["arxiv_id"] == "1810.08810"
    assert res["doi"] == "10.1/x"
    assert res["published_at"] == "2018-10-19T00:00:00Z"
    assert res["authors"] == ["A. Chouldechova"]


def test_s2_falls_back_to_open_access_pdf_then_url():
    pdf = _s2(title=_TITLE, externalIds={}, openAccessPdf={"url": "https://cdn/p.pdf"}, url="https://s2/x")
    assert parse_s2_search(pdf, _TITLE)["url"] == "https://cdn/p.pdf"
    page = _s2(title=_TITLE, externalIds={}, openAccessPdf=None, url="https://s2/x")
    assert parse_s2_search(page, _TITLE)["url"] == "https://s2/x"


def test_s2_year_only_publication_date():
    text = _s2(title=_TITLE, externalIds={"ArXiv": "1810.1"}, year=2018, publicationDate=None)
    assert parse_s2_search(text, _TITLE)["published_at"] == "2018-01-01T00:00:00Z"


def test_s2_rejects_a_title_mismatch():
    text = _s2(title="A Totally Different Paper About Cooking", externalIds={"ArXiv": "1"}, url="x")
    assert parse_s2_search(text, _TITLE) is None


def test_s2_empty_results_is_none():
    assert parse_s2_search(json.dumps({"total": 0, "data": []}), _TITLE) is None


def test_crossref_builds_doi_url_and_date():
    text = json.dumps({
        "message": {"items": [{
            "title": [_TITLE],
            "DOI": "10.1145/abc",
            "issued": {"date-parts": [[2018, 10]]},
            "author": [{"given": "Alexandra", "family": "Chouldechova"}],
        }]}
    })
    res = parse_crossref(text, _TITLE)
    assert res["url"] == "https://doi.org/10.1145/abc"
    assert res["doi"] == "10.1145/abc"
    assert res["published_at"] == "2018-10-01T00:00:00Z"
    assert res["authors"] == ["Alexandra Chouldechova"]


def test_crossref_rejects_a_title_mismatch():
    text = json.dumps({"message": {"items": [{"title": ["Unrelated"], "DOI": "10/x"}]}})
    assert parse_crossref(text, _TITLE) is None


def test_resolver_tries_s2_then_crossref():
    calls = []

    def fetch(url):
        calls.append(url)
        if "semanticscholar" in url:
            return json.dumps({"data": []})  # S2 miss
        return json.dumps({
            "message": {"items": [{"title": [_TITLE], "DOI": "10/y",
                                    "issued": {"date-parts": [[2018]]}}]}
        })

    res = PaperSearchResolver(fetch=fetch).search(_TITLE)
    assert res["url"] == "https://doi.org/10/y"
    assert any("semanticscholar" in u for u in calls)
    assert any("crossref" in u for u in calls)


def test_resolver_network_error_returns_none():
    def boom(url):
        raise RuntimeError("network down")

    assert PaperSearchResolver(fetch=boom).search(_TITLE) is None


def test_resolver_blank_title_returns_none_without_fetching():
    def boom(url):
        raise AssertionError("should not fetch")

    assert PaperSearchResolver(fetch=boom).search("   ") is None
