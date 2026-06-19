"""Source adapters. Each yields RawItem objects from one upstream feed."""

from __future__ import annotations

from typing import Callable, Iterator, Protocol

from paper_watch.models import RawItem

# A fetcher takes a URL and returns the response body text. Injected for testing.
Fetcher = Callable[[str], str]


class Source(Protocol):
    name: str

    def fetch(self, since: str | None = None) -> Iterator[RawItem]:
        ...
