import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from markupsafe import escape

from paper_watch import site as site_mod
from paper_watch.config import SiteConfig
from paper_watch.site import (
    GIT_TIMEOUT_SECONDS,
    MANAGED_FILES,
    RATINGS_NOTE,
    SITE_DESCRIPTION,
    SITE_TITLE,
    TLDR_LABEL,
    SitePublishError,
    _git_env,
    build_site,
    group_by_digest,
    latest_digest_at,
    load_records,
    public_url,
    publish,
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


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------

NOW = datetime(2026, 10, 10, 9, 0, 0, tzinfo=timezone.utc)
STAMP = "2026-10-10T09:00:00Z"


def _git(repo, *args):
    """Run git in `repo` for the test's own setup and assertions."""
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=dict(os.environ),
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def site_repo(tmp_path, monkeypatch):
    """A bare `origin.git` and a clone of it on an unborn `main` branch.

    The git configuration is isolated so that the developer's own settings
    (signing, hooks, default branch, identity) cannot change the result.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("SITE_DEPLOY_KEY", raising=False)

    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "--quiet", str(origin)],
        check=True,
        env=dict(os.environ),
    )
    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "clone", "--quiet", str(origin), str(checkout)],
        check=True,
        capture_output=True,
        env=dict(os.environ),
    )
    _git(checkout, "symbolic-ref", "HEAD", "refs/heads/main")

    cfg = SiteConfig(
        repo_path=str(checkout),
        committer_name="Test Bot",
        committer_email="bot@example.org",
    )
    return cfg, origin


def _commit_count(repo, ref="HEAD"):
    proc = subprocess.run(
        ["git", "-C", str(repo), "rev-list", "--count", ref],
        capture_output=True,
        text=True,
        env=dict(os.environ),
    )
    if proc.returncode != 0:
        return 0
    return int(proc.stdout.strip())


def test_first_publish_commits_and_pushes(site_store, site_repo):
    cfg, origin = site_repo
    result = publish(site_store, cfg, now=NOW)
    assert result.committed is True
    assert result.pushed is True
    assert result.changed is True
    assert result.message == f"site: 2 digests, 3 papers ({STAMP})"
    assert _git(origin, "log", "-1", "--format=%s", "main") == result.message
    assert _commit_count(origin, "main") == 1
    tree = _git(origin, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert sorted(tree) == sorted(MANAGED_FILES)


def test_commit_uses_configured_identity(site_store, site_repo):
    cfg, origin = site_repo
    publish(site_store, cfg, now=NOW)
    identity = _git(origin, "log", "-1", "--format=%an <%ae>|%cn <%ce>", "main")
    assert identity == "Test Bot <bot@example.org>|Test Bot <bot@example.org>"


def test_committer_email_falls_back_to_repo_git_config(site_store, site_repo):
    cfg, origin = site_repo
    cfg = cfg.model_copy(update={"committer_email": None})
    _git(cfg.repo_path, "config", "user.email", "repo@example.org")
    publish(site_store, cfg, now=NOW)
    assert _git(origin, "log", "-1", "--format=%ae", "main") == "repo@example.org"


def test_missing_committer_email_is_a_clear_error(site_store, site_repo):
    cfg, _origin = site_repo
    cfg = cfg.model_copy(update={"committer_email": None})
    with pytest.raises(SitePublishError) as excinfo:
        publish(site_store, cfg, now=NOW)
    assert "committer_email" in str(excinfo.value)
    assert "user.email" in str(excinfo.value)
    assert not (Path(cfg.repo_path) / "index.html").exists()
    assert _commit_count(cfg.repo_path) == 0


def test_second_publish_unchanged_makes_no_commit(site_store, site_repo):
    cfg, origin = site_repo
    publish(site_store, cfg, now=NOW)
    before = _commit_count(origin, "main")
    result = publish(site_store, cfg, now=NOW)
    assert result.changed is False
    assert result.committed is False
    assert result.pushed is False
    assert _commit_count(origin, "main") == before


def test_publish_after_new_digest_commits_again(site_store, site_repo):
    cfg, origin = site_repo
    publish(site_store, cfg, now=NOW)
    entry_id = site_store.insert_entry(
        title="A newly suggested paper",
        title_norm="a newly suggested paper",
        first_seen_at="2026-10-15T00:00:00Z",
        links={"abstract": "https://arxiv.org/abs/2410.09999"},
        published_at="2026-10-14T00:00:00Z",
    )
    site_store.record_shown(
        entry_id=entry_id,
        digest_at="2026-10-16T12:00:00Z",
        rank=1,
        score=1.0,
        resurfaced=False,
    )
    result = publish(site_store, cfg, now=NOW)
    assert result.committed is True
    assert _commit_count(origin, "main") == 2


def _add_another_digest(store):
    entry_id = store.insert_entry(
        title="Another suggested paper",
        title_norm="another suggested paper",
        first_seen_at="2026-10-15T00:00:00Z",
        links={"abstract": "https://arxiv.org/abs/2410.08888"},
        published_at="2026-10-14T00:00:00Z",
    )
    store.record_shown(
        entry_id=entry_id,
        digest_at="2026-10-16T12:00:00Z",
        rank=1,
        score=1.0,
        resurfaced=False,
    )


def test_push_failure_raises_and_keeps_local_commit(site_store, site_repo, tmp_path):
    cfg, origin = site_repo
    publish(site_store, cfg, now=NOW)
    origin.rename(tmp_path / "origin-moved.git")
    _add_another_digest(site_store)
    with pytest.raises(SitePublishError):
        publish(site_store, cfg, now=NOW)
    assert _commit_count(cfg.repo_path) == 2


def test_unpushed_commit_is_pushed_on_next_publish(site_store, site_repo, tmp_path):
    cfg, origin = site_repo
    publish(site_store, cfg, now=NOW)
    origin.rename(tmp_path / "origin-moved.git")
    _add_another_digest(site_store)
    with pytest.raises(SitePublishError):
        publish(site_store, cfg, now=NOW)
    (tmp_path / "origin-moved.git").rename(origin)

    result = publish(site_store, cfg, now=NOW)
    assert result.committed is False
    assert result.pushed is True
    assert _commit_count(origin, "main") == 2


def test_dry_run_leaves_repo_untouched(site_store, site_repo):
    cfg, _origin = site_repo
    result = publish(site_store, cfg, now=NOW, dry_run=True)
    assert result.changed is True
    assert sorted(result.files) == sorted(MANAGED_FILES)
    assert result.committed is False
    assert result.pushed is False
    assert _git(cfg.repo_path, "status", "--porcelain") == ""
    assert _commit_count(cfg.repo_path) == 0


def test_publish_writes_configured_domain(site_store, site_repo):
    cfg, origin = site_repo
    cfg = cfg.model_copy(update={"domain": "example.org"})
    publish(site_store, cfg, now=NOW)
    assert _git(origin, "show", "main:CNAME") == "example.org"


def test_precondition_missing_repo_path(site_store, site_repo, tmp_path):
    cfg, _origin = site_repo
    missing = tmp_path / "nowhere"
    cfg = cfg.model_copy(update={"repo_path": str(missing)})
    with pytest.raises(SitePublishError) as excinfo:
        publish(site_store, cfg, now=NOW)
    assert str(missing) in str(excinfo.value)


def test_precondition_not_a_git_checkout(site_store, site_repo, tmp_path):
    cfg, _origin = site_repo
    plain = tmp_path / "plain"
    plain.mkdir()
    cfg = cfg.model_copy(update={"repo_path": str(plain)})
    with pytest.raises(SitePublishError) as excinfo:
        publish(site_store, cfg, now=NOW)
    assert "not a git checkout" in str(excinfo.value)
    assert list(plain.iterdir()) == []


def test_precondition_wrong_branch(site_store, site_repo):
    cfg, _origin = site_repo
    _git(cfg.repo_path, "symbolic-ref", "HEAD", "refs/heads/other")
    with pytest.raises(SitePublishError) as excinfo:
        publish(site_store, cfg, now=NOW)
    assert "other" in str(excinfo.value)
    assert "main" in str(excinfo.value)


def test_precondition_foreign_dirty_file(site_store, site_repo):
    cfg, _origin = site_repo
    (Path(cfg.repo_path) / "notes.txt").write_text("mine\n", encoding="utf-8")
    with pytest.raises(SitePublishError) as excinfo:
        publish(site_store, cfg, now=NOW)
    assert "notes.txt" in str(excinfo.value)
    assert not (Path(cfg.repo_path) / "index.html").exists()
    assert _commit_count(cfg.repo_path) == 0


def test_dirty_managed_file_is_allowed(site_store, site_repo):
    cfg, _origin = site_repo
    index = Path(cfg.repo_path) / "index.html"
    index.write_text("hand written\n", encoding="utf-8")
    result = publish(site_store, cfg, now=NOW)
    assert result.committed is True
    assert "hand written" not in index.read_text(encoding="utf-8")


def test_git_env_without_key_is_batch_mode():
    for environ in ({}, {"SITE_DEPLOY_KEY": ""}):
        env = _git_env(environ)
        assert env["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"
        assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_git_env_with_key_adds_identity():
    env = _git_env({"SITE_DEPLOY_KEY": "/k/my key"})
    assert (
        env["GIT_SSH_COMMAND"]
        == "ssh -o BatchMode=yes -i '/k/my key' -o IdentitiesOnly=yes"
    )


def test_git_env_overrides_ambient_ssh_command():
    env = _git_env({"GIT_SSH_COMMAND": "ssh -v"})
    assert env["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"


def test_git_timeout_is_a_publish_error(tmp_path, monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(kwargs)
        raise subprocess.TimeoutExpired(cmd, GIT_TIMEOUT_SECONDS)

    monkeypatch.setattr(site_mod.subprocess, "run", fake_run)
    with pytest.raises(SitePublishError) as excinfo:
        site_mod._git(tmp_path, "status", "--porcelain", env={})
    assert "status" in str(excinfo.value)
    assert "120" in str(excinfo.value)
    assert GIT_TIMEOUT_SECONDS == 120
    assert calls[0]["timeout"] == GIT_TIMEOUT_SECONDS


def test_git_error_message_is_first_stderr_line(site_repo):
    cfg, _origin = site_repo
    with pytest.raises(SitePublishError) as excinfo:
        site_mod._git(
            Path(cfg.repo_path),
            "push",
            "origin",
            "no-such-ref",
            env=_git_env(dict(os.environ)),
        )
    lines = [line.strip() for line in excinfo.value.stderr.strip().splitlines()]
    assert len(lines) > 1
    assert lines[0] in str(excinfo.value)
    for line in lines[1:]:
        assert line not in str(excinfo.value)


def test_missing_deploy_key_file_is_a_clear_error(site_store, site_repo, tmp_path):
    cfg, _origin = site_repo
    missing_key = tmp_path / "no-such-key"
    with pytest.raises(SitePublishError) as excinfo:
        publish(
            site_store,
            cfg,
            now=NOW,
            environ={**os.environ, "SITE_DEPLOY_KEY": str(missing_key)},
        )
    assert "SITE_DEPLOY_KEY" in str(excinfo.value)
    assert str(missing_key) not in str(excinfo.value)
    assert not (Path(cfg.repo_path) / "index.html").exists()
    assert _commit_count(cfg.repo_path) == 0
