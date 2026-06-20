from paper_watch.sources.arxiv import (
    ArxivSource,
    author_query_url,
    authors_query_url,
    parse_arxiv_atom,
)


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


def test_authors_query_url_ors_multiple_authors():
    url = authors_query_url(["Ethan Perez", "Neel Nanda"], max_results=100)
    assert "OR" in url
    assert "Ethan" in url and "Neel" in url
    assert "max_results=100" in url


def test_source_batches_authors_into_one_query(fixture_text):
    calls = []

    def fake_fetch(url: str) -> str:
        calls.append(url)
        return fixture_text("arxiv_response.xml")

    # default batch_size is large enough to put both authors in one request
    src = ArxivSource(
        authors=["Ethan Perez", "Neel Nanda"], fetch=fake_fetch, sleep=lambda _s: None
    )
    list(src.fetch())

    assert len(calls) == 1  # batched, not one-per-author
    assert "Ethan" in calls[0] and "Neel" in calls[0]


def test_source_sleeps_between_batches(fixture_text):
    slept = []
    src = ArxivSource(
        authors=["A", "B", "C", "D"],
        fetch=lambda url: fixture_text("arxiv_response.xml"),
        batch_size=2,
        min_interval=3.0,
        sleep=slept.append,
    )
    list(src.fetch())
    # 4 authors / batch_size 2 = 2 batches => exactly one inter-batch sleep
    assert slept == [3.0]


def test_source_filters_by_since(fixture_text):
    src = ArxivSource(
        authors=["Ethan Perez"],
        fetch=lambda url: fixture_text("arxiv_response.xml"),
        sleep=lambda _s: None,
    )
    items = list(src.fetch(since="2026-06-15T00:00:00Z"))
    assert all(i.published_at >= "2026-06-15T00:00:00Z" for i in items)
    assert len(items) == 1  # only the 2026-06-18 entry passes


def test_source_skips_a_failing_batch(fixture_text):
    def flaky_fetch(url: str) -> str:
        raise RuntimeError("429 even after retries")

    src = ArxivSource(authors=["A", "B"], fetch=flaky_fetch, sleep=lambda _s: None)
    # a failing batch must not abort the whole run
    assert list(src.fetch()) == []
