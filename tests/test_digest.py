from paper_watch.digest import DigestItem, render_html, score_explanation
from paper_watch.score import ScoreFeatures


def _item(**kw):
    base = dict(
        title="Scalable Oversight",
        authors=["Ethan Perez"],
        tldr="A survey of oversight.",
        why="Bears on alignment.",
        tags=["oversight", "evals"],
        links={"abstract": "https://arxiv.org/abs/2406.01234", "pdf": "https://arxiv.org/pdf/2406.01234"},
        score=2.5,
        explanation="2 sources · resurfaced",
        resurfaced=True,
    )
    base.update(kw)
    return DigestItem(**base)


def test_render_html_includes_core_fields():
    html = render_html([_item()], generated_at="2026-06-19T08:00:00Z")
    assert "Scalable Oversight" in html
    assert "A survey of oversight." in html
    assert "Bears on alignment." in html
    assert "oversight" in html and "evals" in html
    assert "https://arxiv.org/abs/2406.01234" in html
    assert "https://arxiv.org/pdf/2406.01234" in html
    assert "Ethan Perez" in html
    assert "2 sources" in html  # score explanation


def test_render_html_sorts_by_score_desc():
    low = _item(title="Low Paper", score=1.0, resurfaced=False)
    high = _item(title="High Paper", score=9.0, resurfaced=False)
    html = render_html([low, high], generated_at="2026-06-19T08:00:00Z")
    assert html.index("High Paper") < html.index("Low Paper")


def test_render_html_marks_resurfaced():
    html = render_html([_item(resurfaced=True)], generated_at="2026-06-19T08:00:00Z")
    assert "resurfaced" in html.lower()


def test_render_html_empty():
    html = render_html([], generated_at="2026-06-19T08:00:00Z")
    assert "paper-watch" in html.lower()
    # should not crash and should say there's nothing


def test_score_explanation_reads_features():
    f = ScoreFeatures(
        distinct_sources=3,
        citation_count=10,
        citation_count_prev=4,
        new_mentions_in_window=2,
        feedback_affinity=0.4,
        resurfaced=True,
    )
    text = score_explanation(f)
    assert "3 sources" in text
    assert "+6 citations" in text
    assert "2 recent mentions" in text
    assert "liked by group" in text
    assert "resurfaced" in text
