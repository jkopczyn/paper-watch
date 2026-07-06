from paper_watch.models import RawItem
from paper_watch.sources.newsletter_links import extract_paper_links

DOMAINS = ["arxiv.org", "openreview.net", "lesswrong.com"]


def _newsletter(body):
    return RawItem(
        source="rss:Import AI",
        url="https://newsletter.example/issue/1",
        text=body,
        published_at="2026-06-20T00:00:00Z",
        extract_ids_from_text=False,
    )


def test_keeps_only_paper_links_and_dedupes(fixture_text):
    items = extract_paper_links(_newsletter(fixture_text("newsletter_body.html")), DOMAINS)
    urls = [i.url for i in items]
    # arxiv (deduped to one), openreview forum, and the raw pdf — nav/social/jobs dropped
    assert urls == [
        "https://arxiv.org/abs/2606.08243",
        "https://openreview.net/forum?id=dy2HwmOvFX",
        "https://aibetrayal.com/paper.pdf",
    ]


def test_emitted_items_key_to_the_paper_not_the_newsletter(fixture_text):
    items = extract_paper_links(_newsletter(fixture_text("newsletter_body.html")), DOMAINS)
    for it in items:
        assert it.source == "rss:Import AI"
        assert it.mention_url == it.url
        assert it.extract_ids_from_text is True  # its own url IS the paper
        assert it.published_at == "2026-06-20T00:00:00Z"
    # anchor text carried as mention provenance
    assert "Building Comparative Motivation Profiles" in items[0].text


def test_no_body_returns_empty():
    assert extract_paper_links(RawItem(source="rss:X", url="u", text=None), DOMAINS) == []


def test_link_cap(monkeypatch):
    import paper_watch.sources.newsletter_links as nl

    monkeypatch.setattr(nl, "_MAX_LINKS_PER_ITEM", 2)
    body = "".join(
        f'<a href="https://arxiv.org/abs/2606.0{i:04d}">p{i}</a>' for i in range(5)
    )
    items = nl.extract_paper_links(_newsletter(body), DOMAINS)
    assert len(items) == 2
