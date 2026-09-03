"""Alerts fan out to every configured channel, best-effort: one channel failing
never stops the others, and the log file is always written first."""

from paper_watch import alerts
from paper_watch.config import AlertsConfig, SmtpConfig


class FakeSender:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send(self, *, subject, html, to_addr=None):
        if self.fail:
            raise RuntimeError("smtp down")
        self.sent.append((subject, html, to_addr))


def _cfg(tmp_path, **kw):
    base = dict(log_file=str(tmp_path / "alerts.log"), desktop=True, email=True,
                slack_workspace="far", slack_channel="C123")
    base.update(kw)
    if base["slack_channel"] is None:
        base["slack_workspace"] = None
    return AlertsConfig(**base)


def test_log_line_is_appended_with_timestamp_and_subject(tmp_path):
    cfg = _cfg(tmp_path, desktop=False, email=False, slack_channel=None)
    alerts.send_alert(cfg, "digest overdue", "body text")
    alerts.send_alert(cfg, "second", "more")
    lines = (tmp_path / "alerts.log").read_text().splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("digest overdue: body text")
    assert lines[0][:4].isdigit()  # ISO timestamp first


def test_multiline_body_is_flattened_in_the_log(tmp_path):
    cfg = _cfg(tmp_path, desktop=False, email=False, slack_channel=None)
    alerts.send_alert(cfg, "s", "line one\nline two")
    assert "line one | line two" in (tmp_path / "alerts.log").read_text()


def test_every_channel_is_tried_and_reported(tmp_path):
    cfg = _cfg(tmp_path)
    calls = []
    sender = FakeSender()
    result = alerts.send_alert(
        cfg, "subj", "body",
        smtp=SmtpConfig(from_addr="me@x", username="me@x"), sender=sender,
        desktop_notify=lambda s, b: calls.append(("desktop", s, b)),
        slack_post=lambda ws, ch, text: calls.append(("slack", ws, ch, text)),
    )
    assert ("desktop", "subj", "body") in calls
    assert ("slack", "far", "C123", "*subj*\nbody") in calls
    # Alert email goes to the operator only, never to the digest's to_addrs.
    assert sender.sent == [("[paper-watch alert] subj", "<pre>body</pre>", "me@x")]
    assert result == {"log": None, "desktop": None, "slack": None, "email": None}


def test_one_failing_channel_does_not_block_the_others(tmp_path):
    cfg = _cfg(tmp_path)
    calls = []

    def boom(*a):
        raise OSError("no dbus")

    result = alerts.send_alert(
        cfg, "subj", "body",
        smtp=SmtpConfig(from_addr="me@x", username="me@x"), sender=FakeSender(fail=True),
        desktop_notify=boom,
        slack_post=lambda ws, ch, text: calls.append("slack"),
    )
    assert calls == ["slack"]
    assert result["log"] is None
    assert "no dbus" in result["desktop"]
    assert "smtp down" in result["email"]
    assert (tmp_path / "alerts.log").read_text().count("subj") == 1


def test_disabled_channels_are_skipped(tmp_path):
    cfg = _cfg(tmp_path, desktop=False, email=False, slack_channel=None)
    calls = []
    result = alerts.send_alert(
        cfg, "s", "b",
        smtp=SmtpConfig(from_addr="me@x", username="me@x"), sender=FakeSender(),
        desktop_notify=lambda *a: calls.append("d"),
        slack_post=lambda *a: calls.append("s"),
    )
    assert calls == []
    assert result == {"log": None, "desktop": "disabled", "slack": "disabled", "email": "disabled"}


def test_skip_argument_suppresses_channels_already_handled(tmp_path):
    # alert.sh writes the log and notify-send itself before Python is involved.
    cfg = _cfg(tmp_path)
    result = alerts.send_alert(
        cfg, "s", "b", skip={"log", "desktop"},
        smtp=SmtpConfig(from_addr="me@x", username="me@x"), sender=FakeSender(),
        desktop_notify=lambda *a: 1 / 0, slack_post=lambda *a: None,
    )
    assert not (tmp_path / "alerts.log").exists()
    assert result["log"] == "skipped" and result["desktop"] == "skipped"


