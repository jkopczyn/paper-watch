import json
from pathlib import Path

import pytest

from paper_watch.sources.lesswrong import (
    _ENDPOINT,
    _MAX_ABSTRACT_CHARS,
    LessWrongResolver,
    post_id_from_url,
)

FIXTURES = Path(__file__).parent / "fixtures"
POST_URL = "https://www.lesswrong.com/posts/GNnHHmm8EzePmKzPk/value-is-fragile"
SEQUENCE_URL = "https://www.alignmentforum.org/s/NouBkh4qnK8uKwn3L/p/Z8kLbceGBMWB5HGfn"


def _fixture(name):
    return json.loads((FIXTURES / name).read_text())


def _result(response):
    return response["data"]["post"]["result"]


def _resolver(response, calls=None):
    def post(url, payload):
        if calls is not None:
            calls.append((url, payload))
        if isinstance(response, Exception):
            raise response
        return response

    return LessWrongResolver(post=post)


def test_post_id_from_url_reads_the_id():
    assert post_id_from_url(POST_URL) == "GNnHHmm8EzePmKzPk"


def test_post_id_from_url_accepts_mirror_hosts():
    for host in (
        "https://www.alignmentforum.org",
        "https://www.greaterwrong.com",
        "https://lesswrong.com",
    ):
        url = f"{host}/posts/GNnHHmm8EzePmKzPk/value-is-fragile"
        assert post_id_from_url(url) == "GNnHHmm8EzePmKzPk"


def test_post_id_from_url_reads_the_sequence_form():
    assert post_id_from_url(SEQUENCE_URL) == "Z8kLbceGBMWB5HGfn"
    for host in ("https://www.lesswrong.com", "https://www.greaterwrong.com"):
        url = f"{host}/s/NouBkh4qnK8uKwn3L/p/Z8kLbceGBMWB5HGfn"
        assert post_id_from_url(url) == "Z8kLbceGBMWB5HGfn"
        assert post_id_from_url(url + "/") == "Z8kLbceGBMWB5HGfn"


def test_post_id_from_url_ignores_non_post_urls():
    for url in (
        "https://www.lesswrong.com/",
        "https://www.lesswrong.com/tag/ai",
        "https://www.alignmentforum.org/s/NouBkh4qnK8uKwn3L",
        "https://arxiv.org/abs/2506.18032",
        None,
    ):
        assert post_id_from_url(url) is None


def test_post_id_from_url_ignores_lookalike_hosts():
    assert post_id_from_url("https://example.com/posts/GNnHHmm8EzePmKzPk/value-is-fragile") is None


def test_post_id_from_url_keeps_comment_links():
    url = POST_URL + "?commentId=abc"
    assert post_id_from_url(url) == "GNnHHmm8EzePmKzPk"


def test_resolve_sends_the_extracted_id_in_the_variables():
    calls = []
    _resolver(_fixture("lesswrong_post.json"), calls).resolve(POST_URL)
    (url, payload), = calls
    assert url == _ENDPOINT
    assert payload["variables"]["id"] == "GNnHHmm8EzePmKzPk"


def test_resolve_sends_the_post_id_for_a_sequence_url():
    calls = []
    _resolver(_fixture("lesswrong_sequence_post.json"), calls).resolve(SEQUENCE_URL)
    (url, payload), = calls
    assert url == _ENDPOINT
    assert payload["variables"]["id"] == "Z8kLbceGBMWB5HGfn"


def test_resolve_returns_title_authors_abstract_and_date():
    response = _fixture("lesswrong_post.json")
    post = _result(response)
    meta = _resolver(response).resolve(POST_URL)

    assert meta["title"] == " ".join(post["title"].split())
    expected_authors = [post["user"]["displayName"]] + [
        c["displayName"] for c in post["coauthors"] or []
    ]
    assert meta["authors"] == expected_authors
    body = post["contents"]["plaintextDescription"].strip()
    assert meta["abstract"] == body[:_MAX_ABSTRACT_CHARS]
    assert len(meta["abstract"]) <= _MAX_ABSTRACT_CHARS
    assert meta["published_at"] == post["postedAt"].split(".")[0] + "Z"


def test_resolve_returns_none_for_a_missing_post():
    response = {
        "data": {"post": {"result": None}},
        "errors": None,
    }
    assert _resolver(response).resolve(POST_URL) is None


def test_resolve_returns_none_on_graphql_errors():
    response = {"data": None, "errors": [{"message": "app.missing_document"}]}
    assert _resolver(response).resolve(POST_URL) is None


def test_resolve_returns_none_for_a_non_post_url():
    calls = []
    resolver = _resolver(_fixture("lesswrong_post.json"), calls)
    assert resolver.resolve("https://arxiv.org/abs/2506.18032") is None
    assert calls == []


def test_resolve_date_returns_only_the_date():
    response = _fixture("lesswrong_post.json")
    expected = _result(response)["postedAt"].split(".")[0] + "Z"
    assert _resolver(response).resolve_date(POST_URL) == expected
    assert _resolver({"data": {"post": {"result": None}}}).resolve_date(POST_URL) is None


def test_resolve_never_raises_on_transport_failure():
    resolver = _resolver(RuntimeError("connection refused"))
    assert resolver.resolve(POST_URL) is None
    assert resolver.resolve_date(POST_URL) is None


def test_parse_post_tolerates_a_malformed_body():
    from paper_watch.sources.lesswrong import parse_post

    assert parse_post({}) is None
    assert parse_post({"data": {}}) is None
    assert parse_post({"data": {"post": {"result": {"title": ""}}}}) is None


@pytest.mark.parametrize("name", ["lesswrong_post.json", "lesswrong_sequence_post.json"])
def test_fixtures_carry_a_result(name):
    assert _result(_fixture(name))["_id"]
