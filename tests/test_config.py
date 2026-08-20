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
    assert cfg.new_window == "4d"
    assert cfg.max_new == 20
    assert cfg.max_resurface == 5
    assert cfg.max_resurface < cfg.top_n
    # ~3 months: past that, a paper is padding rather than a lead
    assert cfg.old_after_days == 90
    assert cfg.recent_window == "14d"
    assert cfg.url_search is True
    # two digests a week, delivered at local noon on the last day of each series
    assert cfg.schedule.weekdays == {1, 4}
    assert cfg.schedule.at_time.hour == 12


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
    assert cfg.scoring.feedback == pytest.approx(2.0)
    # legacy single to_addr folds into the recipient list
    assert cfg.smtp.to_addrs == ["me@gmail.com"]
    assert cfg.llm.max_enrich_per_run == 30


def test_smtp_to_addrs_list(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
smtp:
  username: me@gmail.com
  from_addr: me@gmail.com
  to_addrs:
    - me@gmail.com
    - '"PRG Team (Slack)" <chan@far-labs.slack.com>'
"""
    )
    cfg = Config.load(cfg_file)
    assert cfg.smtp.to_addrs == [
        "me@gmail.com",
        '"PRG Team (Slack)" <chan@far-labs.slack.com>',
    ]


def test_schedule_override(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
schedule:
  deliver_days: [Mon, thursday]
  deliver_at: "16:30"
"""
    )
    cfg = Config.load(cfg_file)
    assert cfg.schedule.weekdays == {0, 3}
    assert (cfg.schedule.at_time.hour, cfg.schedule.at_time.minute) == (16, 30)


@pytest.mark.parametrize(
    "body",
    [
        "schedule:\n  deliver_days: [funday]\n",
        'schedule:\n  deliver_at: "noon"\n',
        "schedule:\n  deliver_days: []\n",
    ],
)
def test_bad_schedule_is_rejected_at_load(tmp_path: Path, body: str):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(body)
    with pytest.raises(ValueError):
        Config.load(cfg_file)


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
      ingestion_channels:
        - {id: C0001, name: papers}
      voting_channels:
        - {id: C0002, name: reading-group}
    - name: alignment
      token_env: SLACK_TOKEN_ALIGNMENT
      ingestion_channels:
        - {id: C0009, name: aaron-papers, trusted: true}
"""
    )
    cfg = Config.load(cfg_file)

    assert cfg.slack is not None
    assert [w.name for w in cfg.slack.workspaces] == ["mats", "alignment"]
    mats = cfg.slack.workspaces[0]
    assert mats.token_env == "SLACK_TOKEN_MATS"
    assert mats.ingestion_channels[0].id == "C0001"
    assert mats.ingestion_channels[0].name == "papers"
    # trusted defaults to False when omitted
    assert mats.ingestion_channels[0].trusted is False
    assert mats.voting_channels[0].id == "C0002"
    # voting_channels defaults to empty when omitted
    assert cfg.slack.workspaces[1].voting_channels == []
    assert cfg.slack.workspaces[1].ingestion_channels[0].trusted is True
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


def test_feedback_refresh_block_parses(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        """
feedback_refresh:
  days: [thu]
  at: "16:30"
  workspace: far
  groundtruth_path: gt.csv
  exclude_read_weeks: 12
"""
    )
    cfg = Config.load(cfg_file)
    assert cfg.feedback_refresh is not None
    assert cfg.feedback_refresh.weekdays == {3}
    at = cfg.feedback_refresh.at_time
    assert (at.hour, at.minute) == (16, 30)
    assert cfg.feedback_refresh.workspace == "far"
    assert cfg.feedback_refresh.groundtruth_path == "gt.csv"
    assert cfg.feedback_refresh.exclude_read_weeks == 12


def test_feedback_refresh_defaults(tmp_path: Path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("")
    # Absent block: the feature stays off.
    assert Config.load(cfg_file).feedback_refresh is None
    # Empty block: Thursday noon, the FAR polls, a 26-week horizon.
    cfg_file.write_text("feedback_refresh: {}\n")
    fr = Config.load(cfg_file).feedback_refresh
    assert fr is not None
    assert fr.weekdays == {3}
    assert fr.at_time.hour == 12
    assert fr.workspace == "far"
    assert fr.groundtruth_path == "groundtruth.csv"
    assert fr.exclude_read_weeks == 26


@pytest.mark.parametrize(
    "body",
    [
        "feedback_refresh:\n  days: [funday]\n",
        'feedback_refresh:\n  at: "noon"\n',
        "feedback_refresh:\n  days: []\n",
    ],
)
def test_feedback_refresh_rejects_bad_day(tmp_path: Path, body: str):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(body)
    with pytest.raises(ValueError):
        Config.load(cfg_file)
