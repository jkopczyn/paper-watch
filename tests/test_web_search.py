import json
from dataclasses import dataclass

from paper_watch.sources.web_search import (
    WebSearchResolver,
    parse_json_object,
    web_search_tool,
)


@dataclass
class _Text:
    text: str
    type: str = "text"


@dataclass
class _Resp:
    content: list
    stop_reason: str = "end_turn"


class _FakeClient:
    """Mimics anthropic.Anthropic().messages.create for the web_search flow."""

    def __init__(self, script):
        self._script = list(script)  # list of _Resp to return in order
        self.calls = []

    class _Messages:
        def __init__(self, outer):
            self.outer = outer

        def create(self, **kwargs):
            self.outer.calls.append(kwargs)
            return self.outer._script.pop(0)

    @property
    def messages(self):
        return _FakeClient._Messages(self)


def _reply(obj) -> _Resp:
    return _Resp(content=[_Text(json.dumps(obj))])


def test_tool_version_selected_by_model():
    assert web_search_tool("claude-opus-4-8", max_uses=3)["type"] == "web_search_20260209"
    assert web_search_tool("claude-haiku-4-5", max_uses=3)["type"] == "web_search_20250305"


def test_parse_json_object_tolerates_code_fences_and_prose():
    assert parse_json_object('{"title": "X"}') == {"title": "X"}
    fenced = 'Here it is:\n```json\n{"title": "Y", "snippet": "z"}\n```'
    assert parse_json_object(fenced) == {"title": "Y", "snippet": "z"}
    assert parse_json_object("no json here") is None


def test_resolve_returns_recovered_fields():
    client = _FakeClient([_reply({"title": "Deep Learning", "snippet": "A survey.", "abstract": "We review..."})])
    out = WebSearchResolver("claude-haiku-4-5", client=client).resolve("https://dead.link/x")
    assert out == {"title": "Deep Learning", "snippet": "A survey.", "abstract": "We review..."}
    # the URL and web_search tool were actually sent
    assert "dead.link" in client.calls[0]["messages"][0]["content"]
    assert client.calls[0]["tools"][0]["name"] == "web_search"


def test_resolve_none_when_model_cannot_identify():
    client = _FakeClient([_reply({"title": None})])
    assert WebSearchResolver("claude-haiku-4-5", client=client).resolve("https://x") is None


def test_resolve_resumes_on_pause_turn():
    paused = _Resp(content=[_Text("")], stop_reason="pause_turn")
    done = _reply({"title": "Recovered"})
    client = _FakeClient([paused, done])
    out = WebSearchResolver("claude-haiku-4-5", client=client).resolve("https://x")
    assert out["title"] == "Recovered"
    assert len(client.calls) == 2  # paused, then resumed


def test_resolve_refusal_returns_none():
    client = _FakeClient([_Resp(content=[], stop_reason="refusal")])
    assert WebSearchResolver("claude-haiku-4-5", client=client).resolve("https://x") is None


def test_resolve_blank_url_returns_none():
    client = _FakeClient([])  # never called
    assert WebSearchResolver("claude-haiku-4-5", client=client).resolve("") is None
