from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_real_desktop_alerts(monkeypatch):
    """No test may pop a real notify-send alert on the developer's desktop.

    AlertsConfig defaults desktop to true, so any test config without an
    `alerts:` section reaches the real channel (this queued up popups on
    Jacob's desktop, 2026-09-02). send_alert resolves its desktop default
    at call time, so patching the module attribute covers every path.
    A test that wants the channel passes its own desktop_notify stub.
    """
    monkeypatch.setattr(
        "paper_watch.alerts.desktop_notify", lambda subject, body: None
    )


@pytest.fixture
def fixture_text():
    def _read(name: str) -> str:
        return (FIXTURES / name).read_text()

    return _read
