from paper_watch.config import FeedConfig
from paper_watch.normalize import to_entry_fields
from paper_watch.sources.rss import RssSource, parse_rss


def test_parse_rss(fixture_text):
    items = parse_rss(fixture_text("rss_feed.xml"), feed_name="ML Safety")
    assert len(items) == 2

    first = items[0]
    assert first.source == "rss:ML Safety"
    assert first.title == "SAEs find interpretable features"
    assert first.url == "https://newsletter.mlsafety.org/p/issue-42"
    assert first.published_at == "2026-06-18T09:00:00Z"
    assert "arxiv.org/abs/2406.05678" in first.text


def test_rss_item_arxiv_id_recoverable_by_normalize(fixture_text):
    items = parse_rss(fixture_text("rss_feed.xml"), feed_name="ML Safety")
    fields = to_entry_fields(items[0])
    assert fields["arxiv_id"] == "2406.05678"


def test_source_queries_each_feed_and_filters_since(fixture_text):
    calls = []

    def fake_fetch(url: str) -> str:
        calls.append(url)
        return fixture_text("rss_feed.xml")

    feeds = [
        FeedConfig(name="ML Safety", url="https://newsletter.mlsafety.org/feed"),
        FeedConfig(name="Safe AI", url="https://newsletter.safe.ai/feed"),
    ]
    src = RssSource(feeds=feeds, fetch=fake_fetch)
    items = list(src.fetch(since="2026-06-10T00:00:00Z"))

    assert len(calls) == 2
    # only the 2026-06-18 item passes the since filter, per feed
    assert all(i.published_at >= "2026-06-10T00:00:00Z" for i in items)
    assert len(items) == 2


def test_source_tolerates_a_failing_feed(fixture_text):
    def fetch(url: str) -> str:
        if "broken" in url:
            raise RuntimeError("feed down")
        return fixture_text("rss_feed.xml")

    feeds = [
        FeedConfig(name="Broken", url="https://broken/feed"),
        FeedConfig(name="ML Safety", url="https://newsletter.mlsafety.org/feed"),
    ]
    src = RssSource(feeds=feeds, fetch=fetch)
    items = list(src.fetch())
    # the broken feed is skipped, the good one still yields
    assert {i.source for i in items} == {"rss:ML Safety"}
