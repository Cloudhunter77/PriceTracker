"""Deciding which events are worth telling you about.

Three independent filters: is it soon enough, is it close enough, and is it
something you said you cared about.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from .config import EventsConfig, Interest
from .geo import Geocoder, haversine_km
from .models import Event


def match_interests(event: Event, interests: list[Interest]) -> list[str]:
    """Names of the interests this event matches.

    Matching is accent- and case-insensitive, which is not optional in
    Hungarian: 'Színház', 'szinhaz' and 'SZÍNHÁZ' have to be one word.
    """
    haystack = event.searchable
    return [
        interest.name
        for interest in interests
        if any(keyword in haystack for keyword in interest.folded_keywords)
    ]


def within_window(event: Event, now: datetime, days: int) -> bool:
    """True when the event has not happened yet and starts soon enough.

    An event that started earlier today but runs late still counts as upcoming;
    the cutoff is the end of its start day, not the exact minute.
    """
    if event.starts_at > now + timedelta(days=days):
        return False
    end = event.ends_at or event.starts_at
    return end >= now - timedelta(hours=12)


def locate(event: Event, geocoder: Geocoder | None) -> Event:
    """Fill in coordinates from the event's address when the page didn't give any."""
    if event.lat is not None and event.lon is not None:
        return event
    if geocoder is None:
        return event
    found = geocoder.locate(event.address, event.venue)
    if found is not None:
        event.lat, event.lon = found
    return event


def distance_from_home(event: Event, config: EventsConfig) -> float | None:
    """Kilometres from home, or None when the event's location is unknown."""
    if event.lat is None or event.lon is None:
        return None
    return haversine_km(config.home.lat, config.home.lon, event.lat, event.lon)


def is_nearby(event: Event, config: EventsConfig) -> bool:
    """Within the configured radius.

    An event whose location could not be resolved is kept rather than dropped:
    the sources are city-scoped listings, so an unresolvable venue is far more
    likely to be a badly-formatted address than an event in another country.
    Its distance shows as unknown so you can judge it yourself.
    """
    distance = event.distance_km
    return distance is None or distance <= config.home.radius_km


def select(
    events: list[Event],
    config: EventsConfig,
    now: datetime,
    geocoder: Geocoder | None = None,
) -> list[Event]:
    """Filter and annotate a batch of events, soonest first.

    Filters run cheapest-first: date and keywords are free, geocoding costs a
    network round trip, so it only happens for events that already matter.
    """
    interests = config.active_interests
    selected: list[Event] = []
    for event in events:
        if not within_window(event, now, config.window_days):
            continue
        matched = match_interests(event, interests) if interests else []
        if interests and not matched:
            continue
        event.interests = matched
        locate(event, geocoder)
        event.distance_km = distance_from_home(event, config)
        if not is_nearby(event, config):
            continue
        selected.append(event)

    selected.sort(key=lambda e: (e.starts_at, e.title))
    return selected
