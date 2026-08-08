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
    # a single id string and a one-element list behave the same
    n_list = export_groundtruth(
        "xoxp-test", ["C05UTTS1RNV"], oldest=None, path=out, fetch=fetch
    )
    assert n_list == 3
    rows = list(csv.DictReader(out.open()))
    assert [r["option"] for r in rows] == ["1", "2", "3"]
    assert rows[0]["votes"] == "4"
    assert rows[0]["attendance"] == "4"  # no user lists -> top ballot count
    assert rows[0]["url"] == "https://arxiv.org/abs/2605.01642"


POLL_MSG_T2 = {
    "ts": "1783468800.000200",  # a week after POLL_MSG
    "text": (
        "next week:\n"
        "• :one:<https://a.example/x|First>\n"
        "• :two:<https://b.example/y|Second>\n"
    ),
    "reactions": [{"name": "one", "count": 3}, {"name": "two", "count": 1}],
}


def _fetch_returning(*msgs, captured=None):
    def fetch(token, channel_id, oldest, cursor):
        if captured is not None:
            captured["oldest"] = oldest
        return {
            "ok": True,
            "messages": list(msgs),
            "response_metadata": {"next_cursor": ""},
        }

    return fetch


def test_export_append_only_adds_new_polls(tmp_path):
    out = tmp_path / "gt.csv"
    export_groundtruth(
        "t", "C1", oldest=None, path=out, fetch=_fetch_returning(POLL_MSG)
    )
    original_lines = out.read_text().splitlines()

    n = export_groundtruth(
        "t",
        "C1",
        oldest=None,
        path=out,
        fetch=_fetch_returning(POLL_MSG, POLL_MSG_T2),
        append=True,
    )
    assert n == 2  # only T2's options count
    lines = out.read_text().splitlines()
    assert lines[: len(original_lines)] == original_lines  # untouched, in place
    rows = list(csv.DictReader(out.open()))
    assert [r["message_ts"] for r in rows] == [
        POLL_MSG["ts"]] * 3 + [POLL_MSG_T2["ts"]] * 2
    assert [r["votes"] for r in rows[3:]] == ["3", "1"]


def test_export_append_dedups_by_message_ts(tmp_path):
    out = tmp_path / "gt.csv"
    export_groundtruth(
        "t", "C1", oldest=None, path=out, fetch=_fetch_returning(POLL_MSG)
    )
    before = out.read_text()
    n = export_groundtruth(
        "t", "C1", oldest=None, path=out, fetch=_fetch_returning(POLL_MSG), append=True
    )
    assert n == 0
    assert out.read_text() == before


def test_export_append_uses_max_ts_as_oldest(tmp_path):
    out = tmp_path / "gt.csv"
    export_groundtruth(
        "t", "C1", oldest=None, path=out,
        fetch=_fetch_returning(POLL_MSG, POLL_MSG_T2),
    )
    captured = {}
    export_groundtruth(
        "t", "C1", oldest="ignored", path=out,
        fetch=_fetch_returning(captured=captured), append=True,
    )
    assert captured["oldest"] == POLL_MSG_T2["ts"]


def test_export_append_falls_back_when_file_missing(tmp_path):
    out = tmp_path / "gt.csv"
    captured = {}
    n = export_groundtruth(
        "t", "C1", oldest="1234567890.0", path=out,
        fetch=_fetch_returning(POLL_MSG, captured=captured), append=True,
    )
    assert captured["oldest"] == "1234567890.0"
    assert n == 3
    rows = list(csv.DictReader(out.open()))
    assert len(rows) == 3 and rows[0]["week"]  # header written, rows land


def test_export_append_preserves_hand_deletions(tmp_path):
    # File holds only T2 (T1 hand-pruned); oldest becomes T2's ts, so T1 is
    # never re-fetched and stays absent.
    out = tmp_path / "gt.csv"
    export_groundtruth(
        "t", "C1", oldest=None, path=out, fetch=_fetch_returning(POLL_MSG_T2)
    )
    captured = {}
    export_groundtruth(
        "t", "C1", oldest=None, path=out,
        fetch=_fetch_returning(captured=captured), append=True,
    )
    assert captured["oldest"] == POLL_MSG_T2["ts"]
    rows = list(csv.DictReader(out.open()))
    assert POLL_MSG["ts"] not in {r["message_ts"] for r in rows}


