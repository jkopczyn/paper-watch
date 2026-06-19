from paper_watch.models import RawItem
from paper_watch.normalize import to_entry_fields


def test_arxiv_item_fields():
    raw = RawItem(
        source="arxiv",
        url="https://arxiv.org/abs/2406.01234",
        title="Scalable Oversight: A Survey",
        authors=["Ethan Perez", "Buck Shlegeris"],
        abstract="We survey scalable oversight.",
        pdf_url="https://arxiv.org/pdf/2406.01234",
        published_at="2026-06-18T00:00:00Z",
    )
    f = to_entry_fields(raw)
    assert f["arxiv_id"] == "2406.01234"
    assert f["title"] == "Scalable Oversight: A Survey"
    assert f["title_norm"] == "scalable oversight a survey"
    assert f["authors"] == ["Ethan Perez", "Buck Shlegeris"]
    assert f["links"]["abstract"] == "https://arxiv.org/abs/2406.01234"
    assert f["links"]["pdf"] == "https://arxiv.org/pdf/2406.01234"


def test_tweet_item_recovers_arxiv_id_from_text():
    raw = RawItem(
        source="twitter:NeelNanda5",
        url="https://nitter.net/NeelNanda5/status/123",
        text="New interp paper https://arxiv.org/abs/2406.01234 worth a read",
    )
    f = to_entry_fields(raw)
    assert f["arxiv_id"] == "2406.01234"
    # no explicit title -> falls back to the mention text
    assert "interp paper" in f["title"]
    assert f["title_norm"]  # non-empty
    assert f["links"]["abstract"] == "https://nitter.net/NeelNanda5/status/123"


def test_links_omit_missing_values():
    raw = RawItem(source="rss:Blog", url="https://blog/post", title="A Post")
    f = to_entry_fields(raw)
    assert f["links"] == {"abstract": "https://blog/post"}
    assert f["arxiv_id"] is None
    assert f["doi"] is None


def test_doi_recovered_from_text():
    raw = RawItem(
        source="rss:Blog",
        url="https://blog/post",
        title="A Post",
        text="published as doi:10.1038/s41586-020-2649-2",
    )
    f = to_entry_fields(raw)
    assert f["doi"] == "10.1038/s41586-020-2649-2"
