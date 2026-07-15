from paper_watch.config import GraphqlFeedConfig
from paper_watch.normalize import to_entry_fields
from paper_watch.sources.graphql import _MAX_TEXT_CHARS, GraphqlSource

ENDPOINT = "https://www.lesswrong.com/graphql"


def _feed(**overrides):
    defaults = dict(
        name="LessWrong AI",
        endpoint=ENDPOINT,
        tag_id="sYm3HiWcfZvrGu3ui",
        min_karma=30,
    )
    return GraphqlFeedConfig(**{**defaults, **overrides})


def _post(title="A Post", karma=50, **overrides):
    post = {
        "title": title,
        "url": None,
        "pageUrl": f"https://www.lesswrong.com/posts/abc123/{title.lower().replace(' ', '-')}",
        "postedAt": "2026-07-13T17:20:06.976Z",
        "baseScore": karma,
        "user": {"displayName": "Alice"},
        "coauthors": [],
        "contents": {"plaintextDescription": "Some body text."},
    }
    post.update(overrides)
    return post


def _response(*posts):
    return {"data": {"posts": {"results": list(posts)}}}


def _fetch(feed, response, since=None):
    src = GraphqlSource([feed], post=lambda url, payload: response)
    return list(src.fetch(since))


def test_fetch_maps_posts_and_normalizes_timestamps():
    payloads = {}

    def post(url, payload):
        payloads[url] = payload
        return _response(
            _post(coauthors=[{"displayName": "Bob"}], contents={"plaintextDescription": "x" * 5000})
        )

    items = list(GraphqlSource([_feed()], post=post).fetch())

    assert len(items) == 1
    item = items[0]
    assert item.source == "graphql:LessWrong AI"
    assert item.url.startswith("https://www.lesswrong.com/posts/abc123/")
    assert item.mention_url is None  # not a linkpost: the post is its own URL
    assert item.title == "A Post"
    assert item.authors == ["Alice", "Bob"]
    assert item.published_at == "2026-07-13T17:20:06Z"  # ms stripped, 'Z' kept
    assert len(item.text) == _MAX_TEXT_CHARS
    # the tag filter and karma window went out in the request
    terms = payloads[ENDPOINT]["variables"]["terms"]
    assert terms["filterSettings"]["tags"] == [
        {"tagId": "sYm3HiWcfZvrGu3ui", "filterMode": "Required"}
    ]
    assert terms == {**terms, "sortedBy": "new", "limit": 50}


def test_posts_below_min_karma_are_dropped():
    response = _response(
        _post(title="Popular", karma=31),
        _post(title="Fresh Take", karma=12),
        _post(title="No Score", karma=None),
    )
    items = _fetch(_feed(), response)
    assert [i.title for i in items] == ["Popular"]


def test_since_filters_on_post_date():
    response = _response(_post())
    assert _fetch(_feed(), response, since="2026-07-14T00:00:00Z") == []
    assert len(_fetch(_feed(), response, since="2026-07-13T00:00:00Z")) == 1


def test_linkpost_adopts_target_url_and_keeps_post_as_mention():
    response = _response(
        _post(title="New Interp Paper", url="https://arxiv.org/abs/2506.18032")
    )
    (item,) = _fetch(_feed(), response)
    assert item.url == "https://arxiv.org/abs/2506.18032"
    assert item.mention_url.startswith("https://www.lesswrong.com/posts/")
    assert to_entry_fields(item)["arxiv_id"] == "2506.18032"


def test_ids_in_body_text_are_citations_not_identity():
    response = _response(
        _post(contents={"plaintextDescription": "See https://arxiv.org/abs/2506.18032"})
    )
    (item,) = _fetch(_feed(), response)
    assert to_entry_fields(item)["arxiv_id"] is None


def test_failing_or_erroring_feed_skips_without_aborting_the_rest():
    good = _feed()
    down = _feed(name="Down", endpoint="https://down.example.com/graphql")
    errors = _feed(name="Errors", endpoint="https://errors.example.com/graphql")

    def post(url, payload):
        if url == down.endpoint:
            raise RuntimeError("connection refused")
        if url == errors.endpoint:
            return {"errors": [{"message": "GRAPHQL_VALIDATION_FAILED"}]}
        return _response(_post())

    items = list(GraphqlSource([down, errors, good], post=post).fetch())
    assert [i.source for i in items] == ["graphql:LessWrong AI"]


def test_config_defaults():
    feed = _feed()
    assert feed.min_karma == 30
    assert feed.limit == 50
