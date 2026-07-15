from pathlib import Path

import pytest

from paper_watch.config import Config


def test_load_empty_config_uses_defaults(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("")
    cfg = Config.load(cfg_file)

    assert cfg.authors == []
    assert cfg.feeds == []
    assert cfg.handles == []
    assert cfg.top_n > 0
    assert cfg.nitter_instances  # non-empty default
    assert cfg.smtp.host == "smtp.gmail.com"
    assert cfg.smtp.port == 587
    # resurface window is in the 14-28 day band per design
    assert 14 <= cfg.resurface_window_days <= 28
    # candidate/velocity window is shorter than the resurface window
    assert cfg.candidate_window_days == 7
    assert cfg.candidate_window_days <= cfg.resurface_window_days
    # default ingest lookback is wider than a single cron interval
    assert cfg.lookback == "7d"
    # digest-composition knobs (wishlist)
    assert cfg.new_window == "24h"
    assert cfg.max_new == 10
    assert cfg.recent_window == "48h"
    assert cfg.url_search is True


def test_load_populated_config(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
authors:
  - Neel Nanda
  - Ethan Perez
feeds:
  - name: ML Safety
    url: https://newsletter.mlsafety.org/feed
handles:
  - NeelNanda5
top_n: 20
lookback: 14d
resurface_window_days: 21
scoring:
  overlap: 2.0
  velocity: 1.5
smtp:
  username: me@gmail.com
  from_addr: me@gmail.com
  to_addr: me@gmail.com
llm:
  max_enrich_per_run: 30
"""
    )
    cfg = Config.load(cfg_file)

    assert cfg.authors == ["Neel Nanda", "Ethan Perez"]
    assert cfg.feeds[0].name == "ML Safety"
    assert cfg.feeds[0].url == "https://newsletter.mlsafety.org/feed"
    assert cfg.handles == ["NeelNanda5"]
    assert cfg.top_n == 20
    assert cfg.lookback == "14d"
    assert cfg.scoring.overlap == 2.0
    assert cfg.scoring.velocity == 1.5
    # unspecified weight keeps its default
    assert cfg.scoring.feedback == pytest.approx(1.0)
    assert cfg.smtp.to_addr == "me@gmail.com"
    assert cfg.llm.max_enrich_per_run == 30


def test_load_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        Config.load(tmp_path / "nope.yaml")


def test_slack_absent_defaults_to_none(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("")
    cfg = Config.load(cfg_file)
    assert cfg.slack is None


def test_load_slack_config(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
slack:
  workspaces:
    - name: mats
      token_env: SLACK_TOKEN_MATS
      channels:
        - {id: C0001, name: papers}
    - name: alignment
      token_env: SLACK_TOKEN_ALIGNMENT
      channels:
        - {id: C0009, name: aaron-papers, trusted: true}
"""
    )
    cfg = Config.load(cfg_file)

    assert cfg.slack is not None
    assert [w.name for w in cfg.slack.workspaces] == ["mats", "alignment"]
    mats = cfg.slack.workspaces[0]
    assert mats.token_env == "SLACK_TOKEN_MATS"
    assert mats.channels[0].id == "C0001"
    assert mats.channels[0].name == "papers"
    # trusted defaults to False when omitted
    assert mats.channels[0].trusted is False
    assert cfg.slack.workspaces[1].channels[0].trusted is True
    # paper_link_domains gets a sensible default allowlist
    assert "arxiv.org" in cfg.slack.paper_link_domains
    assert "lesswrong.com" in cfg.slack.paper_link_domains


def test_slack_paper_link_domains_override(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
slack:
  paper_link_domains:
    - example.org
  workspaces: []
"""
    )
    cfg = Config.load(cfg_file)
    assert cfg.slack.paper_link_domains == ["example.org"]
