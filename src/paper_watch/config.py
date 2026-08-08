"""Configuration schema for paper-watch, loaded from a YAML file.

Secrets (SMTP password, Anthropic API key) are NOT stored here; they come from
environment variables / .env so the config file can be committed safely.
"""

from __future__ import annotations

from datetime import time
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

from paper_watch.schedule import parse_deliver_at, parse_weekdays


class FeedConfig(BaseModel):
    name: str
    url: str


class ScheduleConfig(BaseModel):
    """Which days the digest is delivered, and at what local time.

    Each delivery covers everything since the previous successful send, so the
    days define the series: with Tue+Fri, Friday's email covers Wed-Fri and
    Tuesday's covers Sat-Tue. The runner ticks more often than this (every 4h)
    and simply retries whenever a delivery moment is still uncovered — see
    `paper_watch.schedule`.
    """

    deliver_days: list[str] = Field(default_factory=lambda: ["tue", "fri"])
    deliver_at: str = "12:00"

    @property
    def weekdays(self) -> set[int]:
        return parse_weekdays(self.deliver_days)

    @property
    def at_time(self) -> time:
        return parse_deliver_at(self.deliver_at)

    @field_validator("deliver_days")
    @classmethod
    def _check_days(cls, value: list[str]) -> list[str]:
        parse_weekdays(value)  # raises on an unknown or empty day list
        return value

    @field_validator("deliver_at")
    @classmethod
    def _check_at(cls, value: str) -> str:
        parse_deliver_at(value)
        return value


class FeedbackRefreshConfig(BaseModel):
    """Weekly export→import of the reading group's poll votes, run as a second
    scheduled duty inside the ordinary tick (no extra systemd unit).

    `days`/`at` use the delivery schedule's grammar, and dueness has the same
    owed/collapse semantics against its own watermark — see `paper_watch.refresh`.
    """

    days: list[str] = Field(default_factory=lambda: ["thu"])
    at: str = "12:00"
    # slack.workspaces name whose voting_channels hold the reading-group polls.
    workspace: str = "far"
    groundtruth_path: str = "groundtruth.csv"
    # Papers the group read within this many weeks are dropped from digests
    # (display-only: their feedback still moves the weights).
    exclude_read_weeks: int = 26

    @property
    def weekdays(self) -> set[int]:
        return parse_weekdays(self.days)

    @property
    def at_time(self) -> time:
        return parse_deliver_at(self.at)

    @field_validator("days")
    @classmethod
    def _check_days(cls, value: list[str]) -> list[str]:
        parse_weekdays(value)  # raises on an unknown or empty day list
        return value

    @field_validator("at")
    @classmethod
    def _check_at(cls, value: str) -> str:
        parse_deliver_at(value)
        return value


class GraphqlFeedConfig(BaseModel):
    """A ForumMagnum GraphQL feed (LessWrong / Alignment Forum), filtered by tag.

    Queries the forum's public GraphQL endpoint for the newest posts carrying
    `tag_id`, keeping only those at or above `min_karma`. More robust than the
    RSS feed (which scrapes the same data through shakier infrastructure) and
    lets us apply our own karma threshold instead of trusting a URL param.
    """

    name: str
    endpoint: str  # e.g. https://www.lesswrong.com/graphql
    tag_id: str  # the tag's _id, not its slug (query `tags(tagBySlug)` to find it)
    min_karma: int = 30
    limit: int = 50  # newest-N window fetched per run


class PageConfig(BaseModel):
    """A blog index page with no RSS feed, watched by diffing its links."""

    name: str
    url: str
    # A trusted page's items bypass the relevance gate (e.g. a major lab's
    # safety blog, where every post is on-topic). Absent ⇒ gated like RSS.
    trusted: bool = False


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
    # Channels scanned for paper links (the ingestion source).
    ingestion_channels: list[SlackChannel] = Field(default_factory=list)
    # Channels holding the reading-group polls, scanned by `paper-watch
    # groundtruth` for evaluation votes. Not ingested as paper links.
    voting_channels: list[SlackChannel] = Field(default_factory=list)


