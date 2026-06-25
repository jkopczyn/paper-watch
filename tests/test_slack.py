from paper_watch.sources.slack import is_paper_link

DOMAINS = [
    "arxiv.org",
    "lesswrong.com",
    "alignmentforum.org",
    "anthropic.com",
]


def test_is_paper_link_matches_exact_domain():
    assert is_paper_link("https://arxiv.org/abs/2406.05678", DOMAINS)


def test_is_paper_link_matches_subdomain():
    assert is_paper_link("https://www.lesswrong.com/posts/abc/title", DOMAINS)
    assert is_paper_link("https://transformer.anthropic.com/x", DOMAINS)


def test_is_paper_link_rejects_unlisted_domain():
    assert not is_paper_link("https://somebody.substack.com/p/post", DOMAINS)
    assert not is_paper_link("https://example.com/paper", DOMAINS)


def test_is_paper_link_no_substring_false_positive():
    # "notarxiv.org" must not match "arxiv.org"
    assert not is_paper_link("https://notarxiv.org/abs/1", DOMAINS)


def test_is_paper_link_handles_garbage():
    assert not is_paper_link("not a url", DOMAINS)
    assert not is_paper_link("", DOMAINS)
