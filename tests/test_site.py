import json

import pytest
from markupsafe import escape

from paper_watch.site import (
    MANAGED_FILES,
    RATINGS_NOTE,
    SITE_DESCRIPTION,
    SITE_TITLE,
    TLDR_LABEL,
    build_site,
    group_by_digest,
    latest_digest_at,
    load_records,
    public_url,
    render_all,
    render_index,
    render_json,
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


# --- renderers -----------------------------------------------------------


def _render_all_pages(store):
    records = load_records(store)
    latest = latest_digest_at(records)
    return (
        render_index(group_by_digest(records), latest_digest_at=latest),
        render_all(table_rows(records), latest_digest_at=latest),
        render_json(group_by_digest(records), latest_digest_at=latest),
    )


def _row_for(html, title):
    """The `<tr>` of `all.html` that holds `title`."""
    start = html.rindex("<tr", 0, html.index(title))
    return html[start : html.index("</tr>", start)]


def test_index_renders_every_title(site_store):
    html = _render_all_pages(site_store)[0]
    assert TITLE_A in html
    assert TITLE_B in html
    assert TITLE_C in html
    assert TITLE_D not in html


def test_index_orders_digests_newest_first_and_entries_by_rank(site_store):
    html = _render_all_pages(site_store)[0]
    assert html.index("2026-10-09") < html.index("2026-10-02")
    # D2 holds C at rank 1 and A at rank 2, so A's first appearance follows C.
    assert html.index(TITLE_C) < html.index(TITLE_A)
    # D1 holds A at rank 1 and B at rank 2, so A's last appearance precedes B.
    assert html.rindex(TITLE_A) < html.index(TITLE_B)


def test_index_marks_estimates_and_ratings(site_store):
    html = _render_all_pages(site_store)[0]
    assert "~2026-08" in html
    assert "group rating 4/5" in html
    assert "picked" in html
    assert "read" in html
    # Only A's second appearance is resurfaced.
    assert html.count("resurfaced") == 1
    assert html.index("resurfaced") < html.rindex(TITLE_A)


def test_index_tldr_under_ai_summary_label(site_store):
    html = _render_all_pages(site_store)[0]
    assert TLDR_LABEL == "AI summary"
    start = html.index("<details")
    block = html[start : html.index("</details>", start)]
    assert f"<summary>{TLDR_LABEL}</summary>" in block
    assert "One sentence about oversight." in block


def test_index_footer_links_and_ratings_note(site_store):
    html = _render_all_pages(site_store)[0]
    assert 'href="all.html"' in html
    assert 'href="papers.json"' in html
    # The note carries an apostrophe, which autoescaping turns into an entity.
    assert escape(RATINGS_NOTE) in html
    assert "Latest digest: 2026-10-09" in html


def test_index_shows_placeholder_title_and_description(site_store):
    html = _render_all_pages(site_store)[0]
    assert SITE_TITLE in html
    assert escape(SITE_DESCRIPTION) in html


def test_all_renders_one_row_per_paper(site_store):
    html = _render_all_pages(site_store)[1]
    for title in (TITLE_A, TITLE_B, TITLE_C):
        assert html.count(title) == 1
    assert TITLE_D not in html
    assert ">2<" in _row_for(html, TITLE_A)
    assert "Latest digest: 2026-10-09" in html


def test_all_has_filter_box_and_no_network(site_store):
    html = _render_all_pages(site_store)[1]
    assert "<input" in html
    scripts = html.split("<script")[1:]
    assert scripts
    for script in scripts:
        assert "src=" not in script[: script.index(">")]
    assert "fetch(" not in html
    assert "XMLHttpRequest" not in html


def test_json_shape(site_store):
    payload = json.loads(_render_all_pages(site_store)[2])
    assert set(payload) == {"latest_digest_at", "digests"}
    assert payload["latest_digest_at"] == D2
    assert [d["digest_at"] for d in payload["digests"]] == [D2, D1]
    for digest in payload["digests"]:
        assert set(digest) == {"digest_at", "entries"}
        for entry in digest["entries"]:
            assert set(entry) == {
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


def test_titles_are_escaped(tmp_path):
    raw = "Attacks on <script>alert(1)</script> models"
    store = Store(tmp_path / "pw.db")
    try:
        entry_id = store.insert_entry(
            title=raw,
            title_norm="attacks",
            first_seen_at="2026-10-01T00:00:00Z",
            links={"abstract": "https://example.org/x"},
            published_at="2026-09-01T00:00:00Z",
        )
        store.record_shown(
            entry_id=entry_id, digest_at=D1, rank=1, score=1.0, resurfaced=False
        )
        index_html, all_html, payload = _render_all_pages(store)
        for html in (index_html, all_html):
            assert "&lt;script&gt;" in html
            assert "<script>alert(1)</script>" not in html
        assert json.loads(payload)["digests"][0]["entries"][0]["title"] == raw
    finally:
        store.close()


@pytest.mark.parametrize("url", UNSAFE_URLS)
def test_unsafe_url_renders_title_unlinked(tmp_path, url):
    store = _store_with_link(tmp_path, url)
    try:
        index_html, all_html, payload = _render_all_pages(store)
        escaped = escape(url)
        for html in (index_html, all_html):
            assert "Only link" in html
            assert f'href="{url}"' not in html
            assert f'href="{escaped}"' not in html
            assert ">Only link</a>" not in html
        assert json.loads(payload)["digests"][0]["entries"][0]["url"] is None
    finally:
        store.close()


def test_build_site_is_deterministic(site_store):
    first = build_site(site_store, domain="papers.cupchinski.com")
    second = build_site(site_store, domain="papers.cupchinski.com")
    assert first == second


def test_build_site_returns_managed_files(site_store):
    files = build_site(site_store, domain="papers.cupchinski.com")
    assert set(files) == set(MANAGED_FILES)
    assert set(MANAGED_FILES) == {
        "index.html",
        "all.html",
        "papers.json",
        "style.css",
        "CNAME",
        "README.md",
    }
    assert files["CNAME"] == "papers.cupchinski.com\n"


def test_cname_uses_configured_domain(site_store):
    files = build_site(site_store, domain="example.org")
    assert files["CNAME"] == "example.org\n"
    for name, content in files.items():
        assert "papers.cupchinski.com" not in content, name


def test_empty_store_renders(tmp_path):
    store = Store(tmp_path / "empty.db")
    try:
        files = build_site(store, domain="example.org")
        assert json.loads(files["papers.json"]) == {
            "latest_digest_at": None,
            "digests": [],
        }
        for name in ("index.html", "all.html"):
            assert "Latest digest" not in files[name]
    finally:
        store.close()
