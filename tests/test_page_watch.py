import json

from paper_watch.config import PageConfig
from paper_watch.normalize import to_entry_fields
from paper_watch.sources.page_watch import PageWatchSource, extract_post_links

BASE = "https://alignment.example.com/"

NEW_POST = """
    <a href="2026/new-post/">
      <h3>A Brand New Post</h3>
      <p>We announce something new.</p>
    </a>
"""


class FakeState:
    def __init__(self):
        self.cursors: dict[str, str] = {}

    def get_cursor(self, source):
        return self.cursors.get(source)

    def set_cursor(self, source, cursor):
        self.cursors[source] = cursor


def _source(state, pages=None, html_by_url=None, trusted=False):
    pages = pages or [PageConfig(name="Test Blog", url=BASE, trusted=trusted)]
    return PageWatchSource(pages, state, fetch=lambda url: html_by_url[url])


def test_extract_post_links_resolves_filters_and_dedupes(fixture_text):
    links = dict(extract_post_links(fixture_text("page_index.html"), BASE))
    # relative hrefs resolve against the page; off-site post links are kept
    assert "https://alignment.example.com/2026/modular-pretraining/" in links
    assert "https://arxiv.org/abs/2506.18032" in links
    # anchor text is the whitespace-normalized title + blurb
    assert links["https://alignment.example.com/2025/subliminal-learning/index.html"] == (
        "Subliminal Learning Language models transmit behavioral traits via hidden signals."
    )
    # the page itself, fragment-only, icon (empty text), and mailto links drop
    assert not any(u.startswith(BASE.rstrip("/")) and u.rstrip("/") == BASE.rstrip("/") for u in links)
    assert not any(u.startswith("mailto:") for u in links)
    assert len(links) == 4  # 3 posts + the nav link to another site


def test_first_fetch_seeds_baseline_and_yields_nothing(fixture_text):
    state = FakeState()
    src = _source(state, html_by_url={BASE: fixture_text("page_index.html")})

    assert list(src.fetch()) == []
    seen = json.loads(state.cursors[f"page:{BASE}"])
    # the baseline stores diff keys: trailing slashes are stripped
    assert "https://alignment.example.com/2026/modular-pretraining" in seen


def test_new_link_on_next_fetch_is_yielded(fixture_text):
    state = FakeState()
    html = fixture_text("page_index.html")
    pages = {BASE: html}
    src = _source(state, html_by_url=pages, trusted=True)
    list(src.fetch())  # seed

    pages[BASE] = html.replace('<div id="posts">', '<div id="posts">' + NEW_POST)
    items = list(src.fetch())

    assert len(items) == 1
    item = items[0]
    assert item.source == "page:Test Blog"
    assert item.url == "https://alignment.example.com/2026/new-post/"
    assert item.text == "A Brand New Post We announce something new."
    assert item.trusted is True
    # normalize promotes the anchor text to the entry title
    assert to_entry_fields(item)["title"] == "A Brand New Post We announce something new."
    # ...and the same link doesn't re-trigger on the run after
    assert list(src.fetch()) == []


def test_arxiv_link_post_adopts_the_arxiv_id(fixture_text):
    state = FakeState()
    html = fixture_text("page_index.html").replace(
        "2506.18032", "9999.00001"
    )  # pretend the arXiv post is the new one
    src = _source(state, html_by_url={BASE: fixture_text("page_index.html")})
    list(src.fetch())  # seed with the original
    src._fetch = lambda url: html
    items = list(src.fetch())
    assert [to_entry_fields(i)["arxiv_id"] for i in items] == ["9999.00001"]


def test_trailing_slash_mutation_does_not_retrigger_the_whole_page(fixture_text):
    # 2026-08-06: Apollo's site republished with hrefs that dropped their
    # trailing slashes, and every nav link flooded the digest as "new".
    state = FakeState()
    html = fixture_text("page_index.html")
    pages = {BASE: html}
    src = _source(state, html_by_url=pages)
    list(src.fetch())  # seed with slashed forms

    pages[BASE] = html.replace('href="2026/modular-pretraining/"', 'href="2026/modular-pretraining"')
    assert list(src.fetch()) == []

    # ...and the cursor self-migrates, so the slashed form doesn't re-trigger later
    pages[BASE] = html
    assert list(src.fetch()) == []


