"""Tests for the Nitter session-generation helpers under deploy/nitter.

These scripts live outside the package (they run via `uv run` with their own
deps), so we add their directory to sys.path to import the pure-Python bits.
The browser/login automation isn't unit-testable, but the twid -> user-id
parsing that both the cookie and browser tools rely on is, and a wrong id makes
Nitter silently reject the session -- so it's worth pinning down.
"""

import sys
from pathlib import Path

import pytest

NITTER_TOOLS = Path(__file__).resolve().parents[1] / "deploy" / "nitter"
sys.path.insert(0, str(NITTER_TOOLS))

from add_cookie_session import parse_user_id  # noqa: E402


@pytest.mark.parametrize(
    "twid,expected",
    [
        ("u=1234567890", "1234567890"),            # plain
        ("u%3D1234567890", "1234567890"),          # url-encoded '='
        ('"u=1234567890"', "1234567890"),          # quoted whole value
        ("twid=u=1234567890", "1234567890"),       # with twid= prefix
        ('twid="u=1234567890"', "1234567890"),     # prefixed + quoted
        ("u=1234567890&foo=bar", "1234567890"),    # trailing params
    ],
)
def test_parse_user_id_extracts_numeric_id(twid, expected):
    assert parse_user_id(twid) == expected


@pytest.mark.parametrize("twid", ["", None])
def test_parse_user_id_returns_none_when_empty(twid):
    assert parse_user_id(twid) is None
