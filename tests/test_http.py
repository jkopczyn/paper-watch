import httpx
import pytest

import paper_watch.http as http_mod


class FakeResp:
    def __init__(self, status_code, text="ok", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

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
