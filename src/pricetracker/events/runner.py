"""The event half of the daily run: read every source, keep what matters."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from ..fetch import Fetcher
from ..store import utcnow
from .config import EventsConfig
from .geo import Geocoder
from .matching import select
from .models import Event
from .sources import SourceError, build_source
from .store import EventStore

log = logging.getLogger(__name__)


@dataclass
class SourceFailure:
    name: str
    url: str
    error: str


@dataclass
class EventOutcome:
    matched: list[Event] = field(default_factory=list)
    new: list[Event] = field(default_factory=list)
    failures: list[SourceFailure] = field(default_factory=list)
    seen: dict[str, str] = field(default_factory=dict)
    scanned: int = 0


def check_events(
    config: EventsConfig,
    store: EventStore,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    fetcher: Fetcher | None = None,
    geocoder: Geocoder | None = None,
    user_agent: str = "PriceTracker/0.1",
) -> EventOutcome:
    """Read every enabled source and return the events worth mentioning.

    `matched` is everything upcoming and relevant; `new` is the subset you have
    not been told about before, which is what actually gets emailed.
    """
    now = now or utcnow()
    outcome = EventOutcome(seen=store.load_seen())

    owns_fetcher = fetcher is None
    fetcher = fetcher or Fetcher(user_agent=user_agent)
    owns_geocoder = geocoder is None
    if geocoder is None and config.geocode:
        geocoder = Geocoder(user_agent=user_agent)

    found: list[Event] = []
    try:
        for source_config in config.active_sources:
            try:
                source = build_source(source_config)
                events = source.fetch(fetcher)
            except SourceError as exc:
                # One broken source must not stop the others.
                log.info("source %s failed: %s", source_config.name, exc)
                outcome.failures.append(
                    SourceFailure(name=source_config.name, url=source_config.url, error=str(exc))
                )
                continue
            log.debug("%s produced %d events", source_config.name, len(events))
            found.extend(events)

        outcome.scanned = len(found)
        outcome.matched = select(found, config, now, geocoder)
    finally:
        if owns_fetcher:
            fetcher.close()
        if owns_geocoder and geocoder is not None:
            geocoder.close()

    stamp = now.isoformat()
    outcome.new = [e for e in outcome.matched if (e.uid or e.stable_uid()) not in outcome.seen]
    for event in outcome.new:
        outcome.seen[event.uid or event.stable_uid()] = stamp

    if not dry_run:
        store.append(outcome.matched)
        store.save_seen(store.prune_seen(outcome.seen, now))

    return outcome
