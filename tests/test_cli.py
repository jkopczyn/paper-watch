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


def test_groundtruth_append_flag_passes_through(tmp_path, monkeypatch):
    cfg = _write_config(tmp_path)
    monkeypatch.setenv("SLACK_TOKEN_MATS", "xoxp-test")
    captured = {}

    def fake_export(token, channel_ids, *, oldest, path, append=False, **kw):
        captured["oldest"] = oldest
        captured["append"] = append
        return 2

    monkeypatch.setattr(groundtruth_mod, "export_groundtruth", fake_export)
    result = CliRunner().invoke(
        cli,
        ["groundtruth", "--config", str(cfg), "--workspace", "mats",
         "--append", "--out", str(tmp_path / "gt.csv")],
    )
    assert result.exit_code == 0, result.output
    assert captured["append"] is True
    assert captured["oldest"]  # --since-derived fallback still computed
    assert "Appended 2 new poll option(s)" in result.output


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


def test_run_reports_an_ingest_only_tick(tmp_path, monkeypatch):
    from datetime import datetime, timezone

    import paper_watch.runtime as runtime_mod

    cfg = _write_config(tmp_path)
    monkeypatch.setattr(
        runtime_mod,
        "run",
        lambda *a, **kw: runtime_mod.RunResult(
            new_count=3,
            enriched_count=2,
            attempted_delivery=False,
            next_delivery=datetime(2026, 8, 7, 12, tzinfo=timezone.utc),
        ),
    )
    result = CliRunner().invoke(cli, ["run", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "Ingested 3 new, enriched 2." in result.output
    assert "No digest due; next delivery Fri 2026-08-07 12:00." in result.output


def test_run_reports_a_gated_tick(tmp_path, monkeypatch):
    import paper_watch.runtime as runtime_mod

    cfg = _write_config(tmp_path)
    monkeypatch.setattr(
        runtime_mod,
        "run",
        lambda *a, **kw: runtime_mod.RunResult(
            enriched_count=1, polled=False, attempted_delivery=False
        ),
    )
    result = CliRunner().invoke(cli, ["run", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "Poll skipped (last < 24h ago); enriched 1." in result.output
    assert "Ingested" not in result.output


def test_run_reports_a_due_digest_that_could_not_be_filled(tmp_path, monkeypatch):
    import paper_watch.runtime as runtime_mod

    cfg = _write_config(tmp_path)
    monkeypatch.setattr(
        runtime_mod,
        "run",
        lambda *a, **kw: runtime_mod.RunResult(attempted_delivery=True, sent=False),
    )
    result = CliRunner().invoke(cli, ["run", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "not marked delivered" in result.output


def test_run_passes_force_send_through(tmp_path, monkeypatch):
    import paper_watch.runtime as runtime_mod

    cfg = _write_config(tmp_path)
    captured = {}

    def fake_run(config_path, **kw):
        captured.update(kw)
        return runtime_mod.RunResult(attempted_delivery=True, sent=True, chosen_ids=[1])

    monkeypatch.setattr(runtime_mod, "run", fake_run)
    result = CliRunner().invoke(cli, ["run", "--config", str(cfg), "--force-send"])
    assert result.exit_code == 0, result.output
    assert captured["force_send"] is True
    assert "Digest sent with 1 paper(s)." in result.output


def test_run_reports_unhealthy_sources(tmp_path, monkeypatch):
    import paper_watch.runtime as runtime_mod
    from paper_watch.digest import SourceWarning

    cfg = _write_config(tmp_path)
    monkeypatch.setattr(
        runtime_mod,
        "run",
        lambda *a, **kw: runtime_mod.RunResult(
            attempted_delivery=True,
            sent=True,
            chosen_ids=[1],
            warnings=[
                SourceWarning(
                    label="Transluce",
                    url="https://transluce.org/news",
                    consecutive_failures=18,
                    last_ok_at="2026-08-04T08:00:00Z",
                    error="404 Not Found",
                )
            ],
        ),
    )
    result = CliRunner().invoke(cli, ["run", "--config", str(cfg)])
    assert result.exit_code == 0, result.output
    assert "Transluce: 18 consecutive failures" in result.output
    assert "since 2026-08-04" in result.output
    assert "404 Not Found" in result.output


def test_resolve_ties_prompts_and_records(tmp_path):
    from datetime import datetime, timezone

    from paper_watch.store import Store

    ts = str(datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc).timestamp())
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"db_path: {tmp_path / 'pw.db'}\n")
    gt = tmp_path / "gt.csv"
    gt.write_text(
        "week,message_ts,option,emoji,votes,url,context\n"
        f"2026-W27,{ts},1,one,3,https://arxiv.org/abs/2605.01642,Paper A\n"
        f"2026-W27,{ts},2,two,3,https://blog.example/b,Paper B\n"
    )
    store = Store(tmp_path / "pw.db")
    store.close()

    result = CliRunner().invoke(
        cli,
        ["resolve-ties", "--config", str(cfg), "--groundtruth", str(gt)],
        input="1\n",
    )
    assert result.exit_code == 0, result.output
    assert "2026-W27" in result.output
    assert "2026-07-01" in result.output  # poll date, not just the week number
    assert "Paper A" in result.output and "arxiv.org" in result.output
    assert "0: all / none / don't remember" in result.output

    store = Store(tmp_path / "pw.db")
    urls = [r["url"] for r in store.conn.execute("SELECT url FROM readings")]
    assert urls == ["https://arxiv.org/abs/2605.01642"]
    store.close()

    # Settled: a second run finds nothing outstanding.
    result = CliRunner().invoke(
        cli, ["resolve-ties", "--config", str(cfg), "--groundtruth", str(gt)]
    )
    assert result.exit_code == 0
    assert "No outstanding ties" in result.output