def test_slash_variants_of_one_link_collapse_within_a_page():
    html = (
        '<a href="/posts/one/">First</a>'
        '<a href="/posts/one">First again</a>'
        '<a href="/posts/two">Second</a>'
    )
    links = extract_post_links(html, BASE)
    assert [u for u, _ in links] == [
        "https://alignment.example.com/posts/one/",
        "https://alignment.example.com/posts/two",
    ]


def test_pagination_links_to_the_page_itself_are_dropped():
    # Webflow indexes link "?457b778d_page=2" back to themselves; that is the
    # index, not a post.
    html = '<a href="?457b778d_page=2">2</a><a href="/posts/real">Real Post</a>'
    links = extract_post_links(html, BASE)
    assert [u for u, _ in links] == ["https://alignment.example.com/posts/real"]


def test_removed_links_stay_seen_and_never_retrigger(fixture_text):
    state = FakeState()
    html = fixture_text("page_index.html")
    pages = {BASE: html}
    src = _source(state, html_by_url=pages)
    list(src.fetch())  # seed

    # a post falls off the index page, then reappears later
    pages[BASE] = html.replace('href="2025/subliminal-learning/index.html"', 'href="x/"')
    list(src.fetch())
    pages[BASE] = html
    assert list(src.fetch()) == []


def test_failing_or_empty_page_skips_without_touching_state(fixture_text):
    state = FakeState()
    good = PageConfig(name="Good", url=BASE)
    down = PageConfig(name="Down", url="https://down.example.com/")
    blank = PageConfig(name="Blank", url="https://blank.example.com/")

    def fetch(url):
        if url == BASE:
            return fixture_text("page_index.html")
        if url == blank.url:
            return "<html><body>no links here</body></html>"
        raise RuntimeError("connection refused")

    src = PageWatchSource([down, blank, good], state, fetch=fetch)
    assert list(src.fetch()) == []  # good page seeds; others skip
    assert set(state.cursors) == {f"page:{BASE}"}


class HealthState(FakeState):
    """FakeState that also records source health, like the real Store."""

    def __init__(self):
        super().__init__()
        self.ok: list[tuple[str, str]] = []
        self.failures: list[tuple[str, str, str]] = []

    def record_source_ok(self, source, *, label, at):
        self.ok.append((source, label))

    def record_source_failure(self, source, *, label, error, at):
        self.failures.append((source, label, error))


def _boom(url):
    raise RuntimeError("404 Not Found")


def test_a_failing_page_is_recorded_as_unhealthy():
    state = HealthState()
    source = PageWatchSource(
        [PageConfig(name="Dead Blog", url=BASE, trusted=False)], state, fetch=_boom
    )
    assert list(source.fetch()) == []

    # A dead page yields nothing, which is indistinguishable from a quiet one
    # unless the failure is recorded.
    assert state.failures == [(f"page:{BASE}", "Dead Blog", "404 Not Found")]
    assert state.ok == []


def test_a_working_page_is_recorded_as_healthy(fixture_text):
    state = HealthState()
    source = _source(state, html_by_url={BASE: fixture_text("page_index.html")})
    list(source.fetch())
    assert state.ok == [(f"page:{BASE}", "Test Blog")]
    assert state.failures == []


def test_a_page_that_parses_to_no_links_counts_as_a_failure():
    state = HealthState()
    source = _source(state, html_by_url={BASE: "<html><body></body></html>"})
    assert list(source.fetch()) == []
    # An index with no links is an outage, not a site that deleted everything.
    assert len(state.failures) == 1
    assert "no links" in state.failures[0][2]
    assert state.ok == []


def test_health_recording_is_optional_for_plain_cursor_state(fixture_text):
    # The Slack/eval paths pass a bare cursor store; health is a bonus, not a
    # requirement, so a state without the methods must not break ingestion.
    state = FakeState()
    source = _source(state, html_by_url={BASE: fixture_text("page_index.html")})
    list(source.fetch())  # must not raise


def test_a_multi_line_fetch_error_is_reduced_to_its_first_line():
    def raise_httpx_style(url):
        raise RuntimeError(
            "Client error '404 Not Found' for url 'https://x.example/blog'\n"
            "For more information check: https://developer.mozilla.org/en-US/docs/"
            "Web/HTTP/Status/404"
        )

    state = HealthState()
    source = PageWatchSource(
        [PageConfig(name="X", url=BASE, trusted=False)], state, fetch=raise_httpx_style
    )
    list(source.fetch())
    # The MDN boilerplate would swamp a one-line warning in the email.
    assert state.failures[0][2] == (
        "Client error '404 Not Found' for url 'https://x.example/blog'"
    )