def test_export_append_repairs_missing_trailing_newline(tmp_path):
    # Hand edits in some editors strip the final newline; appending must not
    # glue the first new row onto the last existing one.
    out = tmp_path / "gt.csv"
    export_groundtruth(
        "t", "C1", oldest=None, path=out, fetch=_fetch_returning(POLL_MSG)
    )
    out.write_text(out.read_text().rstrip("\n"))
    export_groundtruth(
        "t", "C1", oldest=None, path=out,
        fetch=_fetch_returning(POLL_MSG_T2), append=True,
    )
    rows = list(csv.DictReader(out.open()))
    assert len(rows) == 5
    assert {r["message_ts"] for r in rows} == {POLL_MSG["ts"], POLL_MSG_T2["ts"]}


def test_parse_poll_message_captures_attendance_from_reactors():
    msg = {
        "ts": "1.0",
        "text": (
            "picks:\n"
            "• :one:<https://a.example/x|First>\n"
            "• :two:<https://b.example/y|Second>\n"
        ),
        "reactions": [
            {"name": "one", "count": 2, "users": ["U1", "U2"]},
            {"name": "two", "count": 2, "users": ["U2", "U3"]},
        ],
    }
    # distinct union {U1, U2, U3} = 3, shared across the poll's options
    assert [o.attendance for o in parse_poll_message(msg)] == [3, 3]


def test_parse_poll_message_attendance_falls_back_to_top_count():
    # POLL_MSG reactions carry counts but no user lists -> top ballot count (4).
    assert all(o.attendance == 4 for o in parse_poll_message(POLL_MSG))


# -- hand-edit detection (changed_polls) --------------------------------------

_HDR = "week,message_ts,option,emoji,votes,url,context\n"


def _rows(*lines):
    return _HDR + "".join(l + "\n" for l in lines)


def test_changed_polls_empty_when_identical_or_no_snapshot(tmp_path):
    from paper_watch.groundtruth import changed_polls

    cur = tmp_path / "gt.csv"
    snap = tmp_path / "gt.csv.imported"
    cur.write_text(_rows("2026-W27,111.0,1,one,3,https://x/a,A"))
    assert changed_polls(cur, snap) == set()  # no snapshot yet
    snap.write_text(cur.read_text())
    assert changed_polls(cur, snap) == set()


def test_changed_polls_detects_edits_removals_and_added_rows(tmp_path):
    from paper_watch.groundtruth import changed_polls

    cur = tmp_path / "gt.csv"
    snap = tmp_path / "gt.csv.imported"
    snap.write_text(_rows(
        "2026-W27,111.0,1,one,3,https://x/a,A",
        "2026-W27,111.0,2,two,3,https://x/b,B",
        "2026-W28,222.0,1,one,4,https://x/c,C",
        "2026-W29,333.0,1,one,2,https://x/d,D",
    ))
    cur.write_text(_rows(
        "2026-W27,111.0,1,one,5,https://x/a,A",       # vote hand-corrected
        "2026-W27,111.0,2,two,3,https://x/b,B",
        "2026-W28,222.0,1,one,4,https://x/c,C",        # untouched
        "2026-W28,222.0,2,two,1,https://x/c2,C2",      # row hand-added
        "2026-W30,444.0,1,one,6,https://x/e,E",        # new poll (normal append)
    ))
    # 333.0 deleted, 111.0 edited, 222.0 gained a row; 444.0 is new, not a change
    assert changed_polls(cur, snap) == {"111.0", "222.0", "333.0"}


def test_export_append_migrates_an_old_seven_column_file(tmp_path):
    """A pre-attendance CSV gains the attendance column on first append, so
    appended 8-field rows don't shift under the old 7-name header."""
    from paper_watch.eval import load_groundtruth
    from paper_watch.groundtruth import export_groundtruth

    path = tmp_path / "gt.csv"
    path.write_text(
        "week,message_ts,option,emoji,votes,url,context\n"
        "2026-W29,111.0,1,one,3,https://x/old,Old\n"
    )

    def fetch(token, channel_id, oldest, cursor):
        return {
            "ok": True,
            "messages": [{
                "ts": "222.0",
                "text": ":one: <https://x/new|New A>\n:two: <https://x/new2|New B>",
                "reactions": [
                    {"name": "one", "count": 3, "users": ["u1", "u2", "u3"]},
                    {"name": "two", "count": 1, "users": ["u1"]},
                ],
            }],
        }

    n = export_groundtruth("tok", "C1", oldest=None, path=path, fetch=fetch, append=True)
    assert n == 2
    rows = load_groundtruth(path)
    by_url = {r.url: r for r in rows}
    assert by_url["https://x/old"].votes == 3
    assert by_url["https://x/old"].attendance is None
    assert by_url["https://x/new"].votes == 3
    assert by_url["https://x/new"].attendance is not None
    # single header line, new-format
    text = path.read_text()
    assert text.count("week,message_ts") == 1
    assert text.splitlines()[0].split(",") == [
        "week", "message_ts", "option", "emoji", "votes", "attendance", "url", "context"
    ]
