import csv

from paper_watch.groundtruth import export_groundtruth, parse_poll_message

POLL_TEXT = (
    "This week's papers — react to vote!\n"
    ":performing_arts: <https://arxiv.org/abs/2605.01642|Adaptive Pluralistic Alignment>\n"
    ":fish: <https://www.lesswrong.com/posts/abc123/consistency-training>\n"
    ":smiling_imp: <https://arxiv.org/abs/2606.11502> role-playing beliefs\n"
)

POLL_MSG = {
    "ts": "1782864000.000100",  # 2026-06-30T22:40:00Z-ish
    "text": POLL_TEXT,
    "reactions": [
        {"name": "performing_arts", "count": 4},
        {"name": "smiling_imp", "count": 2},
        {"name": "tada", "count": 7},  # not a ballot emoji -> ignored
    ],
}


def test_parse_poll_message_maps_votes_by_line_emoji():
    options = parse_poll_message(POLL_MSG)
    assert [o.option for o in options] == [1, 2, 3]
    assert [o.emoji for o in options] == ["performing_arts", "fish", "smiling_imp"]
    assert [o.votes for o in options] == [4, 0, 2]  # :fish: unreacted; :tada: ignored
    assert options[0].url == "https://arxiv.org/abs/2605.01642"
    assert "Adaptive Pluralistic" in options[0].context
    assert options[0].week == options[2].week != ""


def test_parse_poll_message_number_emoji_list_format():
    msg = {
        "ts": "1.0",
        "text": (
            "backlog picks:\n"
            "• :one:<https://a.example/x|First>\n"
            "• :two:<https://b.example/y|Second>\n"
        ),
        "reactions": [{"name": "one", "count": 2}, {"name": "two", "count": 3}],
    }
    options = parse_poll_message(msg)
    assert [(o.emoji, o.votes) for o in options] == [("one", 2), ("two", 3)]


def test_parse_poll_message_ignores_non_polls():
    assert parse_poll_message({"ts": "1.0", "text": "one link <https://a.example/x>"}) == []
    assert parse_poll_message({"ts": "1.0", "text": "no links at all"}) == []


def test_parse_poll_message_dedups_repeated_urls():
    msg = {
        "ts": "1.0",
        "text": "<https://a.example/x> then again <https://a.example/x> and <https://b.example/y>",
    }
    assert [o.url for o in parse_poll_message(msg)] == [
        "https://a.example/x",
        "https://b.example/y",
    ]


def test_export_groundtruth_writes_csv(tmp_path):
    def fetch(token, channel_id, oldest, cursor):
        assert token == "xoxp-test" and channel_id == "C05UTTS1RNV"
        return {
            "ok": True,
            "messages": [POLL_MSG, {"ts": "2.0", "text": "chatter, no links"}],
            "response_metadata": {"next_cursor": ""},
        }

    out = tmp_path / "gt.csv"
    n = export_groundtruth("xoxp-test", "C05UTTS1RNV", oldest=None, path=out, fetch=fetch)
    assert n == 3
    rows = list(csv.DictReader(out.open()))
    assert [r["option"] for r in rows] == ["1", "2", "3"]
    assert rows[0]["votes"] == "4"
    assert rows[0]["url"] == "https://arxiv.org/abs/2605.01642"
