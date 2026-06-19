"""Gmail SMTP delivery.

The app password comes from the environment (SMTP_APP_PASSWORD), never config.
The SMTP transport is injectable so tests don't touch the network.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Callable

from paper_watch.config import SmtpConfig

SmtpFactory = Callable[[str, int], smtplib.SMTP]


class GmailSender:
    def __init__(
        self,
        smtp: SmtpConfig,
        app_password: str,
        smtp_factory: SmtpFactory | None = None,
    ):
        self.smtp_config = smtp
        self.app_password = app_password
        self._factory = smtp_factory or (lambda host, port: smtplib.SMTP(host, port))

    def send(self, *, subject: str, html: str, to_addr: str | None = None) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.smtp_config.from_addr
        msg["To"] = to_addr or self.smtp_config.to_addr
        msg.set_content(html, subtype="html")

        with self._factory(self.smtp_config.host, self.smtp_config.port) as smtp:
            smtp.starttls()
            smtp.login(self.smtp_config.username, self.app_password)
            smtp.send_message(msg)
