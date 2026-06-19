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
    assert cfg.scoring.overlap == 2.0
    assert cfg.scoring.velocity == 1.5
    # unspecified weight keeps its default
    assert cfg.scoring.feedback == pytest.approx(1.0)
    assert cfg.smtp.to_addr == "me@gmail.com"
    assert cfg.llm.max_enrich_per_run == 30


def test_load_missing_file_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        Config.load(tmp_path / "nope.yaml")
