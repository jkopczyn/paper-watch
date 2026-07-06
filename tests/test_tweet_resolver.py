import httpx

from paper_watch.identity import canonicalize_url, extract_arxiv_id
from paper_watch.models import RawItem
from paper_watch.sources.tweet_resolver import (
    TweetResolver,
    is_tweet_url,
    parse_tweet_html,
)
from paper_watch.store import Store

TWEET = "https://twitter.com/someone/status/111"


def _req(url):
    return httpx.Request("GET", url)


def _resolver(store, fetch):
    return TweetResolver(store, "http://localhost:8080", fetch=fetch, sleep=lambda _s: None)


# -- parsing ---------------------------------------------------------------
def test_parse_extracts_text_links_and_quote(fixture_text):
    res = parse_tweet_html(fixture_text("nitter_tweet.html"))
    assert res is not None
    assert "Great new paper" in res.text
    assert "https://arxiv.org/abs/2606.08243" in res.links
    assert res.quoted_url == "https://twitter.com/otheruser/status/999"
    assert res.reply_url is None


def test_parse_extracts_thread_reply(fixture_text):
    res = parse_tweet_html(fixture_text("nitter_thread_1.html"))
    assert res.reply_url == "https://twitter.com/ikiran013/status/222"


def test_parse_garbage_is_none():
    assert parse_tweet_html("<html><body>nothing here</body></html>") is None
    assert parse_tweet_html("") is None


def test_is_tweet_url_canonical_only():
    assert is_tweet_url(TWEET) == ("someone", "111")
    assert is_tweet_url("https://x.com/someone/status/111") is None  # not canonical
    assert is_tweet_url(canonicalize_url("https://x.com/someone/status/111")) == ("someone", "111")
    assert is_tweet_url("https://example.com/paper") is None


# -- resolve / cache -------------------------------------------------------
def test_resolve_is_cache_first(tmp_path, fixture_text):
    store = Store(tmp_path / "pw.db")
    calls = []

    def fetch(url):
        calls.append(url)
        return fixture_text("nitter_tweet.html")

    r = _resolver(store, fetch)
    first = r.resolve(TWEET)
    assert "https://arxiv.org/abs/2606.08243" in first.links
    r.resolve(TWEET)
    assert len(calls) == 1  # second call served from cache


def test_resolve_404_is_cached_miss(tmp_path):
    store = Store(tmp_path / "pw.db")
    calls = []

    def fetch(url):
        calls.append(url)
        raise httpx.HTTPStatusError("nf", request=_req(url), response=httpx.Response(404, request=_req(url)))

    r = _resolver(store, fetch)
    assert r.resolve(TWEET) is None
    assert r.resolve(TWEET) is None
    assert len(calls) == 1  # miss is sticky, no refetch
    assert store.get_tweet_cache(TWEET)["status"] == "miss"


def test_resolve_transport_error_not_cached(tmp_path):
    store = Store(tmp_path / "pw.db")
    calls = []

    def fetch(url):
        calls.append(url)
        raise httpx.ConnectError("down")

    r = _resolver(store, fetch)
    assert r.resolve(TWEET) is None
    assert store.get_tweet_cache(TWEET) is None  # transient: retry next run
    r.resolve(TWEET)
    assert len(calls) == 2


# -- augment ---------------------------------------------------------------
def test_augment_noop_when_id_present(tmp_path):
    store = Store(tmp_path / "pw.db")
    called = []
    r = _resolver(store, lambda u: called.append(u) or "")
    raw = RawItem(source="slack:x", url=TWEET, text="see https://arxiv.org/abs/2606.08243")
    assert r.augment(raw) is raw
    assert not called


def test_augment_noop_for_non_tweet_and_rss(tmp_path, fixture_text):
    store = Store(tmp_path / "pw.db")
    r = _resolver(store, lambda u: fixture_text("nitter_tweet.html"))
    non_tweet = RawItem(source="slack:x", url="https://example.com/p", text="hi")
    assert r.augment(non_tweet) is non_tweet
    rss = RawItem(source="rss:Import AI", url=TWEET, text="hi")
    assert r.augment(rss) is rss


def test_augment_recovers_id_and_clears_post_title(tmp_path, fixture_text):
    store = Store(tmp_path / "pw.db")
    r = _resolver(store, lambda u: fixture_text("nitter_tweet.html"))
    raw = RawItem(source="slack:x", url=TWEET, title="Someone (@h) on X", text="check this")
    out = r.augment(raw)
    assert extract_arxiv_id(out.text)
    assert out.title is None  # post-shaped title dropped so a real one is derived


def test_augment_follows_one_quote(tmp_path, fixture_text):
    store = Store(tmp_path / "pw.db")
    main = (
        '<div class="main-tweet"><div class="tweet-content" dir="auto">quoted below</div>'
        '<a class="quote-link" href="/otheruser/status/999#m"></a></div><div class="replies"></div>'
    )
    pages = {
        "http://localhost:8080/someone/status/111": main,
        "http://localhost:8080/otheruser/status/999": fixture_text("nitter_quote.html"),
    }
    r = _resolver(store, lambda u: pages[u])
    out = r.augment(RawItem(source="slack:x", url=TWEET, text="look"))
    assert extract_arxiv_id(out.text) == "2606.08243"


def test_augment_crawls_self_thread(tmp_path, fixture_text):
    store = Store(tmp_path / "pw.db")
    pages = {
        "http://localhost:8080/ikiran013/status/111": fixture_text("nitter_thread_1.html"),
        "http://localhost:8080/ikiran013/status/222": fixture_text("nitter_thread_2.html"),
    }
    r = _resolver(store, lambda u: pages[u])
    url = "https://twitter.com/ikiran013/status/111"
    out = r.augment(RawItem(source="slack:x", url=url, text="thread:"))
    assert extract_arxiv_id(out.text) == "2606.08243"


def test_augment_thread_respects_hop_cap(tmp_path, fixture_text):
    store = Store(tmp_path / "pw.db")
    pages = {
        "http://localhost:8080/ikiran013/status/111": fixture_text("nitter_thread_1.html"),
        "http://localhost:8080/ikiran013/status/222": fixture_text("nitter_thread_2.html"),
    }
    r = TweetResolver(
        store, "http://localhost:8080", fetch=lambda u: pages[u], sleep=lambda _s: None, max_thread_hops=0
    )
    out = r.augment(RawItem(source="slack:x", url="https://twitter.com/ikiran013/status/111", text="t"))
    assert extract_arxiv_id(out.text) is None  # never crawled to page 2


def test_augment_thread_same_author_only(tmp_path):
    store = Store(tmp_path / "pw.db")
    # thread_1 whose reply is by a DIFFERENT author must not be followed
    cross = (
        '<div class="main-tweet"><div class="tweet-content" dir="auto">1/2</div></div>'
        '<div class="replies"><a class="tweet-link" href="/otheruser/status/222#m"></a></div>'
    )
    called = []

    def fetch(u):
        called.append(u)
        return cross

    r = _resolver(store, fetch)
    out = r.augment(RawItem(source="slack:x", url="https://twitter.com/ikiran013/status/111", text="t"))
    assert extract_arxiv_id(out.text) is None
    assert called == ["http://localhost:8080/ikiran013/status/111"]  # cross-author reply skipped
