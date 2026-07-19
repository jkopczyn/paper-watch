from click.testing import CliRunner

import paper_watch.groundtruth as groundtruth_mod
import paper_watch.sources.slack as slack_mod
from paper_watch.cli import cli

CONFIG = """
slack:
  workspaces:
    - name: mats
      token_env: SLACK_TOKEN_MATS
      ingestion_channels:
        - {id: C1, name: papers}
      voting_channels:
        - {id: CV1, name: reading-group}
"""


def _write_config(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(CONFIG)
    return cfg


def test_sources_reports_slack(tmp_path):
    cfg = _write_config(tmp_path)
    result = CliRunner().invoke(cli, ["sources", "--config", str(cfg)])
    assert result.exit_code == 0
    assert "Slack:         1 workspace(s), 1 channel(s)" in result.output


def test_slack_channels_lists_channels(tmp_path, monkeypatch):
    cfg = _write_config(tmp_path)
    monkeypatch.setenv("SLACK_TOKEN_MATS", "xoxp-test")
    monkeypatch.setattr(
        slack_mod,
        "list_channels",
        lambda token, **kw: [{"id": "C1", "name": "papers"}, {"id": "C2", "name": "random"}],
    )
    result = CliRunner().invoke(
        cli, ["slack-channels", "--config", str(cfg), "--workspace", "mats"]
    )
    assert result.exit_code == 0
    assert "C1\tpapers" in result.output
    assert "2 channel(s)" in result.output


def test_slack_channels_unknown_workspace(tmp_path):
    cfg = _write_config(tmp_path)
    result = CliRunner().invoke(
        cli, ["slack-channels", "--config", str(cfg), "--workspace", "nope"]
    )
    assert result.exit_code != 0
    assert "not in config.slack.workspaces" in result.output


def test_slack_channels_missing_token(tmp_path, monkeypatch):
    cfg = _write_config(tmp_path)
    monkeypatch.delenv("SLACK_TOKEN_MATS", raising=False)
    result = CliRunner().invoke(
        cli, ["slack-channels", "--config", str(cfg), "--workspace", "mats"]
    )
    assert result.exit_code != 0
    assert "SLACK_TOKEN_MATS" in result.output


def test_groundtruth_defaults_to_config_voting_channels(tmp_path, monkeypatch):
    cfg = _write_config(tmp_path)
    monkeypatch.setenv("SLACK_TOKEN_MATS", "xoxp-test")
    captured = {}

    def fake_export(token, channel_ids, *, oldest, path, **kw):
        captured["token"] = token
        captured["channel_ids"] = channel_ids
        return 3

    monkeypatch.setattr(groundtruth_mod, "export_groundtruth", fake_export)
    result = CliRunner().invoke(
        cli,
        ["groundtruth", "--config", str(cfg), "--workspace", "mats", "--out", str(tmp_path / "gt.csv")],
    )
    assert result.exit_code == 0, result.output
    assert captured["channel_ids"] == ["CV1"]
    assert "Wrote 3 poll option(s)" in result.output


def test_groundtruth_channel_flag_overrides_config(tmp_path, monkeypatch):
    cfg = _write_config(tmp_path)
    monkeypatch.setenv("SLACK_TOKEN_MATS", "xoxp-test")
    captured = {}
    monkeypatch.setattr(
        groundtruth_mod,
        "export_groundtruth",
        lambda token, channel_ids, **kw: captured.setdefault("channel_ids", channel_ids) or 0,
    )
    result = CliRunner().invoke(
        cli,
        ["groundtruth", "--config", str(cfg), "--workspace", "mats",
         "--channel", "COVERRIDE", "--out", str(tmp_path / "gt.csv")],
    )
    assert result.exit_code == 0, result.output
    assert captured["channel_ids"] == ["COVERRIDE"]


def test_groundtruth_errors_without_voting_channels(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        """
slack:
  workspaces:
    - name: mats
      token_env: SLACK_TOKEN_MATS
      ingestion_channels:
        - {id: C1, name: papers}
"""
    )
    monkeypatch.setenv("SLACK_TOKEN_MATS", "xoxp-test")
    result = CliRunner().invoke(
        cli, ["groundtruth", "--config", str(cfg), "--workspace", "mats"]
    )
    assert result.exit_code != 0
    assert "voting_channels" in result.output
