import json

from paper_watch.sources.openreview import OpenReviewResolver, forum_id


def _fetch_from(fixture_text):
    def fetch(url, *, params=None):
        if "api2." in url:
            return json.loads(fixture_text("openreview_note_v2.json"))
        return json.loads(fixture_text("openreview_note_v1.json"))

    return fetch


def test_forum_id_parsing():
    assert forum_id("https://openreview.net/forum?id=dy2HwmOvFX") == "dy2HwmOvFX"
    assert forum_id("https://openreview.net/pdf?id=abc123") == "abc123"
    assert forum_id("https://arxiv.org/abs/2606.08243") is None
    assert forum_id(None) is None


def test_resolve_reads_v2(fixture_text):
    r = OpenReviewResolver(fetch=_fetch_from(fixture_text))
    meta = r.resolve("https://openreview.net/forum?id=dy2HwmOvFX")
    assert meta["title"] == "A Structured Study of Oversight"
    assert "wrapped abstract" in meta["abstract"]
    assert meta["authors"] == ["Alice Ng", "Bob Lim"]


def test_resolve_falls_back_to_v1(fixture_text):
    # v2 endpoint returns no notes; v1 has the flat content.
    def fetch(url, *, params=None):
        if "api2." in url:
            return {"notes": []}
        return json.loads(fixture_text("openreview_note_v1.json"))

    r = OpenReviewResolver(fetch=fetch)
    meta = r.resolve("https://openreview.net/forum?id=OldVenue123")
    assert meta["title"] == "An Older Venue Paper"
    assert meta["abstract"].startswith("A v1-style")
    assert meta["authors"] == ["Carol Reyes"]


def test_resolve_error_is_none():
    def fetch(url, *, params=None):
        raise RuntimeError("boom")

    assert OpenReviewResolver(fetch=fetch).resolve("https://openreview.net/forum?id=x") is None


def test_resolve_non_openreview_is_none(fixture_text):
    r = OpenReviewResolver(fetch=_fetch_from(fixture_text))
    assert r.resolve("https://arxiv.org/abs/2606.08243") is None
