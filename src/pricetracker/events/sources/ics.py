"""Read events from an iCalendar (.ics) feed.

Where a venue publishes one, this is the most reliable source there is: the
format is unambiguous, so nothing has to be inferred from markup that a redesign
can change.
"""

from __future__ import annotations

import logging

from icalendar import Calendar

from ...fetch import Fetcher, FetchError
from ..models import Event, as_aware
from .base import SourceError, register

log = logging.getLogger(__name__)

_CANCELLED = {"cancelled"}


@register("ics")
class IcsSource:
    """Every VEVENT in a calendar feed."""

    def __init__(self, name: str, url: str, **_ignored) -> None:
        # A calendar feed is already the detail level, so link-following
        # options are accepted and ignored rather than rejected.
        self.name = name
        self.url = url

    def fetch(self, fetcher: Fetcher) -> list[Event]:
        try:
            page = fetcher.fetch(self.url)
        except FetchError as exc:
            raise SourceError(f"fetch failed: {exc}") from exc
        return self.parse(page.html)

    def parse(self, text: str) -> list[Event]:
        try:
            calendar = Calendar.from_ical(text)
        except (ValueError, IndexError) as exc:
            raise SourceError(f"not a valid iCalendar feed: {exc}") from exc

        events: list[Event] = []
        for component in calendar.walk("VEVENT"):
            event = self._to_event(component)
            if event is not None:
                events.append(event)
        return events

    def _to_event(self, component) -> Event | None:
        title = _text(component.get("SUMMARY"))
        start = component.get("DTSTART")
        starts_at = as_aware(start.dt) if start is not None else None
        if not title or starts_at is None:
            return None
        if (_text(component.get("STATUS")) or "").casefold() in _CANCELLED:
            return None

        end = component.get("DTEND")
        lat = lon = None
        geo = component.get("GEO")
        if geo is not None:
            try:
                lat, lon = float(geo.latitude), float(geo.longitude)
            except (AttributeError, TypeError, ValueError):
                lat = lon = None

        location = _text(component.get("LOCATION"))
        return Event(
            title=title,
            starts_at=starts_at,
            ends_at=as_aware(end.dt) if end is not None else None,
            source=self.name,
            url=_text(component.get("URL")),
            # An ICS LOCATION is one free-text field; treat it as both the venue
            # label and the string to geocode.
            venue=location,
            address=location,
            lat=lat,
            lon=lon,
            category=_text(component.get("CATEGORIES")),
            description=_text(component.get("DESCRIPTION")),
            # A feed's own UID is stabler than anything we could derive.
            uid=_text(component.get("UID")),
        )


def _text(value) -> str | None:
    """icalendar returns vText/vCategory wrappers rather than plain strings."""
    if value is None:
        return None
    if hasattr(value, "cats"):  # vCategory
        return ", ".join(str(c) for c in value.cats) or None
    text = str(value).strip()
    return text or None
