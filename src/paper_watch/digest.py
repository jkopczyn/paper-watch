"""Build and render the ranked HTML email digest."""

from __future__ import annotations

from dataclasses import dataclass, field

from jinja2 import Environment

from paper_watch.score import ScoreFeatures


@dataclass
class DigestItem:
    title: str
    authors: list[str]
    tldr: str | None
    why: str | None
    tags: list[str]
    links: dict[str, str]
    score: float
    explanation: str
    resurfaced: bool = False
    extra_tags: list[str] = field(default_factory=list)


def score_explanation(f: ScoreFeatures) -> str:
    """A short, human-readable reason a paper ranked where it did."""
    parts = [f"{f.distinct_sources} source{'s' if f.distinct_sources != 1 else ''}"]
    growth = max(0, (f.citation_count or 0) - (f.citation_count_prev or 0))
    if growth:
        parts.append(f"+{growth} citations")
    if f.new_mentions_in_window:
        parts.append(f"{f.new_mentions_in_window} recent mentions")
    if f.feedback_affinity > 0.05:
        parts.append("liked by group")
    elif f.feedback_affinity < -0.05:
        parts.append("disliked by group")
    if f.resurfaced:
        parts.append("resurfaced")
    return " · ".join(parts)


_TEMPLATE = """\
<!doctype html>
<html>
<head><meta charset="utf-8"><title>paper-watch digest</title></head>
<body style="font-family: -apple-system, Helvetica, Arial, sans-serif; max-width: 720px; margin: 0 auto; color: #1a1a1a;">
  <h1 style="font-size: 18px;">paper-watch digest</h1>
  <p style="color:#666; font-size: 12px;">{{ generated_at }} · {{ items|length }} paper(s)</p>
  {% if not items %}
  <p>Nothing new worth surfacing this run.</p>
  {% endif %}
  {% for it in items %}
  <div style="border-top: 1px solid #eee; padding: 12px 0;">
    <div style="font-size: 16px; font-weight: 600;">
      {% if it.resurfaced %}<span style="background:#fde68a; color:#92400e; font-size:10px; padding:1px 5px; border-radius:3px; vertical-align:middle;">RESURFACED</span> {% endif %}
      {{ it.title }}
    </div>
    {% if it.authors %}<div style="color:#666; font-size:12px;">{{ it.authors|join(", ") }}</div>{% endif %}
    {% if it.tldr %}<p style="margin: 6px 0;">{{ it.tldr }}</p>{% endif %}
    {% if it.why %}<p style="margin: 6px 0; color:#555; font-style: italic;">{{ it.why }}</p>{% endif %}
    <div style="font-size: 12px; margin: 6px 0;">
      {% for t in it.tags %}<span style="background:#eef; color:#334; padding:1px 6px; border-radius:3px; margin-right:4px;">{{ t }}</span>{% endfor %}
    </div>
    <div style="font-size: 12px;">
      {% for label, url in it.links.items() %}<a href="{{ url }}" style="margin-right:10px;">{{ label }}</a>{% endfor %}
    </div>
    <div style="color:#999; font-size: 11px; margin-top: 4px;">score {{ "%.2f"|format(it.score) }} — {{ it.explanation }}</div>
  </div>
  {% endfor %}
</body>
</html>
"""


def render_html(items: list[DigestItem], *, generated_at: str) -> str:
    ranked = sorted(items, key=lambda i: i.score, reverse=True)
    env = Environment(autoescape=True)
    return env.from_string(_TEMPLATE).render(items=ranked, generated_at=generated_at)
