"""The contract every event source implements.

Sources are pluggable because no single one covers everywhere. Ticketmaster has
a proper API but does not cover Hungary; the sources that do are ordinary web
pages and calendar feeds. Adding a new kind means one class and one registry
entry.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ...fetch import Fetcher
from ..models import Event


class SourceError(Exception):
    """A source could not be read."""


@runtime_checkable
class EventSource(Protocol):
    """Anything that can produce a list of events."""

    name: str
    url: str

    def fetch(self, fetcher: Fetcher) -> list[Event]:
        """Retrieve and parse this source. Raises SourceError on failure."""
        ...


def build_source(config) -> EventSource:
    """Turn a config entry into a live source object."""
    kind = config.type
    if kind not in SOURCE_TYPES:
        raise SourceError(f"unknown source type {kind!r}; known types: {', '.join(sorted(SOURCE_TYPES))}")
    return SOURCE_TYPES[kind](name=config.name, url=config.url, selector=config.selector)


# Populated at the bottom of the concrete source modules to avoid import cycles.
SOURCE_TYPES: dict[str, type] = {}


def register(kind: str):
    def decorator(cls):
        SOURCE_TYPES[kind] = cls
        return cls

    return decorator
