"""The events half of the daily run, with the network stubbed out."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pricetracker.events.config import EventsConfig
from pricetracker.events.runner import check_events
from pricetracker.events.store import EventStore
from pricetracker.fetch import FetchError, FetchResult

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures"

LISTING = "https://example.hu/programok"
FEED = "https://example.hu/venue.ics"


class StubFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def fetch(self, url):
        self.requested.append(url)
        page = self.pages[url]
        if isinstance(page, Exception):
            raise page
        return FetchResult(url=url, html=page, status_code=200)

    def close(self):
        pass


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def make_config(**overrides) -> EventsConfig:
    data = {
        # Centred on the fixtures' Budapest venues, with a generous window so
        # the fixed fixture dates stay inside it.
        "home": {"lat": 47.4979, "lon": 19.0402, "radius_km": 15},
        "window_days": 60,
        "geocode": False,
        "interests": [
            {"name": "Live music", "keywords": ["koncert", "jazz"]},
            {"name": "Culture", "keywords": ["színház", "tánc"]},
        ],
        "sources": [
            {"name": "port.hu", "type": "schemaorg", "url": LISTING},
            {"name": "Trafó", "type": "ics", "url": FEED},
        ],
    }
    data.update(overrides)
    return EventsConfig.model_validate(data)


def stub():
    return StubFetcher({LISTING: fixture("events_jsonld.html"), FEED: fixture("venue.ics")})


def test_reads_every_source(event_store):
    fetcher = stub()
    outcome = check_events(make_config(), event_store, fetcher=fetcher, now=NOW)

    assert fetcher.requested == [LISTING, FEED]
    titles = [e.title for e in outcome.matched]
    assert "Éjszakai Koncert a Dunán" in titles
    assert "Kortárs tánc: Fivefold" in titles


def test_events_are_sorted_soonest_first(event_store):
    outcome = check_events(make_config(), event_store, fetcher=stub(), now=NOW)
    starts = [e.starts_at for e in outcome.matched]
    assert starts == sorted(starts)


def test_irrelevant_events_are_filtered_out(event_store):
    config = make_config(interests=[{"name": "Tech", "keywords": ["hackathon"]}])
    outcome = check_events(config, event_store, fetcher=stub(), now=NOW)
    assert outcome.matched == []
    assert outcome.scanned > 0, "they were read, just not matched"


def test_one_dead_source_does_not_stop_the_others(event_store):
    fetcher = StubFetcher({LISTING: FetchError("HTTP 503"), FEED: fixture("venue.ics")})
    outcome = check_events(make_config(), event_store, fetcher=fetcher, now=NOW)

    assert len(outcome.failures) == 1
    assert outcome.failures[0].name == "port.hu"
    assert "HTTP 503" in outcome.failures[0].error
    assert outcome.matched, "the working feed still produced events"


def test_unparseable_feed_is_reported_not_raised(event_store):
    fetcher = StubFetcher({LISTING: fixture("events_jsonld.html"), FEED: "not a calendar"})
    outcome = check_events(make_config(), event_store, fetcher=fetcher, now=NOW)
    assert [f.name for f in outcome.failures] == ["Trafó"]


def test_disabled_sources_are_not_fetched(event_store):
    config = make_config(
        sources=[
            {"name": "port.hu", "type": "schemaorg", "url": LISTING},
            {"name": "Trafó", "type": "ics", "url": FEED, "enabled": False},
        ]
    )
    fetcher = stub()
    check_events(config, event_store, fetcher=fetcher, now=NOW)
    assert fetcher.requested == [LISTING]


# ---- telling you once --------------------------------------------------


def test_everything_is_new_on_the_first_run(event_store):
    outcome = check_events(make_config(), event_store, fetcher=stub(), now=NOW)
    assert len(outcome.new) == len(outcome.matched) > 0


def test_second_run_reports_nothing_new(event_store):
    first = check_events(make_config(), event_store, fetcher=stub(), now=NOW)
    assert first.new

    later = NOW + timedelta(days=1)
    second = check_events(make_config(), event_store, fetcher=stub(), now=later)

    assert second.matched, "they are still upcoming"
    assert second.new == [], "but you have already been told about them"


def test_a_genuinely_new_event_is_reported(event_store):
    check_events(make_config(), event_store, fetcher=stub(), now=NOW)

    extra = fixture("venue.ics").replace(
        "BEGIN:VEVENT\nUID:trafo-2026-0912@example.hu",
        "BEGIN:VEVENT\nUID:trafo-new@example.hu\nSUMMARY:Új jazz koncert\n"
        "DTSTART:20260918T190000Z\nLOCATION:Trafó Ház\nEND:VEVENT\n"
        "BEGIN:VEVENT\nUID:trafo-2026-0912@example.hu",
    )
    fetcher = StubFetcher({LISTING: fixture("events_jsonld.html"), FEED: extra})
    later = NOW + timedelta(days=1)
    outcome = check_events(make_config(), event_store, fetcher=fetcher, now=later)

    assert [e.title for e in outcome.new] == ["Új jazz koncert"]


def test_dry_run_writes_nothing(event_store):
    outcome = check_events(make_config(), event_store, fetcher=stub(), now=NOW, dry_run=True)
    assert outcome.new
    assert not event_store.events_path.exists()
    assert not event_store.seen_path.exists()


def test_history_reloads_from_disk(event_store):
    check_events(make_config(), event_store, fetcher=stub(), now=NOW)

    reloaded = EventStore(events_path=event_store.events_path, seen_path=event_store.seen_path)
    upcoming = reloaded.upcoming(now=NOW)
    assert [e.title for e in upcoming] == [e.title for e in reloaded.events()]
    assert upcoming[0].starts_at.tzinfo is not None


def test_upcoming_deduplicates_repeated_sightings(event_store):
    """The same event recorded on many days must appear once."""
    check_events(make_config(), event_store, fetcher=stub(), now=NOW)
    check_events(make_config(), event_store, fetcher=stub(), now=NOW + timedelta(days=1))

    reloaded = EventStore(events_path=event_store.events_path, seen_path=event_store.seen_path)
    upcoming = reloaded.upcoming(now=NOW)
    assert len(upcoming) == len({e.uid for e in upcoming})


def test_past_events_drop_out_of_upcoming(event_store):
    check_events(make_config(), event_store, fetcher=stub(), now=NOW)
    assert event_store.upcoming(now=NOW + timedelta(days=365)) == []


def test_seen_state_is_pruned(event_store):
    old = (NOW - timedelta(days=200)).isoformat()
    kept = event_store.prune_seen({"a": old, "b": NOW.isoformat(), "c": "nonsense"}, NOW)
    assert set(kept) == {"b"}
