# paper-watch

Scan AI-safety paper sources and email yourself a ranked digest a few times a day.

Sources (v1): **arXiv author feeds** (replaces Google Scholar alerts), **RSS newsletters/blogs**,
and **Twitter via Nitter** per-user RSS. Papers are deduplicated across sources and ranked by
cross-source overlap, citation/social velocity, and learned reading-group feedback. Each item gets
an LLM-generated TL;DR, topic tags, and links. Previously-shown papers can "resurface" if their
attention surges within a rolling 2–4 week window.

See `method-rec.md` for the source list this is built from.

## Setup

```bash
uv sync                      # install deps
cp .env.example .env         # add SMTP app password + ANTHROPIC_API_KEY
uv run paper-watch init      # write config.yaml, then edit it
```

## Usage

```bash
uv run paper-watch run --dry-run   # fetch + render, don't send (M10)
uv run paper-watch run             # fetch, score, email the digest
uv run paper-watch feedback export # weekly reading-group candidates file (M9)
uv run paper-watch feedback import # re-import filled picks + ratings
```

## Development

```bash
uv run pytest
```

Source adapters are tested against recorded fixtures (no live network), and the LLM is mocked.