def test_slack_token_missing_is_reported_not_raised(tmp_path, monkeypatch):
    monkeypatch.delenv("SLACK_TOKEN_FAR", raising=False)
    cfg = _cfg(tmp_path, desktop=False, email=False)
    from paper_watch.config import Config
    config = Config(alerts=cfg, slack={"workspaces": [{"name": "far", "token_env": "SLACK_TOKEN_FAR"}]})
    result = alerts.send_alert(cfg, "s", "b", slack_post=alerts.slack_poster(config))
    assert "SLACK_TOKEN_FAR" in result["slack"]


def _fake_api(log):
    """A stand-in for alerts._slack_api: records (method, payload), replies canned."""

    def api(token, method, payload):
        log.append((method, payload))
        if method == "users.lookupByEmail":
            return {"ok": True, "user": {"id": "U42"}}
        if method == "conversations.open":
            return {"ok": True, "channel": {"id": "D99"}}
        if method == "chat.postMessage":
            return {"ok": True}
        raise AssertionError(method)

    return api


def _config_with_token(monkeypatch, **alerts_kw):
    from paper_watch.config import Config

    monkeypatch.setenv("SLACK_TOKEN_FAR", "xoxb-test")
    return Config(
        alerts=dict(slack_workspace="far", desktop=False, email=False, **alerts_kw),
        slack={"workspaces": [{"name": "far", "token_env": "SLACK_TOKEN_FAR"}]},
    )


def test_slack_user_id_target_opens_a_dm_and_posts_there(tmp_path, monkeypatch):
    log = []
    config = _config_with_token(monkeypatch, slack_user="U42", log_file=str(tmp_path / "a.log"))
    post = alerts.slack_poster(config, api=_fake_api(log))
    result = alerts.send_alert(config.alerts, "s", "b", slack_post=post)
    assert result["slack"] is None
    assert log == [
        ("conversations.open", {"users": "U42"}),
        ("chat.postMessage", {"channel": "D99", "text": "*s*\nb"}),
    ]


def test_slack_user_email_target_is_looked_up_first(tmp_path, monkeypatch):
    log = []
    config = _config_with_token(monkeypatch, slack_user="me@example.com", log_file=str(tmp_path / "a.log"))
    post = alerts.slack_poster(config, api=_fake_api(log))
    alerts.send_alert(config.alerts, "s", "b", slack_post=post)
    assert log[0] == ("users.lookupByEmail", {"email": "me@example.com"})
    assert log[1] == ("conversations.open", {"users": "U42"})


def test_slack_channel_target_posts_directly(tmp_path, monkeypatch):
    log = []
    config = _config_with_token(monkeypatch, slack_channel="C7", log_file=str(tmp_path / "a.log"))
    post = alerts.slack_poster(config, api=_fake_api(log))
    alerts.send_alert(config.alerts, "s", "b", slack_post=post)
    assert log == [("chat.postMessage", {"channel": "C7", "text": "*s*\nb"})]


def test_slack_api_error_is_reported_with_the_slack_error_code(tmp_path, monkeypatch):
    config = _config_with_token(monkeypatch, slack_user="U42", log_file=str(tmp_path / "a.log"))

    def api(token, method, payload):
        return {"ok": False, "error": "missing_scope", "needed": "im:write"}

    result = alerts.send_alert(config.alerts, "s", "b", slack_post=alerts.slack_poster(config, api=api))
    assert "conversations.open" in result["slack"] and "missing_scope" in result["slack"]


def test_desktop_default_resolves_at_call_time(tmp_path, monkeypatch):
    # The conftest guard against real notify-send popups relies on this:
    # patching the module attribute must reach send_alert's default channel.
    calls = []
    monkeypatch.setattr(alerts, "desktop_notify", lambda s, b: calls.append((s, b)))
    cfg = _cfg(tmp_path, email=False, slack_channel=None)
    alerts.send_alert(cfg, "subj", "body")
    assert calls == [("subj", "body")]
