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
from urllib.parse import urljoin, urlsplit

import extruct

from parsel import Selector

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


# Listing pages routinely carry no event markup at all: Google asks for it on
# each event's own "leaf" page, so that is where sites put it. When `follow` is
# set the listing is treated as an index and each matching link is visited.
DEFAULT_MAX_LINKS = 40


@register("schemaorg")
class SchemaOrgSource:
    """Events marked up on a page, optionally crawling one level deeper.

    Without `follow` this reads a single page. With it, the page is treated as a
    listing: links whose path contains the pattern are visited and their markup
    read instead. One level only — this is an index-and-detail reader, not a
    crawler let loose on a site.
    """

    def __init__(
        self,
        name: str,
        url: str,
        selector: str | None = None,
        follow: str | None = None,
        max_links: int = DEFAULT_MAX_LINKS,
    ) -> None:
        self.name = name
        self.url = url
        self.selector = selector  # optional CSS scope for finding links
        self.follow = follow
        self.max_links = max_links

    def fetch(self, fetcher: Fetcher) -> list[Event]:
        try:
            page = fetcher.fetch(self.url)
        except FetchError as exc:
            raise SourceError(f"fetch failed: {exc}") from exc

        if not self.follow:
            return self.parse(page.html)

        links = self.find_links(page.html, page.url)
        if not links:
            raise SourceError(
                f"no links matching {self.follow!r} on {page.url} — check the pattern, "
                "or drop `follow` if the listing carries its own event markup"
            )

        events: list[Event] = []
        seen: set[str] = set()
        for link in links:
            try:
                detail = fetcher.fetch(link)
            except FetchError as exc:
                # One dead detail page must not lose the rest of the listing.
                log.info("%s: skipping %s (%s)", self.name, link, exc)
                continue
            for event in self.parse(detail.html):
                if event.url is None:
                    event.url = link
                if event.uid not in seen:
                    seen.add(event.uid)
                    events.append(event)
        return events

    def find_links(self, html: str, base_url: str) -> list[str]:
        """Detail-page links from a listing, deduplicated and capped.

        Capped because a listing can link to hundreds of pages and each one is a
        separate throttled request; the daily run should take a minute, not an hour.
        """
        sel = Selector(text=html)
        if self.selector:
            sel = Selector(text="".join(sel.css(self.selector).getall()) or html)

        host = urlsplit(base_url).netloc
        links: list[str] = []
        seen: set[str] = set()
        for href in sel.css("a::attr(href)").getall():
            absolute = urljoin(base_url, href.strip())
            parts = urlsplit(absolute)
            if parts.scheme not in ("http", "https") or parts.netloc != host:
                continue  # stay on the site we were pointed at
            if self.follow not in parts.path:
                continue
            clean = f"{parts.scheme}://{parts.netloc}{parts.path}"
            if clean in seen:
                continue
            seen.add(clean)
            links.append(clean)
            if len(links) >= self.max_links:
                break
        return links

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
