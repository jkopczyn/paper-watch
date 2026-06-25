import httpx
import pytest

import paper_watch.http as http_mod


class FakeResp:
    def __init__(self, status_code, text="ok", headers=None, json_body=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self._json = json_body if json_body is not None else {}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)


def _patch_get(monkeypatch, responses):
    seq = list(responses)
    calls = []

    def fake_get(url, **kwargs):
        calls.append(url)
        return seq.pop(0)

    monkeypatch.setattr(http_mod.httpx, "get", fake_get)
    return calls


def test_get_text_retries_on_429_then_succeeds(monkeypatch):
    calls = _patch_get(
        monkeypatch,
        [FakeResp(429, headers={"Retry-After": "1"}), FakeResp(200, text="body")],
    )
    slept = []
    text = http_mod.get_text("http://x", max_retries=3, sleep=slept.append)
    assert text == "body"
    assert len(calls) == 2
    assert slept == [1.0]  # honored Retry-After header


def test_get_text_uses_backoff_without_retry_after(monkeypatch):
    _patch_get(monkeypatch, [FakeResp(429), FakeResp(200, text="ok")])
    slept = []
    http_mod.get_text("http://x", max_retries=3, sleep=slept.append)
    assert slept == [1.0]  # 2 ** attempt(0)


def test_get_text_raises_after_exhausting_retries(monkeypatch):
    _patch_get(monkeypatch, [FakeResp(429), FakeResp(429)])
    with pytest.raises(httpx.HTTPStatusError):
        http_mod.get_text("http://x", max_retries=1, sleep=lambda _s: None)


def test_get_text_returns_immediately_on_200(monkeypatch):
    calls = _patch_get(monkeypatch, [FakeResp(200, text="hi")])
    assert http_mod.get_text("http://x", sleep=lambda _s: None) == "hi"
    assert len(calls) == 1


def test_get_json_returns_parsed_body(monkeypatch):
    calls = _patch_get(monkeypatch, [FakeResp(200, json_body={"ok": True, "n": 3})])
    body = http_mod.get_json(
        "http://x", headers={"Authorization": "Bearer t"}, sleep=lambda _s: None
    )
    assert body == {"ok": True, "n": 3}
    assert len(calls) == 1


def test_get_json_retries_on_429(monkeypatch):
    _patch_get(
        monkeypatch,
        [FakeResp(429, headers={"Retry-After": "2"}), FakeResp(200, json_body={"ok": True})],
    )
    slept = []
    body = http_mod.get_json("http://x", max_retries=3, sleep=slept.append)
    assert body == {"ok": True}
    assert slept == [2.0]


def test_get_json_raises_after_exhausting_retries(monkeypatch):
    _patch_get(monkeypatch, [FakeResp(429), FakeResp(429)])
    with pytest.raises(httpx.HTTPStatusError):
        http_mod.get_json("http://x", max_retries=1, sleep=lambda _s: None)
