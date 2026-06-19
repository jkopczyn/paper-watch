from paper_watch.sources.arxiv import ArxivSource, author_query_url, parse_arxiv_atom


def test_parse_arxiv_atom(fixture_text):
    items = parse_arxiv_atom(fixture_text("arxiv_response.xml"))
    assert len(items) == 2

    first = items[0]
    assert first.source == "arxiv"
    assert first.title == "Scalable Oversight: A Survey"
    assert first.authors == ["Ethan Perez", "Buck Shlegeris"]
    assert first.url == "http://arxiv.org/abs/2406.01234v1"
    assert first.pdf_url == "http://arxiv.org/pdf/2406.01234v1"
    assert first.published_at == "2026-06-18T10:00:00Z"
    assert "scalable oversight" in first.abstract.lower()

    # whitespace inside the title is collapsed
    assert items[1].title == "Interpretability of Sparse Autoencoders"


def test_author_query_url_encodes_author():
    url = author_query_url("Neel Nanda", max_results=25)
    assert "export.arxiv.org/api/query" in url
    assert "Neel" in url and "Nanda" in url
    assert "max_results=25" in url
    assert "sortBy=submittedDate" in url


def test_source_queries_each_author_and_filters_by_since(fixture_text):
    calls = []

    def fake_fetch(url: str) -> str:
        calls.append(url)
        return fixture_text("arxiv_response.xml")

    src = ArxivSource(authors=["Ethan Perez", "Neel Nanda"], fetch=fake_fetch)
    items = list(src.fetch(since="2026-06-15T00:00:00Z"))

    # one query per author
    assert len(calls) == 2
    # fixture has 2 items per author but only the 2026-06-18 one passes `since`
    assert all(i.published_at >= "2026-06-15T00:00:00Z" for i in items)
    assert len(items) == 2  # one passing item per author query


def test_source_without_since_returns_all(fixture_text):
    src = ArxivSource(authors=["Ethan Perez"], fetch=lambda url: fixture_text("arxiv_response.xml"))
    items = list(src.fetch())
    assert len(items) == 2
