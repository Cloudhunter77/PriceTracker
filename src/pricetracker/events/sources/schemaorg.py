"""Read events from the schema.org markup a listing or venue page publishes.

This is the same idea the price tracker uses for products: Google requires
structured data for event rich results, so event sites publish it. The parsing
helpers in extract.py already handle the awkward parts — @graph wrappers, types
given as lists, prices as strings or numbers — so they are reused directly
rather than reimplemented.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import extruct

from ...extract import _has_type, _normalise_currency, _parse_price, _walk
from ...fetch import Fetcher, FetchError
from ..models import Event, as_aware
from .base import SourceError, register

log = logging.getLogger(__name__)

# schema.org Event and every subtype worth looking for.
EVENT_TYPES = (
    "Event",
    "MusicEvent",
    "TheaterEvent",
    "Festival",
    "ScreeningEvent",
    "SocialEvent",
    "ExhibitionEvent",
    "FoodEvent",
    "SportsEvent",
    "ComedyEvent",
    "DanceEvent",
    "LiteraryEvent",
    "VisualArtsEvent",
    "EducationEvent",
    "BusinessEvent",
    "Hackathon",
    "ChildrensEvent",
)

# An online-only event is never "nearby", whatever its listed address says.
_ONLINE_ONLY = "onlineeventattendancemode"

_CANCELLED = {"eventcancelled", "eventpostponed"}


@register("schemaorg")
class SchemaOrgSource:
    """Every event marked up on a single page."""

    def __init__(self, name: str, url: str, selector: str | None = None) -> None:
        self.name = name
        self.url = url
        self.selector = selector  # unused; kept for a uniform constructor

    def fetch(self, fetcher: Fetcher) -> list[Event]:
        try:
            page = fetcher.fetch(self.url)
        except FetchError as exc:
            raise SourceError(f"fetch failed: {exc}") from exc
        return self.parse(page.html)

    def parse(self, html: str) -> list[Event]:
        """Pull every event out of a page's markup."""
        data = extruct.extract(html, syntaxes=["json-ld", "microdata"], errors="ignore")
        nodes = list(_walk(data.get("json-ld", [])))
        # Microdata nests real fields under "properties"; flatten those too.
        for node in _walk(data.get("microdata", [])):
            props = node.get("properties")
            if isinstance(props, dict):
                nodes.append({"@type": node.get("type", ""), **props})

        events: list[Event] = []
        seen: set[str] = set()
        for node in nodes:
            if not _has_type(node, *EVENT_TYPES):
                continue
            event = self._to_event(node)
            if event is None or event.uid in seen:
                continue
            seen.add(event.uid)
            events.append(event)
        return events

    def _to_event(self, node: dict) -> Event | None:
        title = _text(node.get("name"))
        starts_at = as_aware(_parse_datetime(node.get("startDate")))
        if not title or starts_at is None:
            return None  # an event without a name or a date is not usable

        mode = _text(node.get("eventAttendanceMode")) or ""
        if _ONLINE_ONLY in mode.rsplit("/", 1)[-1].replace(" ", "").casefold():
            return None
        status = _text(node.get("eventStatus")) or ""
        if status.rsplit("/", 1)[-1].replace(" ", "").casefold() in _CANCELLED:
            return None

        venue, address, lat, lon = _location(node.get("location"))
        price, currency = _offer(node.get("offers"))

        return Event(
            title=title,
            starts_at=starts_at,
            ends_at=as_aware(_parse_datetime(node.get("endDate"))),
            source=self.name,
            url=_text(node.get("url")),
            venue=venue,
            address=address,
            lat=lat,
            lon=lon,
            price=price,
            currency=currency,
            category=_text(node.get("genre")) or _first_type(node),
            description=_text(node.get("description")),
        )


def _text(value: Any) -> str | None:
    """Flatten the several shapes a schema.org string value arrives in."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (list, tuple)):
        for item in value:
            text = _text(item)
            if text:
                return text
        return None
    if isinstance(value, dict):
        for key in ("name", "@value", "@id", "url"):
            text = _text(value.get(key))
            if text:
                return text
    return None


def _first_type(node: dict) -> str | None:
    raw = node.get("@type") or node.get("type")
    values = raw if isinstance(raw, list) else [raw]
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.rsplit("/", 1)[-1]
    return None


def _parse_datetime(value: Any) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    # Some feeds emit a date with no time, or a trailing zone abbreviation.
    for candidate in (text[:10], text.split("T")[0]):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            continue
    log.debug("could not parse date %r", text)
    return None


def _location(value: Any) -> tuple[str | None, str | None, float | None, float | None]:
    """Venue name, printable address, and coordinates when the page gives them."""
    if isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, str):
        return value.strip() or None, None, None, None
    if not isinstance(value, dict):
        return None, None, None, None

    venue = _text(value.get("name"))
    address = _address(value.get("address"))
    lat = lon = None
    geo = value.get("geo")
    if isinstance(geo, list) and geo:
        geo = geo[0]
    if isinstance(geo, dict):
        lat = _float(geo.get("latitude"))
        lon = _float(geo.get("longitude"))
    return venue, address, lat, lon


def _address(value: Any) -> str | None:
    if isinstance(value, list) and value:
        value = value[0]
    if isinstance(value, str):
        return value.strip() or None
    if not isinstance(value, dict):
        return None
    parts = [
        _text(value.get(key))
        for key in ("streetAddress", "postalCode", "addressLocality", "addressRegion", "addressCountry")
    ]
    joined = ", ".join(p for p in parts if p)
    return joined or None


def _offer(value: Any) -> tuple[Any, str | None]:
    """Cheapest ticket price advertised, if any."""
    prices: list[tuple[Any, str | None]] = []
    for offer in _walk(value):
        for key in ("price", "lowPrice"):
            price = _parse_price(offer.get(key))
            if price is not None:
                prices.append((price, _normalise_currency(offer.get("priceCurrency"))))
                break
    if not prices:
        return None, None
    return min(prices, key=lambda p: p[0])


def _float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None
