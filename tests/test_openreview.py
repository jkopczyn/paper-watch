import json

from paper_watch.sources.openreview import (
    OpenReviewResolver,
    _LOGIN,
    _env_login,
    forum_id,
)


def _fetch_from(fixture_text):
    def fetch(url, *, params=None, headers=None):
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
    r = OpenReviewResolver(fetch=_fetch_from(fixture_text), login=lambda: None)
    meta = r.resolve("https://openreview.net/forum?id=dy2HwmOvFX")
    assert meta["title"] == "A Structured Study of Oversight"
    assert "wrapped abstract" in meta["abstract"]
    assert meta["authors"] == ["Alice Ng", "Bob Lim"]


def test_resolve_falls_back_to_v1(fixture_text):
    # v2 endpoint returns no notes; v1 has the flat content.
    def fetch(url, *, params=None, headers=None):
        if "api2." in url:
            return {"notes": []}
        return json.loads(fixture_text("openreview_note_v1.json"))

    r = OpenReviewResolver(fetch=fetch, login=lambda: None)
    meta = r.resolve("https://openreview.net/forum?id=OldVenue123")
    assert meta["title"] == "An Older Venue Paper"
    assert meta["abstract"].startswith("A v1-style")
    assert meta["authors"] == ["Carol Reyes"]


def test_resolve_error_is_none():
    def fetch(url, *, params=None, headers=None):
        raise RuntimeError("boom")

    r = OpenReviewResolver(fetch=fetch, login=lambda: None)
    assert r.resolve("https://openreview.net/forum?id=x") is None


def test_resolve_non_openreview_is_none(fixture_text):
    r = OpenReviewResolver(fetch=_fetch_from(fixture_text), login=lambda: None)
    assert r.resolve("https://arxiv.org/abs/2606.08243") is None


# --- authentication ---


def test_env_login_returns_none_without_creds():
    calls = []

    def post(url, payload):
        calls.append(url)
        return {"token": "nope"}

    # Missing password.
    getenv = {"OPENREVIEW_USERNAME": "me@example.com"}.get
    assert _env_login(post=post, getenv=getenv) is None
    # Missing both.
    assert _env_login(post=post, getenv={}.get) is None
    assert calls == []  # never attempts a login POST


def test_env_login_posts_and_returns_token():
    seen = {}

    def post(url, payload):
        seen["url"] = url
        seen["payload"] = payload
        return {"token": "jwt-123", "user": {"id": "~Me1"}}

    getenv = {
        "OPENREVIEW_USERNAME": "me@example.com",
        "OPENREVIEW_PASSWORD": "s3cret",
    }.get
    assert _env_login(post=post, getenv=getenv) == "jwt-123"
    assert seen["url"] == _LOGIN
    assert seen["payload"] == {"id": "me@example.com", "password": "s3cret"}


def test_env_login_returns_none_on_post_error():
    def post(url, payload):
        raise RuntimeError("boom")

    getenv = {
        "OPENREVIEW_USERNAME": "me@example.com",
        "OPENREVIEW_PASSWORD": "s3cret",
    }.get
    assert _env_login(post=post, getenv=getenv) is None


def test_resolve_sends_bearer_header_when_authed(fixture_text):
    seen_headers = []

    def fetch(url, *, params=None, headers=None):
        seen_headers.append(headers)
        return json.loads(fixture_text("openreview_note_v2.json"))

    r = OpenReviewResolver(fetch=fetch, login=lambda: "jwt-123")
    r.resolve("https://openreview.net/forum?id=dy2HwmOvFX")
    assert seen_headers[0] == {"Authorization": "Bearer jwt-123"}


def test_resolve_sends_no_header_without_token(fixture_text):
    seen_headers = []

    def fetch(url, *, params=None, headers=None):
        seen_headers.append(headers)
        return json.loads(fixture_text("openreview_note_v2.json"))

    r = OpenReviewResolver(fetch=fetch, login=lambda: None)
    r.resolve("https://openreview.net/forum?id=dy2HwmOvFX")
    assert seen_headers[0] is None


def test_login_attempted_once_across_resolves(fixture_text):
    logins = []

    def login():
        logins.append(1)
        return "jwt-123"

    r = OpenReviewResolver(fetch=_fetch_from(fixture_text), login=login)
    r.resolve("https://openreview.net/forum?id=a")
    r.resolve("https://openreview.net/forum?id=b")
    assert len(logins) == 1  # token cached; login not repeated per resolve
