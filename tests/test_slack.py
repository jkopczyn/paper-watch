import json
from pathlib import Path

from paper_watch.config import SlackChannel, SlackWorkspace
from paper_watch.normalize import to_entry_fields
from paper_watch.sources.slack import (
    SlackSource,
    extract_urls,
    is_paper_link,
    ts_to_iso,
)

FIXTURES = Path(__file__).parent / "fixtures"
PAPER_DOMAINS = ["arxiv.org", "lesswrong.com", "alignmentforum.org"]


def _history_page():
    return json.loads((FIXTURES / "slack_history.json").read_text())


def _workspace(*, trusted=False, name="papers"):
    return SlackWorkspace(
        name="mats",
        token_env="SLACK_TOKEN_MATS",
        channels=[SlackChannel(id="C001", name=name, trusted=trusted)],
    )


def _source(workspaces, *, pages=None, token="xoxp-test", domains=PAPER_DOMAINS):
    """Build a SlackSource with an injected fetch that serves canned pages."""
    pages = pages if pages is not None else [_history_page()]
    served = {"i": 0}

    def fetch(tok, channel_id, oldest, cursor):
        assert tok == token
        page = pages[served["i"]]
        served["i"] = min(served["i"] + 1, len(pages) - 1)
        return page

    return SlackSource(
        workspaces,
        domains,
        fetch=fetch,
        get_token=lambda env: token,
    )

DOMAINS = [
    "arxiv.org",
    "lesswrong.com",
    "alignmentforum.org",
    "anthropic.com",
]


def test_is_paper_link_matches_exact_domain():
    assert is_paper_link("https://arxiv.org/abs/2406.05678", DOMAINS)


def test_is_paper_link_matches_subdomain():
    assert is_paper_link("https://www.lesswrong.com/posts/abc/title", DOMAINS)
    assert is_paper_link("https://transformer.anthropic.com/x", DOMAINS)


def test_is_paper_link_rejects_unlisted_domain():
    assert not is_paper_link("https://somebody.substack.com/p/post", DOMAINS)
    assert not is_paper_link("https://example.com/paper", DOMAINS)


def test_is_paper_link_no_substring_false_positive():
    # "notarxiv.org" must not match "arxiv.org"
    assert not is_paper_link("https://notarxiv.org/abs/1", DOMAINS)


def test_is_paper_link_handles_garbage():
    assert not is_paper_link("not a url", DOMAINS)
    assert not is_paper_link("", DOMAINS)


def test_extract_urls_from_slack_markup():
    text = "see <https://arxiv.org/abs/1|Title> and <https://x.example/y> and <@U1>"
    assert extract_urls(text) == ["https://arxiv.org/abs/1", "https://x.example/y"]


def test_ts_to_iso():
    assert ts_to_iso("1781784000.000200") == "2026-06-18T12:00:00Z"
    assert ts_to_iso(None) is None
    assert ts_to_iso("garbage") is None


def test_fetch_yields_one_item_per_paper_link():
    items = list(_source([_workspace()]).fetch())
    urls = [i.url for i in items]
    # arxiv, lesswrong, random blog, and the older arxiv -> 4 (chatter skipped)
    assert urls == [
        "https://arxiv.org/abs/2406.05678",
        "https://www.lesswrong.com/posts/abc123/deceptive-alignment",
        "https://randomblog.example/my-post",
        "https://arxiv.org/abs/2401.00001",
    ]


def test_fetch_skips_messages_with_no_link():
    texts = [i.text for i in _source([_workspace()]).fetch()]
    assert all("thanks for sharing" not in (t or "") for t in texts)


def test_source_label_and_metadata():
    item = next(iter(_source([_workspace()]).fetch()))
    assert item.source == "slack:mats:papers"
    assert item.title == "Scalable Oversight: A Survey"
    assert item.abstract.startswith("We survey")
    assert item.published_at == "2026-06-18T12:00:00Z"


def test_trust_via_paper_link_domain():
    items = {i.url: i for i in _source([_workspace()]).fetch()}
    assert items["https://arxiv.org/abs/2406.05678"].trusted is True
    assert items["https://www.lesswrong.com/posts/abc123/deceptive-alignment"].trusted is True
    # random blog in an untrusted channel is NOT trusted -> will hit the gate
    assert items["https://randomblog.example/my-post"].trusted is False


def test_trust_via_trusted_channel():
    items = {i.url: i for i in _source([_workspace(trusted=True)]).fetch()}
    # every item from a trusted channel bypasses the gate, even the blog link
    assert items["https://randomblog.example/my-post"].trusted is True


def test_arxiv_id_recovered_for_dedup():
    items = {i.url: i for i in _source([_workspace()]).fetch()}
    fields = to_entry_fields(items["https://arxiv.org/abs/2406.05678"])
    assert fields["arxiv_id"] == "2406.05678"


def test_fetch_filters_by_since():
    items = list(_source([_workspace()]).fetch(since="2026-06-10T00:00:00Z"))
    urls = [i.url for i in items]
    # the 2026-06-01 message is filtered out
    assert "https://arxiv.org/abs/2401.00001" not in urls
    assert "https://arxiv.org/abs/2406.05678" in urls


def test_fetch_follows_pagination():
    page1 = {
        "ok": True,
        "messages": [
            {"ts": "1781784000.000200", "text": "<https://arxiv.org/abs/1111.1>"}
        ],
        "response_metadata": {"next_cursor": "CURSOR2"},
    }
    page2 = {
        "ok": True,
        "messages": [
            {"ts": "1781773200.000100", "text": "<https://arxiv.org/abs/2222.2>"}
        ],
        "response_metadata": {"next_cursor": ""},
    }
    items = list(_source([_workspace()], pages=[page1, page2]).fetch())
    assert [i.url for i in items] == [
        "https://arxiv.org/abs/1111.1",
        "https://arxiv.org/abs/2222.2",
    ]


def test_missing_token_skips_workspace():
    src = SlackSource(
        [_workspace()],
        PAPER_DOMAINS,
        fetch=lambda *a: (_ for _ in ()).throw(AssertionError("should not fetch")),
        get_token=lambda env: None,
    )
    assert list(src.fetch()) == []


def test_channel_error_does_not_abort_other_channels():
    ws = SlackWorkspace(
        token_env="SLACK_TOKEN_MATS",
        name="mats",
        channels=[
            SlackChannel(id="CBAD", name="broken"),
            SlackChannel(id="CGOOD", name="papers"),
        ],
    )

    def fetch(tok, channel_id, oldest, cursor):
        if channel_id == "CBAD":
            raise RuntimeError("slack api error")
        return _history_page()

    src = SlackSource(ws and [ws], PAPER_DOMAINS, fetch=fetch, get_token=lambda e: "t")
    items = list(src.fetch())
    assert items  # the good channel still produced items
    assert all(i.source == "slack:mats:papers" for i in items)
