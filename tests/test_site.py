import json

import pytest

from paper_watch.site import (
    group_by_digest,
    load_records,
    public_url,
    table_rows,
)
from paper_watch.store import Store

D1 = "2026-10-02T12:00:00Z"
D2 = "2026-10-09T12:00:00Z"

TITLE_A = "Scalable Oversight of Frontier Models"
TITLE_B = "Notes on deceptive alignment"
TITLE_C = "A talk nobody wrote down"
TITLE_D = "Never shown at all"

# Values public_url must reject. Kept here so the renderer tests can reuse them.
UNSAFE_URLS = [
    "javascript:alert(1)",
    " javascript:alert(1)",
    "JaVaScRiPt:alert(1)",
    "data:text/html,<b>x</b>",
    "slack://channel?team=T1&id=C1",
    "//evil.example/x",
    "http://",
    "https:///path",
    "not a url",
    "https://exa mple.org/",
    "https://example.org/\n",
    "",
]


@pytest.fixture
def site_store(tmp_path):
    """A store with three shown papers, one of them shown twice, and one that
    never appeared in a digest."""
    store = Store(tmp_path / "pw.db")

    a = store.insert_entry(
        title=TITLE_A,
        title_norm="scalable oversight of frontier models",
        first_seen_at="2026-09-30T00:00:00Z",
        arxiv_id="2410.00001",
        authors=["Ada Lovelace", "Alan Turing"],
        abstract="A long abstract that must never be published.",
        links={
            "abstract": "https://arxiv.org/abs/2410.00001",
            "pdf": "https://arxiv.org/pdf/2410.00001",
        },
        published_at="2024-10-01T00:00:00Z",
    )
    store.set_enrichment(
        a,
        tldr="One sentence about oversight.",
        why="Private reasoning that must never be published.",
        tags=["oversight", "evals"],
        relevance=9,
        version=2,
    )
    store.record_shown(entry_id=a, digest_at=D1, rank=1, score=1.5, resurfaced=False)
    store.record_shown(entry_id=a, digest_at=D2, rank=2, score=1.1, resurfaced=True)
    store.record_feedback(
        entry_id=a,
        week="2026-W40",
        picked=False,
        group_rating=2,
        notes=None,
        imported_at=D1,
    )
    store.record_feedback(
        entry_id=a,
        week="2026-W41",
        picked=False,
        group_rating=4,
        notes=None,
        imported_at=D2,
    )
    store.record_feedback(
        entry_id=a,
        week="2026-W42",
        picked=False,
        group_rating=None,
        notes=None,
        imported_at=D2,
    )

    b = store.insert_entry(
        title=TITLE_B,
        title_norm="notes on deceptive alignment",
        first_seen_at="2026-09-29T00:00:00Z",
        authors=["Blaise Pascal"],
        links={"abstract": "https://www.lesswrong.com/posts/abc123/slug"},
        published_at="2026-09-28T00:00:00Z",
    )
    store.add_entry_url(b, "https://www.alignmentforum.org/posts/abc123/slug")
    store.record_shown(entry_id=b, digest_at=D1, rank=2, score=1.2, resurfaced=False)
    store.record_feedback(
        entry_id=b,
        week="2026-W40",
        picked=True,
        group_rating=None,
        notes=None,
        imported_at=D1,
    )

    c = store.insert_entry(
        title=TITLE_C,
        title_norm="a talk nobody wrote down",
        first_seen_at="2026-10-08T00:00:00Z",
        authors=["Carl Friedrich Gauss"],
    )
    store.add_mention(
        entry_id=c,
        source="slack:alignment:papers",
        source_item_url="https://example.slack.com/archives/C1/p123",
        mention_text="worth a read",
        published_at="2026-08-15T00:00:00Z",
        fetched_at="2026-10-08T00:00:00Z",
    )
    store.record_shown(entry_id=c, digest_at=D2, rank=1, score=1.4, resurfaced=False)
    store.record_reading(
        week="2026-W41",
        message_ts="1700000000.000100",
        url="https://example.slack.com/archives/C1/p123",
        arxiv_id=None,
        title_norm="a talk nobody wrote down",
        entry_id=c,
        recorded_at=D2,
    )

    store.insert_entry(
        title=TITLE_D,
        title_norm="never shown at all",
        first_seen_at="2026-10-08T00:00:00Z",
        links={"abstract": "https://example.org/d"},
    )

    yield store
    store.close()


def _store_with_link(tmp_path, url):
    """A store with one shown entry whose only link is `url`."""
    store = Store(tmp_path / "link.db")
    entry_id = store.insert_entry(
        title="Only link",
        title_norm="only link",
        first_seen_at="2026-10-01T00:00:00Z",
        links={"abstract": url},
        published_at="2026-09-01T00:00:00Z",
    )
    store.record_shown(
        entry_id=entry_id, digest_at=D1, rank=1, score=1.0, resurfaced=False
    )
    return store


