"""Vote-math for the real-votes feedback import: the turnout proxy, the
votes->target curve (checked against the user's sketch), and score scaling."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from paper_watch.config import ScoringWeights
from paper_watch.feedback import (
    _score_scale,
    import_file,
    import_votes,
    poll_attendance,
    votes_to_target,
)
from paper_watch.store import Store

TS1 = str(datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc).timestamp())  # 2026-W27
TS2 = str(datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc).timestamp())  # 2026-W28
SEEN = "2026-06-28T00:00:00Z"


def _cfg(authors=()):
    return SimpleNamespace(
        scoring=ScoringWeights(),
        source_priors={"default": 0.5, "arxiv": 0.6, "slack": 0.9},
        authors=list(authors),
        candidate_window_days=7,
    )


def _seed(store, title, *, arxiv_id, authors, relevance=8):
    eid = store.insert_entry(
        title=title, title_norm=title.lower(), first_seen_at=SEEN,
        arxiv_id=arxiv_id, authors=authors, abstract="a",
    )
    store.set_enrichment(eid, tldr="t", why="w", tags=["interp"], relevance=relevance, version=2)
    store.add_mention(
        entry_id=eid, source="slack:far:papers",
        source_item_url=f"https://arxiv.org/abs/{arxiv_id}", fetched_at=SEEN,
    )
    return eid


def _votes_csv(path):
    path.write_text(
        "week,message_ts,option,emoji,votes,url,context\n"
        f"2026-W27,{TS1},1,one,5,https://arxiv.org/abs/2605.01642,Winner\n"
        f"2026-W27,{TS1},2,two,1,https://arxiv.org/abs/2605.00002,Meh\n"
        f"2026-W27,{TS1},3,three,2,https://unknown.example/x,Unresolvable\n"
        f"2026-W27,{TS1},4,four,0,https://arxiv.org/abs/2605.00003,ZeroVote\n"
        f"2026-W28,{TS2},1,one,4,https://arxiv.org/abs/2605.01642,Winner again\n"
        f"2026-W28,{TS2},2,two,3,https://arxiv.org/abs/2605.00002,Meh again\n"
    )
    return path


def _seed_all(store):
    a = _seed(store, "Winner", arxiv_id="2605.01642", authors=["Alice"])
    b = _seed(store, "Meh", arxiv_id="2605.00002", authors=["Bob"])
    _seed(store, "ZeroVote", arxiv_id="2605.00003", authors=["Cara"])
    return a, b


def test_import_votes_counts_and_weight_directions(tmp_path):
    store = Store(tmp_path / "pw.db")
    _seed_all(store)
    res = import_votes(store, path=_votes_csv(tmp_path / "gt.csv"), config=_cfg())

    # 2 entries x 2 weeks; the 0-vote option skipped; the unknown URL unresolved.
    assert (res.imported, res.skipped_zero, res.unresolved) == (4, 1, 1)

    weights = store.get_feedback_weights()
    # Alice swept a high-turnout poll -> positive; Bob's lone/low votes -> negative.
    assert weights[("author", "Alice")] > 0
    assert weights[("author", "Bob")] < 0
    store.close()


def test_import_votes_records_per_row_week_and_winner_picked(tmp_path):
    store = Store(tmp_path / "pw.db")
    a, b = _seed_all(store)
    import_votes(store, path=_votes_csv(tmp_path / "gt.csv"), config=_cfg())

    fb = {(r["entry_id"], r["week"]): r for r in store.conn.execute(
        "SELECT entry_id, week, picked FROM feedback"
    ).fetchall()}
    # keyed by each row's own week, not today's
    assert (a, "2026-W27") in fb and (a, "2026-W28") in fb
    # winner of the W27 poll (5 votes) is picked; the 1-vote runner-up is not
    assert fb[(a, "2026-W27")]["picked"] == 1
    assert fb[(b, "2026-W27")]["picked"] == 0
    store.close()


def test_import_votes_week_filter(tmp_path):
    store = Store(tmp_path / "pw.db")
    _seed_all(store)
    res = import_votes(
        store, path=_votes_csv(tmp_path / "gt.csv"), config=_cfg(), week_filter="2026-W28"
    )
    # only the two W28 rows; the unresolved/zero rows live in W27
    assert (res.imported, res.skipped_zero, res.unresolved) == (2, 0, 0)
    store.close()


def test_import_file_routes_by_header(tmp_path):
    store = Store(tmp_path / "pw.db")
    _seed_all(store)
    summary = import_file(
        store, path=_votes_csv(tmp_path / "gt.csv"), week=None, config=_cfg()
    )
    assert "vote row(s)" in summary

    cand = tmp_path / "c.csv"
    cand.write_text("entry_id,title,picked,group_rating,notes\n")  # header only
    assert "feedback row(s)" in import_file(store, path=cand, week="2026-W25", config=_cfg())

    bad = tmp_path / "bad.csv"
    bad.write_text("foo,bar\n1,2\n")
    with pytest.raises(ValueError):
        import_file(store, path=bad, week=None, config=_cfg())
    store.close()


def test_import_votes_is_idempotent(tmp_path):
    store = Store(tmp_path / "pw.db")
    _seed_all(store)
    path = _votes_csv(tmp_path / "gt.csv")
    import_votes(store, path=path, config=_cfg())
    weights_after_first = store.get_feedback_weights()

    res = import_votes(store, path=path, config=_cfg())
    assert res.imported == 0
    assert res.skipped_existing > 0
    assert store.get_feedback_weights() == weights_after_first
    store.close()


def test_import_votes_tie_no_pick_no_ledger(tmp_path):
    store = Store(tmp_path / "pw.db")
    a, b = _seed_all(store)
    path = tmp_path / "gt.csv"
    path.write_text(
        "week,message_ts,option,emoji,votes,url,context\n"
        f"2026-W27,{TS1},1,one,3,https://arxiv.org/abs/2605.01642,Winner\n"
        f"2026-W27,{TS1},2,two,3,https://arxiv.org/abs/2605.00002,Meh\n"
    )
    res = import_votes(store, path=path, config=_cfg())

    picks = [r["picked"] for r in store.conn.execute("SELECT picked FROM feedback")]
    assert picks == [0, 0]  # nobody picked
    assert store.conn.execute("SELECT COUNT(*) AS n FROM readings").fetchone()["n"] == 0
    assert res.ties == ["2026-W27"]
    # votes are still real signal: both rows imported, weights nudged
    assert res.imported == 2
    assert store.get_feedback_weights() != {}
    store.close()


def test_import_votes_winner_enters_ledger(tmp_path):
    store = Store(tmp_path / "pw.db")
    a, _ = _seed_all(store)
    res = import_votes(store, path=_votes_csv(tmp_path / "gt.csv"), config=_cfg())

    rows = store.conn.execute("SELECT * FROM readings ORDER BY id").fetchall()
    # one reading per non-tied poll (W27 and W28, same winner both times)
    assert [r["week"] for r in rows] == ["2026-W27", "2026-W28"]
    assert all(r["entry_id"] == a for r in rows)
    assert all(r["arxiv_id"] == "2605.01642" for r in rows)
    assert res.readings_recorded == 2
    store.close()


def test_import_votes_unresolved_winner_still_ledgered(tmp_path):
    store = Store(tmp_path / "pw.db")
    _seed_all(store)
    path = tmp_path / "gt.csv"
    path.write_text(
        "week,message_ts,option,emoji,votes,url,context\n"
        f"2026-W27,{TS1},1,one,5,https://unknown.example/x,A Distinctive Unknown Paper\n"
        f"2026-W27,{TS1},2,two,1,https://arxiv.org/abs/2605.00002,Meh\n"
    )
    res = import_votes(store, path=path, config=_cfg())

    (row,) = store.conn.execute("SELECT * FROM readings").fetchall()
    assert row["entry_id"] is None
    assert row["title_norm"] == "a distinctive unknown paper"
    assert "https://unknown.example/x" in res.unresolved_urls
    store.close()


def test_import_votes_backfills_resolution_on_next_run(tmp_path):
    store = Store(tmp_path / "pw.db")
    _seed_all(store)
    path = tmp_path / "gt.csv"
    path.write_text(
        "week,message_ts,option,emoji,votes,url,context\n"
        f"2026-W27,{TS1},1,one,5,https://arxiv.org/abs/2607.28607,Read Before Ingest\n"
        f"2026-W27,{TS1},2,two,1,https://arxiv.org/abs/2605.00002,Meh\n"
    )
    import_votes(store, path=path, config=_cfg())
    (row,) = store.conn.execute("SELECT * FROM readings").fetchall()
    assert row["entry_id"] is None

    # the paper finally arrives via a later ingest
    late = _seed(store, "Read Before Ingest", arxiv_id="2607.28607", authors=["Dan"])
    res = import_votes(store, path=path, config=_cfg())
    (row,) = store.conn.execute("SELECT * FROM readings").fetchall()
    assert row["entry_id"] == late
    assert res.resolutions_backfilled == 1
    store.close()


def test_import_votes_reports_weeks_and_weight_keys(tmp_path):
    store = Store(tmp_path / "pw.db")
    _seed_all(store)
    res = import_votes(store, path=_votes_csv(tmp_path / "gt.csv"), config=_cfg())
    assert res.weeks == ["2026-W27", "2026-W28"]
    assert res.weight_keys_touched == len(store.get_feedback_weights())
    assert res.weight_keys_touched > 0
    store.close()


def test_poll_attendance_proxy_top_plus_runner_third():
    assert poll_attendance([3, 1, 0]) == pytest.approx(3 + 1 / 3)
    assert poll_attendance([7, 5, 2]) == pytest.approx(7 + 5 / 3)
    assert poll_attendance([2]) == pytest.approx(2.0)
    assert poll_attendance([]) == 0.0


def test_votes_to_target_zero_is_none():
    assert votes_to_target(0, 5) is None
    assert votes_to_target(-1, 5) is None


def test_votes_to_target_nomination_base_anchors():
    # B(a) = 0.125 + 0.0375*a: +0.2 at attendance 2, +0.5 at attendance 10.
    assert votes_to_target(2, 2) == pytest.approx(0.2)
    assert votes_to_target(2, 10) == pytest.approx(0.5)
    # degenerate a<votes: a clamped up to 2, no division by zero
    assert votes_to_target(2, 1) == pytest.approx(0.2)


def test_votes_to_target_full_sweep_is_plus_one():
    assert votes_to_target(7, 7) == pytest.approx(1.0)
    # a := max(a, v) -- 10 votes clamp attendance up to 10, still a sweep
    assert votes_to_target(10, 4) == pytest.approx(1.0)


def test_votes_to_target_interpolates_between_base_and_sweep():
    # v=6, a=10: B(10) + (1 - B(10)) * (6-2)/(10-2) = 0.5 + 0.5*0.5
    assert votes_to_target(6, 10) == pytest.approx(0.75)


def test_votes_to_target_monotonic_in_votes():
    a = 8
    vals = [votes_to_target(v, a) for v in range(2, 9)]
    assert vals == sorted(set(vals)) and len(set(vals)) == len(vals)
    # a lone vote sits strictly below any nomination-with-support
    assert votes_to_target(1, a) < vals[0]


def test_votes_to_target_monotonic_in_attendance_for_v2():
    vals = [votes_to_target(2, a) for a in range(2, 13)]
    assert vals == sorted(set(vals)) and len(set(vals)) == len(vals)


def test_votes_to_target_lone_vote_unchanged():
    assert votes_to_target(1, 3) == pytest.approx(-0.5)
    assert votes_to_target(1, 4) == pytest.approx(-0.625)
    assert votes_to_target(1, 7) == pytest.approx(-1.0)
    assert votes_to_target(1, 20) == pytest.approx(-1.0)  # clamped


def test_votes_to_target_clamped_to_unit_interval():
    # huge attendance drives B(a) past 1 before the clamp; huge votes sweep
    for v, a in [(2, 100), (50, 50), (1, 100), (3, 1000)]:
        t = votes_to_target(v, a)
        assert -1.0 <= t <= 1.0


def test_score_scale_is_prediction_error_bounded():
    # neutral score 5 -> unchanged
    assert _score_scale(1.0, 5.0) == pytest.approx(1.0)
    assert _score_scale(-1.0, 5.0) == pytest.approx(-1.0)
    # high score: small boost, large penalty
    assert _score_scale(1.0, 10.0) == pytest.approx(0.0)
    assert _score_scale(-1.0, 10.0) == pytest.approx(-2.0)
    # low score: large boost, small penalty
    assert _score_scale(1.0, 0.0) == pytest.approx(2.0)
    assert _score_scale(-1.0, 0.0) == pytest.approx(0.0)
    # scores outside [0,10] are clamped
    assert _score_scale(1.0, 12.0) == pytest.approx(0.0)
    assert _score_scale(1.0, -3.0) == pytest.approx(2.0)


def test_import_votes_force_ts_reimports_a_hand_edited_poll(tmp_path):
    store = Store(tmp_path / "pw.db")
    a, b = _seed_all(store)
    path = _votes_csv(tmp_path / "gt.csv")
    import_votes(store, path=path, config=_cfg())

    # Hand-fix W27: "Meh" actually won with 6 votes, not 1.
    path.write_text(path.read_text().replace(
        f"2026-W27,{TS1},2,two,1,", f"2026-W27,{TS1},2,two,6,"
    ))
    res = import_votes(store, path=path, config=_cfg(), force_ts={TS1})

    fb = {(r["entry_id"], r["week"]): r for r in store.conn.execute(
        "SELECT entry_id, week, picked, notes FROM feedback"
    ).fetchall()}
    assert fb[(b, "2026-W27")]["picked"] == 1  # winner corrected
    assert fb[(a, "2026-W27")]["picked"] == 0
    assert "6/" in fb[(b, "2026-W27")]["notes"]
    readings = store.conn.execute(
        "SELECT url FROM readings WHERE message_ts = ?", (TS1,)
    ).fetchall()
    assert [r["url"] for r in readings] == ["https://arxiv.org/abs/2605.00002"]
    assert res.reimported == 2  # both resolvable W27 rows re-ran
    assert res.imported == 0  # W28 untouched
    store.close()


def test_import_votes_force_ts_of_a_deleted_poll_clears_its_ledger_row(tmp_path):
    store = Store(tmp_path / "pw.db")
    _seed_all(store)
    path = _votes_csv(tmp_path / "gt.csv")
    import_votes(store, path=path, config=_cfg())
    assert store.conn.execute(
        "SELECT COUNT(*) AS n FROM readings WHERE message_ts = ?", (TS1,)
    ).fetchone()["n"] == 1

    # Hand-prune the whole W27 poll (it was a misdetected non-poll).
    kept = [l for l in path.read_text().splitlines() if TS1 not in l]
    path.write_text("\n".join(kept) + "\n")
    import_votes(store, path=path, config=_cfg(), force_ts={TS1})
    assert store.conn.execute(
        "SELECT COUNT(*) AS n FROM readings WHERE message_ts = ?", (TS1,)
    ).fetchone()["n"] == 0
    store.close()


# -- tie resolution (resolve-ties) --------------------------------------------


def _tied_csv(path):
    path.write_text(
        "week,message_ts,option,emoji,votes,url,context\n"
        f"2026-W27,{TS1},1,one,3,https://arxiv.org/abs/2605.01642,Winner\n"
        f"2026-W27,{TS1},2,two,3,https://arxiv.org/abs/2605.00002,Meh\n"
        f"2026-W27,{TS1},3,three,1,https://arxiv.org/abs/2605.00003,ZeroVote\n"
    )
    return path


def test_outstanding_ties_lists_tied_options_and_skips_resolved(tmp_path):
    from paper_watch.feedback import outstanding_ties, resolve_tie

    store = Store(tmp_path / "pw.db")
    _seed_all(store)
    path = _tied_csv(tmp_path / "gt.csv")
    import_votes(store, path=path, config=_cfg())

    ties = outstanding_ties(store, path)
    assert len(ties) == 1
    tie = ties[0]
    assert (tie.week, tie.message_ts) == ("2026-W27", TS1)
    # Only the tied-top options are choices; the 1-vote option is not.
    assert [o.title for o in tie.options] == ["Winner", "Meh"]
    assert all(o.source == "arxiv.org" for o in tie.options)
    assert tie.options[0].authors == ["Alice"]

    resolve_tie(store, tie, 1, recorded_at="2026-08-08T00:00:00Z")
    assert outstanding_ties(store, path) == []
    store.close()


def test_resolve_tie_single_choice_marks_read_and_picked(tmp_path):
    from paper_watch.feedback import outstanding_ties, resolve_tie

    store = Store(tmp_path / "pw.db")
    a, b = _seed_all(store)
    path = _tied_csv(tmp_path / "gt.csv")
    import_votes(store, path=path, config=_cfg())

    tie = outstanding_ties(store, path)[0]
    n = resolve_tie(store, tie, 2, recorded_at="2026-08-08T00:00:00Z")
    assert n == 1
    readings = store.conn.execute(
        "SELECT url, entry_id FROM readings WHERE message_ts = ?", (TS1,)
    ).fetchall()
    assert [(r["url"], r["entry_id"]) for r in readings] == [
        ("https://arxiv.org/abs/2605.00002", b)
    ]
    picked = {r["entry_id"]: r["picked"] for r in store.conn.execute(
        "SELECT entry_id, picked FROM feedback WHERE week = '2026-W27'"
    )}
    assert picked[b] == 1 and picked[a] == 0
    store.close()


def test_resolve_tie_zero_marks_all_tied_options_read(tmp_path):
    from paper_watch.feedback import outstanding_ties, resolve_tie

    store = Store(tmp_path / "pw.db")
    _seed_all(store)
    path = _tied_csv(tmp_path / "gt.csv")
    import_votes(store, path=path, config=_cfg())

    tie = outstanding_ties(store, path)[0]
    n = resolve_tie(store, tie, 0, recorded_at="2026-08-08T00:00:00Z")
    assert n == 2
    urls = [r["url"] for r in store.conn.execute(
        "SELECT url FROM readings WHERE message_ts = ? ORDER BY url", (TS1,)
    )]
    assert urls == [
        "https://arxiv.org/abs/2605.00002", "https://arxiv.org/abs/2605.01642"
    ]
    # Nobody picked: "all/none/don't remember" is exclusion, not endorsement.
    assert all(r["picked"] == 0 for r in store.conn.execute(
        "SELECT picked FROM feedback"
    ))
    store.close()


def test_import_votes_omits_resolved_ties_from_result(tmp_path):
    from paper_watch.feedback import outstanding_ties, resolve_tie

    store = Store(tmp_path / "pw.db")
    _seed_all(store)
    path = _tied_csv(tmp_path / "gt.csv")
    import_votes(store, path=path, config=_cfg())
    tie = outstanding_ties(store, path)[0]
    resolve_tie(store, tie, 0, recorded_at="2026-08-08T00:00:00Z")

    res = import_votes(store, path=path, config=_cfg())
    assert res.ties == []
    store.close()


def _three_way_csv(path):
    path.write_text(
        "week,message_ts,option,emoji,votes,url,context\n"
        f"2026-W27,{TS1},1,one,3,https://arxiv.org/abs/2605.01642,Winner\n"
        f"2026-W27,{TS1},2,two,3,https://arxiv.org/abs/2605.00002,Meh\n"
        f"2026-W27,{TS1},3,three,3,https://arxiv.org/abs/2605.00003,ZeroVote\n"
    )
    return path


def _three_way_tie(store, tmp_path):
    path = _three_way_csv(tmp_path / "gt3.csv")
    import_votes(store, path=path, config=_cfg())
    from paper_watch.feedback import outstanding_ties

    return outstanding_ties(store, path)[0]


def _reading_urls(store):
    return [r["url"] for r in store.conn.execute(
        "SELECT url FROM readings WHERE message_ts = ? ORDER BY url", (TS1,)
    )]


def test_resolve_tie_list_marks_each_listed_option_read_and_none_picked(tmp_path):
    from paper_watch.feedback import resolve_tie

    store = Store(tmp_path / "pw.db")
    _seed_all(store)
    tie = _three_way_tie(store, tmp_path)

    n = resolve_tie(store, tie, [1, 3], recorded_at="2026-08-08T00:00:00Z")
    assert n == 2
    assert _reading_urls(store) == [
        "https://arxiv.org/abs/2605.00003", "https://arxiv.org/abs/2605.01642"
    ]
    assert all(r["picked"] == 0 for r in store.conn.execute("SELECT picked FROM feedback"))
    store.close()


def test_resolve_tie_single_element_list_behaves_like_the_bare_number(tmp_path):
    from paper_watch.feedback import resolve_tie

    store = Store(tmp_path / "pw.db")
    _, b = _seed_all(store)
    tie = _three_way_tie(store, tmp_path)

    n = resolve_tie(store, tie, [2], recorded_at="2026-08-08T00:00:00Z")
    assert n == 1
    assert _reading_urls(store) == ["https://arxiv.org/abs/2605.00002"]
    picked = {r["entry_id"]: r["picked"] for r in store.conn.execute(
        "SELECT entry_id, picked FROM feedback WHERE week = '2026-W27'"
    )}
    assert picked[b] == 1
    store.close()


def test_resolve_tie_full_list_matches_zero(tmp_path):
    from paper_watch.feedback import resolve_tie

    store = Store(tmp_path / "pw.db")
    _seed_all(store)
    tie = _three_way_tie(store, tmp_path)

    n = resolve_tie(store, tie, [1, 2, 3], recorded_at="2026-08-08T00:00:00Z")
    assert n == 3
    assert _reading_urls(store) == [
        "https://arxiv.org/abs/2605.00002",
        "https://arxiv.org/abs/2605.00003",
        "https://arxiv.org/abs/2605.01642",
    ]
    assert all(r["picked"] == 0 for r in store.conn.execute("SELECT picked FROM feedback"))
    store.close()


@pytest.mark.parametrize("choice", [[0, 2], [4], [-1]])
def test_resolve_tie_rejects_out_of_range_indices(tmp_path, choice):
    from paper_watch.feedback import resolve_tie

    store = Store(tmp_path / "pw.db")
    _seed_all(store)
    tie = _three_way_tie(store, tmp_path)

    with pytest.raises(ValueError):
        resolve_tie(store, tie, choice, recorded_at="2026-08-08T00:00:00Z")
    store.close()


def test_resolve_tie_rejects_duplicates(tmp_path):
    from paper_watch.feedback import resolve_tie

    store = Store(tmp_path / "pw.db")
    _seed_all(store)
    tie = _three_way_tie(store, tmp_path)

    with pytest.raises(ValueError):
        resolve_tie(store, tie, [1, 1], recorded_at="2026-08-08T00:00:00Z")
    store.close()


def test_resolve_tie_rejects_an_empty_list(tmp_path):
    from paper_watch.feedback import resolve_tie

    store = Store(tmp_path / "pw.db")
    _seed_all(store)
    tie = _three_way_tie(store, tmp_path)

    with pytest.raises(ValueError):
        resolve_tie(store, tie, [], recorded_at="2026-08-08T00:00:00Z")
    store.close()


def test_parse_tie_choice_reads_a_bare_number():
    from paper_watch.feedback import parse_tie_choice

    assert parse_tie_choice("2", 3) == 2
    assert parse_tie_choice(" 3 ", 3) == 3


def test_parse_tie_choice_reads_a_comma_list():
    from paper_watch.feedback import parse_tie_choice

    assert parse_tie_choice("1,3", 3) == [1, 3]
    assert parse_tie_choice(" 1 , 3 ", 3) == [1, 3]


def test_parse_tie_choice_reads_zero():
    from paper_watch.feedback import parse_tie_choice

    assert parse_tie_choice("0", 3) == 0


@pytest.mark.parametrize("raw", ["", "   ", "x", "1,", "1,x", "4", "1,1", "0,1", "-1"])
def test_parse_tie_choice_rejects_junk(raw):
    from paper_watch.feedback import parse_tie_choice

    with pytest.raises(ValueError):
        parse_tie_choice(raw, 3)
