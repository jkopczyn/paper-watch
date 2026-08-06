"""Build and render the ranked HTML email digest."""

from __future__ import annotations

from dataclasses import dataclass, field

from jinja2 import Environment

from paper_watch.score import ScoreFeatures, citation_growth


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
    # Published long enough ago to not count as news, whether or not we have
    # shown it before. Distinct from `resurfaced` — one means "you have seen
    # this", the other "this is not new" — so both chips can appear at once.
    is_old: bool = False
    extra_tags: list[str] = field(default_factory=list)
    # Provenance / recency metadata shown as chips beneath the paper.
    pub_display: str = ""  # publication date, e.g. "2018-10" (empty ⇒ hidden)
    pub_is_estimate: bool = False  # rendered with a leading "~" when estimated
    surfaced_recent: int = 0  # times surfaced in the recent window (0 ⇒ hidden)
    # full source labels shown as chips, e.g. ["arxiv", "slack:alignment:papers"]
    sources: list[str] = field(default_factory=list)
    trusted: bool = False  # any trusted channel is a source


@dataclass
class SourceWarning:
    """A source that has stopped working, surfaced at the top of the digest.

    A dead source is invisible by construction — it yields nothing, which reads
    exactly like a blog that hasn't posted lately. The email is the only place
    it reliably gets seen, and a quiet digest is precisely when it matters most.
    """

    label: str
    url: str
    consecutive_failures: int
    last_ok_at: str | None  # None ⇒ never worked at all (a URL wrong from the start)
    error: str

    @property
    def since(self) -> str:
        return f"since {self.last_ok_at[:10]}" if self.last_ok_at else "never succeeded"


def score_explanation(f: ScoreFeatures) -> str:
    """A short, human-readable reason a paper ranked where it did.

    The distinct-source count is intentionally omitted: the digest lists the
    actual source labels as chips, which carries the same information.
    """
    parts: list[str] = []
    if f.relevance is not None:
        parts.append(f"relevance {f.relevance}/10")
    if f.tracked_author:
        parts.append("tracked author")
    growth = citation_growth(f.citation_count, f.citation_count_prev)
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
  {% if warnings %}
  <div style="border:1px solid #fcd34d; background:#fffbeb; border-radius:4px; padding:8px 12px; margin: 10px 0; font-size:12px; color:#78350f;">
    <div style="font-weight:600;">⚠ {{ warnings|length }} source{{ "s" if warnings|length != 1 }} unhealthy</div>
    {% for w in warnings %}
    <div style="margin-top:4px;">
      {{ w.label }} — {{ w.consecutive_failures }} consecutive failure{{ "s" if w.consecutive_failures != 1 }}, {{ w.since }}<br>
      <span style="color:#92400e;">{{ w.error }}</span> · <span style="color:#a16207;">{{ w.url }}</span>
    </div>
    {% endfor %}
  </div>
  {% endif %}
  {% if not items %}
  <p>Nothing new worth surfacing this run.</p>
  {% endif %}
  {% for it in items %}
  <div style="border-top: 1px solid #eee; padding: 12px 0;">
    <div style="font-size: 16px; font-weight: 600;">
      {% if it.is_old %}<span style="background:#e5e7eb; color:#4b5563; font-size:10px; padding:1px 5px; border-radius:3px; vertical-align:middle;">OLDER{% if it.pub_display %} · {{ it.pub_display }}{% endif %}</span> {% endif %}
      {% if it.resurfaced %}<span style="background:#fde68a; color:#92400e; font-size:10px; padding:1px 5px; border-radius:3px; vertical-align:middle;">RESURFACED</span> {% endif %}
      {% if it.trusted %}<span style="background:#bbf7d0; color:#166534; font-size:10px; padding:1px 5px; border-radius:3px; vertical-align:middle;">TRUSTED</span> {% endif %}
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
    <div style="font-size: 11px; color:#667; margin-top: 5px;">
      {% if it.pub_display %}<span style="background:#f1f1f4; color:#444; padding:1px 6px; border-radius:3px; margin-right:4px;">{{ "~" if it.pub_is_estimate }}{{ it.pub_display }}</span>{% endif %}
      {% if it.surfaced_recent > 0 %}<span style="background:#f1f1f4; color:#444; padding:1px 6px; border-radius:3px; margin-right:4px;">surfaced {{ it.surfaced_recent }}×</span>{% endif %}
      {% for s in it.sources %}<span style="background:#e7edf7; color:#334; padding:1px 6px; border-radius:3px; margin-right:4px;">{{ s }}</span>{% endfor %}
    </div>
    <div style="color:#999; font-size: 11px; margin-top: 4px;">score {{ "%.2f"|format(it.score) }} — {{ it.explanation }}</div>
  </div>
  {% endfor %}
</body>
</html>
"""


def render_html(
    items: list[DigestItem],
    *,
    generated_at: str,
    warnings: list[SourceWarning] | None = None,
) -> str:
    # New results lead; padding — reruns and long-published papers alike —
    # follows. Within each group, rank by score. (False < True, so the fresh
    # leads sort first.)
    ranked = sorted(items, key=lambda i: (i.resurfaced or i.is_old, -i.score))
    env = Environment(autoescape=True)
    return env.from_string(_TEMPLATE).render(
        items=ranked, generated_at=generated_at, warnings=list(warnings or [])
    )
