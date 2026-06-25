from click.testing import CliRunner

import paper_watch.sources.slack as slack_mod
from paper_watch.cli import cli

CONFIG = """
slack:
  workspaces:
    - name: mats
      token_env: SLACK_TOKEN_MATS
      channels:
        - {id: C1, name: papers}
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
