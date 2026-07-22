"""Page-1 metadata from an HTML landing page. Uses stdlib html.parser (as the
rest of the codebase does), so the tests need no network and no new dependency."""

from paper_watch.sources.html_meta import (
    HtmlMetaResolver,
    parse_html_meta,
)


def test_prefers_og_title_over_document_title():
    html = """
    <html><head>
      <title>Constitutional Classifiers | Anthropic</title>
      <meta property="og:title" content="Constitutional Classifiers: Defending against Jailbreaks">
      <meta property="og:description" content="We train classifiers that guard an LLM.">
    </head><body>ignored</body></html>
    """
    meta = parse_html_meta(html)
    assert meta["title"] == "Constitutional Classifiers: Defending against Jailbreaks"
    assert meta["abstract"] == "We train classifiers that guard an LLM."


def test_falls_back_to_title_tag_and_strips_the_site_suffix():
    html = "<head><title>Selective Gradient Masking — Anthropic Alignment</title></head>"
    meta = parse_html_meta(html)
    assert meta["title"] == "Selective Gradient Masking"
    assert meta["abstract"] is None


def test_twitter_title_used_when_no_og_title():
    html = """<head>
      <title>Home</title>
      <meta name="twitter:title" content="Scaling Monosemanticity">
    </head>"""
    assert parse_html_meta(html)["title"] == "Scaling Monosemanticity"


def test_meta_description_used_when_no_og_description():
    html = """<head>
      <meta property="og:title" content="Gradient Routing">
      <meta name="description" content="Localizing computation to a subnetwork.">
    </head>"""
    meta = parse_html_meta(html)
    assert meta["abstract"] == "Localizing computation to a subnetwork."


def test_no_usable_title_returns_none():
    assert parse_html_meta("<head><title></title></head>") is None
    assert parse_html_meta("<head></head>") is None
    # a bare site name is not a paper title
    assert parse_html_meta("<head><title>Home</title></head>") is None


def test_a_url_as_the_og_title_is_rejected():
    # archive.is and some CDNs echo the source URL into og:title; a URL is never
    # a title, and accepting it just swaps one junk title for another.
    html = (
        '<head><meta property="og:title" '
        'content="https://cdn.openai.com/pdf/June-2026-Threat-Report.pdf"></head>'
    )
    assert parse_html_meta(html) is None


def test_body_is_not_read_as_metadata():
    # Only the head is metadata; an <h1> or stray text in the body must not leak
    # into the title, and there is no title in the head here.
    html = "<head></head><body><h1>Some Section Heading</h1><title>late</title></body>"
    assert parse_html_meta(html) is None


def test_resolver_fetches_and_parses():
    page = '<head><meta property="og:title" content="Off-Switch for Dual-Use Knowledge"></head>'
    r = HtmlMetaResolver(fetch=lambda _u: page)
    meta = r.resolve("https://www.anthropic.com/research/off-switch-dual-use")
    assert meta["title"] == "Off-Switch for Dual-Use Knowledge"


def test_resolver_fetch_error_is_none():
    def boom(_u):
        raise RuntimeError("down")

    assert HtmlMetaResolver(fetch=boom).resolve("https://x/p") is None


def test_extracts_publication_date_from_article_meta():
    html = """<head>
      <meta property="og:title" content="Deep RL from Human Preferences">
      <meta property="article:published_time" content="2017-06-12T09:00:00Z">
    </head>"""
    assert parse_html_meta(html)["published_at"] == "2017-06-12T09:00:00Z"


def test_extracts_publication_date_from_json_ld():
    html = """<head>
      <meta property="og:title" content="Concrete Problems in AI Safety">
      <script type="application/ld+json">
      {"@type": "Article", "headline": "x", "datePublished": "2016-06-21"}
      </script>
    </head>"""
    assert parse_html_meta(html)["published_at"] == "2016-06-21T00:00:00Z"


def test_json_ld_date_found_inside_graph_in_body():
    # JSON-LD often sits in the body and nests the article under @graph.
    html = """<head><meta property="og:title" content="Some Post"></head>
    <body>
      <script type="application/ld+json">
      {"@graph": [{"@type": "WebSite"}, {"@type": "Article", "datePublished": "2021-11-03T00:00:00+00:00"}]}
      </script>
    </body>"""
    assert parse_html_meta(html)["published_at"] == "2021-11-03T00:00:00Z"


def test_no_date_yields_none():
    html = '<head><meta property="og:title" content="Undated Post"></head>'
    assert parse_html_meta(html)["published_at"] is None


def test_resolver_returns_published_date():
    page = """<head>
      <meta property="og:title" content="Dated Research Post">
      <meta property="article:published_time" content="2019-03-11">
    </head>"""
    meta = HtmlMetaResolver(fetch=lambda _u: page).resolve("https://x/p")
    assert meta["published_at"] == "2019-03-11T00:00:00Z"
