"""Operational alerts: tell the operator that paper-watch itself is broken.

Distinct from the source warnings that ride inside a digest — those assume the
digest gets out. Alerts are for when it does not: a crashed tick (raised by
systemd's OnFailure= via `paper-watch alert`) or a digest still owed long after
its due point (raised by `runtime.run` itself).

Every channel is best-effort and independent. The log file is written first
because it is the one that cannot fail with the machine still up; desktop,
Slack and email are then each tried and their outcome recorded. A failure in
one never stops the next, and nothing here ever raises to the caller.
"""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from paper_watch.config import AlertsConfig, Config, SmtpConfig

log = logging.getLogger(__name__)

DesktopNotify = Callable[[str, str], None]
SlackPost = Callable[[str, str, str], None]  # (workspace, channel, text)

CHANNELS = ("log", "desktop", "slack", "email")


class Sender(Protocol):
    def send(self, *, subject: str, html: str, to_addr: str | None = None) -> None: ...


def _timestamp(now: datetime | None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def append_log(path: str | Path, subject: str, body: str, *, now: datetime | None = None) -> None:
    """One line per alert; newlines in the body are flattened so the file greps."""
    flat = " | ".join(part.strip() for part in body.splitlines() if part.strip())
    line = f"{_timestamp(now)} {subject}: {flat}\n"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)


def desktop_notify(subject: str, body: str) -> None:
    subprocess.run(
        ["notify-send", "--urgency=critical", "--app-name=paper-watch", subject, body],
        check=True,
        capture_output=True,
        timeout=15,
    )


def slack_poster(config: Config) -> SlackPost:
    """A poster bound to the config's workspace tokens (read from the env)."""

    def post(workspace: str, channel: str, text: str) -> None:
        import httpx

        ws = next((w for w in (config.slack.workspaces if config.slack else []) if w.name == workspace), None)
        if ws is None:
            raise LookupError(f"no slack workspace named {workspace!r} in config")
        token = os.environ.get(ws.token_env)
        if not token:
            raise LookupError(f"no Slack token for workspace {workspace} (env {ws.token_env})")
        resp = httpx.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {token}"},
            json={"channel": channel, "text": text},
            timeout=20,
        )
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"chat.postMessage: {data.get('error', resp.status_code)}")

    return post


def send_alert(
    cfg: AlertsConfig,
    subject: str,
    body: str,
    *,
    skip: set[str] | None = None,
    smtp: SmtpConfig | None = None,
    sender: Sender | None = None,
    desktop_notify: DesktopNotify = desktop_notify,
    slack_post: SlackPost | None = None,
    now: datetime | None = None,
) -> dict[str, str | None]:
    """Fan `subject`/`body` out to every enabled channel.

    Returns one entry per channel: None on success, "disabled"/"skipped", or
    the error text. `skip` names channels the caller already handled itself
    (alert.sh writes the log and notify-send before Python is even involved).
    """
    skip = skip or set()
    result: dict[str, str | None] = {}

    def attempt(name: str, enabled: bool, fn: Callable[[], None]) -> None:
        if name in skip:
            result[name] = "skipped"
            return
        if not enabled:
            result[name] = "disabled"
            return
        try:
            fn()
            result[name] = None
        except Exception as exc:  # noqa: BLE001 - best-effort by design
            result[name] = f"{type(exc).__name__}: {exc}"
            log.warning("alert channel %s failed: %s", name, result[name])

    attempt("log", True, lambda: append_log(cfg.log_file, subject, body, now=now))
    attempt("desktop", cfg.desktop, lambda: desktop_notify(subject, body))

    def do_slack() -> None:
        if slack_post is None:
            raise LookupError("no Slack poster available")
        slack_post(cfg.slack_workspace, cfg.slack_channel, f"*{subject}*\n{body}")

    attempt("slack", bool(cfg.slack_channel), do_slack)

    def do_email() -> None:
        if sender is None or smtp is None:
            raise LookupError("no email sender available")
        to = smtp.from_addr or smtp.username
        if not to:
            raise LookupError("smtp.from_addr is empty")
        sender.send(subject=f"[paper-watch alert] {subject}", html=f"<pre>{body}</pre>", to_addr=to)

    attempt("email", cfg.email, do_email)
    return result
