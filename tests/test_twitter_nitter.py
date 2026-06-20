from paper_watch.normalize import to_entry_fields
from paper_watch.sources.twitter_nitter import (
    NitterSource,
    parse_nitter,
    rss_url,
)


def test_rss_url():
    assert rss_url("https://nitter.net/", "NeelNanda5") == "https://nitter.net/NeelNanda5/rss"


def test_parse_nitter(fixture_text):
    items = parse_nitter(fixture_text("nitter_feed.xml"), handle="NeelNanda5")
    assert len(items) == 2
    assert items[0].source == "twitter:NeelNanda5"
    assert items[0].url == "https://nitter.net/NeelNanda5/status/100"
    assert items[0].published_at == "2026-06-18T12:00:00Z"


def test_source_yields_only_paper_linking_tweets(fixture_text):
    src = NitterSource(
        handles=["NeelNanda5"],
        instances=["https://nitter.net"],
        fetch=lambda url: fixture_text("nitter_feed.xml"),
    )
    items = list(src.fetch())
    # the coffee tweet (no paper link) is filtered out
    assert len(items) == 1
    assert to_entry_fields(items[0])["arxiv_id"] == "2406.05678"


def test_source_falls_back_across_instances(fixture_text):
    def fetch(url: str) -> str:
        if "down.example" in url:
            raise RuntimeError("instance down")
        return fixture_text("nitter_feed.xml")

    src = NitterSource(
        handles=["NeelNanda5"],
        instances=["https://down.example", "https://nitter.net"],
        fetch=fetch,
    )
    items = list(src.fetch())
    assert len(items) == 1  # second instance served the feed


def test_source_skips_handle_when_all_instances_fail():
    def fetch(url: str) -> str:
        raise RuntimeError("all down")

    src = NitterSource(
        handles=["NeelNanda5"],
        instances=["https://a.example", "https://b.example"],
        fetch=fetch,
    )
    assert list(src.fetch()) == []


def test_source_sleeps_between_handles(fixture_text):
    slept = []
    src = NitterSource(
        handles=["A", "B", "C"],
        instances=["https://nitter.net"],
        fetch=lambda url: fixture_text("nitter_feed.xml"),
        min_interval=1.5,
        sleep=slept.append,
    )
    list(src.fetch())
    # 3 handles => exactly two inter-handle pauses
    assert slept == [1.5, 1.5]


_PINNED_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title>
<item>
  <title>Pinned: Adaptive Pluralistic Alignment https://arxiv.org/abs/2605.01642</title>
  <link>https://nitter.net/FreedmanRach/status/100#m</link>
  <pubDate>Mon, 29 Jun 2026 20:34:22 GMT</pubDate>
  <description>Pinned tweet body https://arxiv.org/abs/2605.01642</description>
</item>
<item>
  <title>Fresh tweet about https://arxiv.org/abs/2406.05678</title>
  <link>https://nitter.net/FreedmanRach/status/200#m</link>
  <pubDate>Tue, 30 Jun 2026 10:00:00 GMT</pubDate>
  <description>body</description>
</item>
</channel></rss>
"""


def test_pinned_tweets_are_skipped():
    items = parse_nitter(_PINNED_FEED, handle="FreedmanRach")
    assert len(items) == 1
    assert "Fresh tweet" in items[0].text


def test_source_filters_by_since(fixture_text):
    src = NitterSource(
        handles=["NeelNanda5"],
        instances=["https://nitter.net"],
        fetch=lambda url: fixture_text("nitter_feed.xml"),
    )
    items = list(src.fetch(since="2026-06-18T00:00:00Z"))
    assert len(items) == 1
    assert items[0].published_at >= "2026-06-18T00:00:00Z"