def _by_title(records, title):
    return [r for r in records if r.title == title]


def test_records_one_per_shown_row(site_store):
    records = load_records(site_store)
    assert len(records) == 4
    titles = [r.title for r in records]
    assert titles.count(TITLE_A) == 2
    assert TITLE_D not in titles


def test_record_field_mapping(site_store):
    record = _by_title(load_records(site_store), TITLE_A)[0]
    assert record.title == TITLE_A
    assert record.authors == ["Ada Lovelace", "Alan Turing"]
    assert record.arxiv_id == "2410.00001"
    assert record.tags == ["oversight", "evals"]
    assert record.tldr == "One sentence about oversight."
    assert record.published == "2024-10"
    assert record.is_estimate is False
    assert record.url == "https://arxiv.org/abs/2410.00001"


def test_record_url_prefers_alignmentforum(site_store):
    record = _by_title(load_records(site_store), TITLE_B)[0]
    assert record.url == "https://www.alignmentforum.org/posts/abc123/slug"


def test_record_url_falls_back_to_mention(site_store):
    record = _by_title(load_records(site_store), TITLE_C)[0]
    assert record.url == "https://example.slack.com/archives/C1/p123"
    assert record.is_estimate is True
    assert record.published == "2026-08"


def test_latest_week_rating_wins(site_store):
    records = _by_title(load_records(site_store), TITLE_A)
    assert [r.group_rating for r in records] == [4, 4]


def test_picked_and_read_flags(site_store):
    records = load_records(site_store)
    b = _by_title(records, TITLE_B)[0]
    assert b.picked is True
    assert b.read is False
    assert b.group_rating is None
    c = _by_title(records, TITLE_C)[0]
    assert c.read is True


def test_resurfaced_flag_per_digest(site_store):
    records = _by_title(load_records(site_store), TITLE_A)
    by_digest = {r.digest_at: r.resurfaced for r in records}
    assert by_digest[D2] is True
    assert by_digest[D1] is False


def test_group_by_digest_newest_first_rank_order(site_store):
    groups = group_by_digest(load_records(site_store))
    assert [digest_at for digest_at, _ in groups] == [D2, D1]
    assert [r.title for r in groups[0][1]] == [TITLE_C, TITLE_A]
    assert [r.title for r in groups[1][1]] == [TITLE_A, TITLE_B]


def test_table_rows_once_per_paper_under_first_digest(site_store):
    rows = table_rows(load_records(site_store))
    assert [row.record.title for row in rows] == [TITLE_C, TITLE_A, TITLE_B]
    row_a = [row for row in rows if row.record.title == TITLE_A][0]
    assert row_a.first_digest_at == D1
    assert row_a.record.rank == 1
    assert row_a.times_shown == 2


def test_record_json_excludes_private_fields(site_store):
    record = _by_title(load_records(site_store), TITLE_A)[0]
    payload = record.to_json()
    assert set(payload) == {
        "digest_at",
        "rank",
        "resurfaced",
        "title",
        "authors",
        "url",
        "arxiv_id",
        "published",
        "is_estimate",
        "tags",
        "tldr",
        "group_rating",
        "picked",
        "read",
    }
    for private in ("score", "why", "abstract", "explanation", "sources", "entry_id"):
        assert private not in payload
    assert json.loads(json.dumps(payload))["title"] == TITLE_A


@pytest.mark.parametrize(
    "url",
    ["https://arxiv.org/abs/1", "http://example.org/x", "HTTPS://Example.org/x"],
)
def test_public_url_accepts_http_and_https(url):
    assert public_url(url) == url


@pytest.mark.parametrize("url", UNSAFE_URLS)
def test_public_url_rejects_unsafe_and_malformed(url):
    assert public_url(url) is None


def test_public_url_rejects_none():
    assert public_url(None) is None


@pytest.mark.parametrize("url", UNSAFE_URLS)
def test_record_with_unsafe_link_has_no_url(tmp_path, url):
    store = _store_with_link(tmp_path, url)
    try:
        records = load_records(store)
        assert len(records) == 1
        assert records[0].url is None
    finally:
        store.close()


def test_slack_app_mention_fallback_has_no_url(tmp_path):
    store = Store(tmp_path / "pw.db")
    try:
        entry_id = store.insert_entry(
            title="App link only",
            title_norm="app link only",
            first_seen_at="2026-10-01T00:00:00Z",
        )
        store.add_mention(
            entry_id=entry_id,
            source="slack:alignment:papers",
            source_item_url="slack://channel?team=T1&id=C1",
            fetched_at="2026-10-01T00:00:00Z",
            published_at="2026-09-01T00:00:00Z",
        )
        store.record_shown(
            entry_id=entry_id, digest_at=D1, rank=1, score=1.0, resurfaced=False
        )
        records = load_records(store)
        assert len(records) == 1
        assert records[0].url is None
    finally:
        store.close()
