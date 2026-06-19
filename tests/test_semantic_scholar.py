from paper_watch.sources.semantic_scholar import (
    SemanticScholar,
    parse_citation_count,
    paper_url,
)


def test_paper_url_uses_arxiv_prefix():
    url = paper_url("2406.01234")
    assert "paper/arXiv:2406.01234" in url
    assert "citationCount" in url


def test_parse_citation_count(fixture_text):
    assert parse_citation_count(fixture_text("s2_paper.json")) == 42


def test_client_returns_count(fixture_text):
    s2 = SemanticScholar(fetch=lambda url: fixture_text("s2_paper.json"))
    assert s2.citation_count("2406.01234") == 42


def test_client_swallows_errors():
    def boom(url: str) -> str:
        raise RuntimeError("network down")

    s2 = SemanticScholar(fetch=boom)
    assert s2.citation_count("2406.01234") is None
