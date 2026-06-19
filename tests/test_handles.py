from pathlib import Path

from paper_watch.handles import merge_handles


def test_merge_handles_unions_and_dedups(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("handles:\n  - NeelNanda5\n")

    added = merge_handles(cfg, ["EthanJPerez", "NeelNanda5", "BuckShlegeris"])

    assert sorted(added) == ["BuckShlegeris", "EthanJPerez"]

    import yaml

    data = yaml.safe_load(cfg.read_text())
    assert data["handles"] == ["BuckShlegeris", "EthanJPerez", "NeelNanda5"]


def test_merge_handles_strips_at_prefix(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("handles: []\n")
    merge_handles(cfg, ["@NeelNanda5"])
    import yaml

    assert yaml.safe_load(cfg.read_text())["handles"] == ["NeelNanda5"]


def test_merge_handles_creates_key_if_missing(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("top_n: 10\n")
    added = merge_handles(cfg, ["NeelNanda5"])
    assert added == ["NeelNanda5"]
    import yaml

    data = yaml.safe_load(cfg.read_text())
    assert data["handles"] == ["NeelNanda5"]
    assert data["top_n"] == 10  # existing keys preserved