class SlackConfig(BaseModel):
    workspaces: list[SlackWorkspace] = Field(default_factory=list)
    paper_link_domains: list[str] = Field(
        default_factory=lambda: list(_DEFAULT_PAPER_LINK_DOMAINS)
    )


class ScoringWeights(BaseModel):
    # Scaled so raw score targets a 0-10 range on real data (see
    # deploy/measure_score_distribution.py); tune offline against the
    # ground-truth eval before trusting relative values.
    relevance: float = 4.0  # LLM 0-10 vs reader profile (cached at enrichment)
    source: float = 2.0  # per-source base weight (see Config.source_priors)
    overlap: float = 2.0
    velocity: float = 1.0
    feedback: float = 2.0  # starting weight; ramps up with feedback (see score)
    author: float = 1.0  # any author on the config `authors` whitelist
    resurface_boost: float = 1.0


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
    # Watched pages are hand-picked primary blogs; weight like curated feeds.
    "page": 0.6,
    # Tag-filtered forum queries (LessWrong AI tag): a wider, takes-ier
    # distribution than the Alignment Forum feed, so weight it below RSS.
    # Override per feed ("graphql:<name>") in config when adding more.
    "graphql": 0.3,
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
    # Tag-filtered ForumMagnum GraphQL feeds (LessWrong / Alignment Forum).
    graphql: list[GraphqlFeedConfig] = Field(default_factory=list)
    # Blog index pages without an RSS feed, watched by diffing their link sets
    # (new link on the page ⇒ new post).
    pages: list[PageConfig] = Field(default_factory=list)
    handles: list[str] = Field(default_factory=list)
    nitter_instances: list[str] = Field(
        default_factory=lambda: ["https://nitter.net"]
    )
    # Seconds to wait between Nitter requests. Nitter scrapes Twitter's
    # heavily rate-limited guest API, so a self-hosted instance often needs a
    # generous pause; raise this if you still see 429s.
    nitter_min_interval: float = 2.0
    slack: SlackConfig | None = None
    # Delivery days and local delivery time. The runner ticks every few hours
    # and only mails when one of these moments is uncovered.
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    # Weekly export→import of the reading group's poll votes; absent means the
    # feedback loop stays manual (`paper-watch groundtruth` / `feedback import`).
    feedback_refresh: FeedbackRefreshConfig | None = None
    top_n: int = 20
    # The digest leads with up to `max_new` genuinely new papers (never shown
    # before, first mentioned within `new_window`); the remaining slots up to
    # `top_n` are padded with at most `max_resurface` resurfaced papers that
    # outscore the new ones' average. Extra new papers beyond `max_new` are
    # dropped this run.
    #
    # `new_window` is only the fallback bound on "new": once a digest has been
    # delivered, freshness is measured from the last successful send, so a
    # paper first seen on Wednesday still leads Friday's email.
    new_window: str = "4d"
    max_new: int = 20
    max_resurface: int = 5
    # A paper published longer ago than this is marked OLDER and treated as
    # padding even the first time we see it — news to us, but not new — so it
    # shares the `max_resurface` budget instead of a lead slot.
    old_after_days: int = 90
    # How many runs in a row a source must fail before the digest calls it out.
    # A dead URL fails every run; a rate-limit blip clears on the next one, and
    # at 4-hourly ticks this is about half a day of being genuinely down.
    alert_after_failures: int = 3
    # Window over which each item is tagged with how many past digests surfaced
    # it, shown as a "surfaced N×" chip. Must span several digests to say
    # anything: at two deliveries a week, a 48h window only ever saw one.
    recent_window: str = "14d"
    # Fill an entry that still has no displayable URL by searching Semantic
    # Scholar / Crossref for its title and adopting the paper's canonical link.
    url_search: bool = True
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
    # How many mentions inside candidate_window_days count as a surge, i.e. as
    # renewed attention strong enough to bring an already-shown paper back.
    # Raise it to resurface less; 1 resurfaces on any new mention.
    resurface_min_mentions: int = 2
    # Resolve bare tweet links (via local Nitter) and paper links inside
    # newsletter bodies into real paper entries. Both zero-LLM, best-effort.
    tweet_resolution: bool = True
    newsletter_links: bool = True
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
