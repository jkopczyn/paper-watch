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
    assert "https://alignment.example.com/2026/modular-pretraining/" in seen


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
