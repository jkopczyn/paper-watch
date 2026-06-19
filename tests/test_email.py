from paper_watch.config import SmtpConfig
from paper_watch.delivery.email import GmailSender


class FakeSMTP:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.events = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.events.append("close")
        return False

    def starttls(self):
        self.events.append("starttls")

    def login(self, user, password):
        self.events.append(("login", user, password))

    def send_message(self, msg):
        self.events.append(("send", msg))


def test_gmail_sender_builds_and_sends():
    created = {}

    def factory(host, port):
        smtp = FakeSMTP(host, port)
        created["smtp"] = smtp
        return smtp

    cfg = SmtpConfig(
        host="smtp.gmail.com",
        port=587,
        username="me@gmail.com",
        from_addr="me@gmail.com",
        to_addr="me@gmail.com",
    )
    sender = GmailSender(cfg, app_password="app-pw", smtp_factory=factory)
    sender.send(subject="paper-watch digest", html="<p>hi</p>")

    smtp = created["smtp"]
    assert smtp.host == "smtp.gmail.com" and smtp.port == 587
    # STARTTLS before login before send before close
    assert smtp.events[0] == "starttls"
    assert smtp.events[1] == ("login", "me@gmail.com", "app-pw")
    assert smtp.events[2][0] == "send"
    assert smtp.events[-1] == "close"

    msg = smtp.events[2][1]
    assert msg["Subject"] == "paper-watch digest"
    assert msg["From"] == "me@gmail.com"
    assert msg["To"] == "me@gmail.com"
    assert msg.get_content_type() == "text/html"


def test_gmail_sender_to_override():
    smtps = []
    sender = GmailSender(
        SmtpConfig(username="me@gmail.com", from_addr="me@gmail.com", to_addr="default@x.com"),
        app_password="pw",
        smtp_factory=lambda h, p: smtps.append(s := FakeSMTP(h, p)) or s,
    )
    sender.send(subject="s", html="<p>x</p>", to_addr="other@x.com")
    msg = smtps[0].events[2][1]
    assert msg["To"] == "other@x.com"
