"""Configuration schema for paper-watch, loaded from a YAML file.

Secrets (SMTP password, Anthropic API key) are NOT stored here; they come from
environment variables / .env so the config file can be committed safely.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class FeedConfig(BaseModel):
    name: str
    url: str


# "Obviously a paper" link allowlist for the Slack source: arXiv, the alignment
# forums, and the major labs' safety/alignment/interpretability blogs. Items
# linking these bypass the relevance gate; everything else is gated like Twitter.
_DEFAULT_PAPER_LINK_DOMAINS = [
    "arxiv.org",
    "lesswrong.com",
    "alignmentforum.org",
    "openreview.net",
    "anthropic.com",
    "openai.com",
    "deepmind.google",
    "deepmind.com",
    "transformer-circuits.pub",
    "distill.pub",
]


class SlackChannel(BaseModel):
    id: str
    name: str
    # A trusted channel's items bypass the relevance gate wholesale (e.g. a
    # curated paper channel). Absent ⇒ not trusted.
    trusted: bool = False


class SlackWorkspace(BaseModel):
    name: str
    # Name of the env var holding this workspace's user token (xoxp-…); the
    # token itself stays out of the committed config.
    token_env: str
    channels: list[SlackChannel] = Field(default_factory=list)


class SlackConfig(BaseModel):
    workspaces: list[SlackWorkspace] = Field(default_factory=list)
    paper_link_domains: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_PAPER_LINK_DOMAINS)
    )


class ScoringWeights(BaseModel):
    # Hand-set starting points; tune offline against the ground-truth eval
    # before trusting relative values.
    relevance: float = 2.0  # LLM 0-4 vs reader profile (cached at enrichment)
    source: float = 1.0  # per-source base weight (see Config.source_priors)
    overlap: float = 1.0
    velocity: float = 0.5
    feedback: float = 1.0
    author: float = 0.5  # any author on the config `authors` whitelist
    resurface_boost: float = 0.5


# Base weight per source label, longest-prefix matched ("slack:alignment:x"
# beats "slack"). Curated human channels outrank raw feeds; corporate blogs
# barely count. "default" covers unmatched sources.
_DEFAULT_SOURCE_PRIORS: dict[str, float] = {
    "default": 0.5,
    "arxiv": 0.6,
    "slack": 0.8,
    "twitter": 0.5,
    "rss": 0.4,
    "rss:OpenAI Blog": 0.1,
}


class SmtpConfig(BaseModel):
    host: str = "smtp.gmail.com"
    port: int = 587
    username: str = ""
    from_addr: str = ""
    to_addr: str = ""


class LlmConfig(BaseModel):
    # Cheap tier is plenty for TL;DR / tagging / relevance gating.
    # Bump to claude-opus-4-8 in config for higher-quality enrichment.
    model: str = "claude-haiku-4-5"
    max_enrich_per_run: int = 50
    # Reader profile + controlled tag vocabulary included in the enrichment
    # prompt (see profile.md / tags.yaml at the repo root).
    profile_path: str = "profile.md"
    tags_path: str = "tags.yaml"


class Config(BaseModel):
    db_path: str = "paper_watch.db"
    authors: list[str] = Field(default_factory=list)
    feeds: list[FeedConfig] = Field(default_factory=list)
    handles: list[str] = Field(default_factory=list)
    nitter_instances: list[str] = Field(
        default_factory=lambda: ["https://nitter.net"]
    )
    slack: SlackConfig | None = None
    # Local run times the cron installer reads; "configurable" per design.
    schedule: list[str] = Field(default_factory=lambda: ["08:00", "16:00"])
    top_n: int = 15
    # How far back to fetch papers when `--since` isn't given. Wider than one
    # cron interval so nothing slips through the gaps; already-shown papers are
    # deduped downstream, so a generous window is cheap.
    lookback: str = "7d"
    # How recently an entry must have been mentioned to enter the digest as a
    # fresh (never-shown) paper; also the window over which recent mentions are
    # counted for the velocity score term and the surge test.
    candidate_window_days: int = 7
    # How recently an already-shown paper must have been mentioned to be eligible
    # to resurface (it still only reappears if it also surges within
    # candidate_window_days).
    resurface_window_days: int = 21
    scoring: ScoringWeights = Field(default_factory=ScoringWeights)
    source_priors: dict[str, float] = Field(
        default_factory=lambda: dict(_DEFAULT_SOURCE_PRIORS)
    )
    smtp: SmtpConfig = Field(default_factory=SmtpConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"config file not found: {path}")
        data = yaml.safe_load(path.read_text()) or {}
        return cls.model_validate(data)
