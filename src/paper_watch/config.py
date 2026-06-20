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


class ScoringWeights(BaseModel):
    overlap: float = 1.0
    velocity: float = 1.0
    feedback: float = 1.0
    resurface_boost: float = 2.0


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


class Config(BaseModel):
    db_path: str = "paper_watch.db"
    authors: list[str] = Field(default_factory=list)
    feeds: list[FeedConfig] = Field(default_factory=list)
    handles: list[str] = Field(default_factory=list)
    nitter_instances: list[str] = Field(
        default_factory=lambda: ["https://nitter.net"]
    )
    # Seconds to wait between Nitter requests. Nitter scrapes Twitter's
    # heavily rate-limited guest API, so a self-hosted instance often needs a
    # generous pause; raise this if you still see 429s.
    nitter_min_interval: float = 2.0
    # Local run times the cron installer reads; "configurable" per design.
    schedule: list[str] = Field(default_factory=lambda: ["08:00", "16:00"])
    top_n: int = 15
    # How far back to fetch papers when `--since` isn't given. Wider than one
    # cron interval so nothing slips through the gaps; already-shown papers are
    # deduped downstream, so a generous window is cheap.
    lookback: str = "7d"
    resurface_window_days: int = 21
    scoring: ScoringWeights = Field(default_factory=ScoringWeights)
    smtp: SmtpConfig = Field(default_factory=SmtpConfig)
    llm: LlmConfig = Field(default_factory=LlmConfig)

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"config file not found: {path}")
        data = yaml.safe_load(path.read_text()) or {}
        return cls.model_validate(data)
