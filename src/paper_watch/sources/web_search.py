"""Recover a title / snippet / abstract for a URL-only entry via Claude's
web_search server tool.

Some entries arrive as a bare URL with no title and no abstract — a dead link, a
scanned PDF, a page with no Open Graph metadata — that the deterministic
resolvers (arXiv/OpenReview/PDF/HTML) can't crack. A web search can still recover
the work's title and a one-line snippet (and sometimes an abstract) from the
search index even when the page itself yields nothing.

Best-effort and key-gated, like the PDF-OCR helper: no ANTHROPIC_API_KEY, no
resolver. The LLM is used only for this metadata recovery, never for ranking.
"""

from __future__ import annotations

import json
import re

# Models that support the dynamic-filtering web_search tool; everything else
# (e.g. Haiku 4.5) uses the basic variant. See the claude-api server-tools ref.
_ADVANCED_WEB_SEARCH = {
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
}


def web_search_tool(model: str, *, max_uses: int) -> dict:
    tool_type = (
        "web_search_20260209" if model in _ADVANCED_WEB_SEARCH else "web_search_20250305"
    )
    return {"type": tool_type, "name": "web_search", "max_uses": max_uses}


_SYSTEM = (
    "You identify the academic paper, blog post, or article that a URL points to. "
    "Use web_search to find its real title and a short description. When done, reply "
    "with ONLY a JSON object and no other text:\n"
    '{"title": <the work\'s real title, or null if you cannot identify it>, '
    '"snippet": <one-sentence description, or null>, '
    '"abstract": <the abstract if you find one, else null>}'
)


def parse_json_object(text: str) -> dict | None:
    """Lenient JSON-object extraction from a model reply (tolerates code fences)."""
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


class WebSearchResolver:
    """Resolve a URL to {title, snippet, abstract} using Claude + web_search."""

    def __init__(self, model: str, client=None, *, max_uses: int = 3):
        self.model = model
        self.max_uses = max_uses
        if client is None:
            import anthropic

            client = anthropic.Anthropic()
        self._client = client

    def resolve(self, url: str, blurb: str | None = None) -> dict | None:
        if not url:
            return None
        prompt = f"Identify the work at this URL: {url}\n"
        if blurb:
            prompt += f"Context from where it was shared: {blurb}\n"
        messages = [{"role": "user", "content": prompt}]
        tool = web_search_tool(self.model, max_uses=self.max_uses)

        resp = None
        # The web_search tool runs a server-side loop that can pause; resend the
        # accumulated turn to resume, bounded so a stuck loop can't spin.
        for _ in range(self.max_uses + 2):
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=_SYSTEM,
                tools=[tool],
                messages=messages,
            )
            if getattr(resp, "stop_reason", None) != "pause_turn":
                break
            messages = messages + [{"role": "assistant", "content": resp.content}]

        if resp is None or getattr(resp, "stop_reason", None) == "refusal":
            return None
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        )
        data = parse_json_object(text)
        if not data or not data.get("title"):
            return None
        snippet = (data.get("snippet") or "").strip() or None
        abstract = (data.get("abstract") or "").strip() or None
        return {"title": str(data["title"]).strip(), "snippet": snippet, "abstract": abstract}
